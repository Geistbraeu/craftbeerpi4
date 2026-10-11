import asyncio
import copy
import json
import logging
import os
import os.path
import tempfile
from os import listdir
from os.path import isfile, join

import cbpi
import shortuuid
import yaml
from cbpi.api.dataclasses import (NotificationAction, NotificationType, Props,
                                  Step)
from tabulate import tabulate

from ..api.step import StepMove, StepResult, StepState


class StepController:

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.logger = logging.getLogger(__name__)
        self.path = self.cbpi.config_folder.get_file_path("step_data.json")
        self.basic_data = {}
        self.step = None
        self.types = {}
        self.profile = []
        self._save_lock = asyncio.Lock()
        self._checkpoint_task = None
        self._shutting_down = False
        self._load_failed = False
        self.cbpi.app.on_cleanup.append(self.shutdown)

    async def init(self):
        logging.info("INIT STEP Controller")
        self.load()
        await self.restore_steps()
        self._checkpoint_task = asyncio.create_task(self._checkpoint_loop())

    async def _checkpoint_loop(self):
        while True:
            await asyncio.sleep(60)
            if self.find_by_status(StepState.ACTIVE) is not None:
                try:
                    await self.save()
                except OSError:
                    # save() reports the error; keep trying subsequent checkpoints.
                    continue

    async def restore_steps(self):
        automatic_steps = []
        for step in self.profile:
            if step.status not in (StepState.ACTIVE, StepState.STOP):
                continue
            was_active = step.status == StepState.ACTIVE
            automatic = was_active and step.props.get("RecoveryMode", "Paused") == "Automatic"
            supported = step.instance is not None and step.instance.timer_recovery
            if supported and step.timer_state is not None:
                try:
                    step.instance.restore_timer_state(
                        step.timer_state,
                        count_downtime=was_active and step.props.get("RecoveryDowntime", "No") == "Yes",
                        keep_deadline=automatic,
                    )
                except (ValueError, TypeError, KeyError) as error:
                    self.logger.error("Invalid timer state for step %s: %s", step.id, error)
                    self.cbpi.notify(
                        step.name, "Invalid saved timer. Restore paused with original duration.",
                        NotificationType.ERROR,
                    )
                    step.instance.timer = None
                    step.instance.recovery_state = None
                    step.timer_state = None
                    automatic = False
            elif supported:
                self.cbpi.notify(
                    step.name, "No saved timer remaining. Original duration will be used.",
                    NotificationType.WARNING,
                )
                automatic = False
            else:
                automatic = False

            step.status = StepState.STOP
            if automatic:
                automatic_steps.append(step)
            else:
                self.cbpi.notify(
                    step.name, "Step restored paused. Press Start to continue.",
                    NotificationType.INFO,
                )
        try:
            await self.save()
            for step in automatic_steps:
                await self.start_step(step)
            if automatic_steps:
                await self.save()
        except OSError:
            for step in automatic_steps:
                await step.instance.stop()
                await step.instance.timer.stop()
                step.status = StepState.STOP
            raise

    def create(self, data):

        id = data.get("id")
        name = data.get("name")
        type = data.get("type")
        status = StepState(data.get("status", "I"))
        props = Props(data.get("props", {}))

        try:
            type_cfg = self.types.get(type)
            clazz = type_cfg.get("class")
            instance = clazz(self.cbpi, id, name, props, self.done)
        except Exception as e:
            logging.warning("Failed to create step instance %s - %s" % (id, e))
            instance = None
        step = Step(
            id, name, type=type, status=status, instance=instance, props=props,
            timer_state=data.get("timer_state"),
        )
        return step

    def load(self):

        # create file if not exists
        if os.path.exists(self.path) is False:
            logging.warning("Missing step_data.json file. INIT empty file")
            with open(self.path, "w") as file:
                json.dump(dict(basic={}, steps=[]), file, indent=4, sort_keys=True)

        try:
            with open(self.path) as json_file:
                data = json.load(json_file)
            if (
                not isinstance(data, dict) or not isinstance(data.get("basic"), dict)
                or not isinstance(data.get("steps"), list)
                or any(not isinstance(item, dict) for item in data["steps"])
            ):
                raise ValueError("Invalid brewing profile structure")
            profile = list(map(self.create, data["steps"]))
            self.basic_data = data["basic"]
            self.profile = profile
            self._load_failed = False
        except (OSError, ValueError, KeyError, TypeError):
            self._load_failed = True
            self.logger.exception("Cannot load brewing profile; file left unchanged")
            self.cbpi.notify(
                "Error", "Cannot load brewing profile. Check step_data.json.",
                NotificationType.ERROR,
            )
            raise

    async def add(self, item: Step):
        logging.debug("Add step")
        item.id = shortuuid.uuid()
        item.status = StepState.INITIAL
        try:
            type_cfg = self.types.get(item.type)
            clazz = type_cfg.get("class")
            item.instance = clazz(self.cbpi, item.id, item.name, item.props, self.done)
        except Exception as e:
            logging.warning("Failed to create step instance %s - %s " % (id, e))
            item.instance = None
        self.profile.append(item)
        await self.save()
        return item

    async def update(self, item: Step):

        logging.info("update step")
        old = self.find_by_id(item.id)
        if old is not None and old.status == StepState.ACTIVE:
            raise ValueError("Stop the step before editing it")
        try:
            type_cfg = self.types.get(item.type)
            clazz = type_cfg.get("class")
            item.instance = clazz(self.cbpi, item.id, item.name, item.props, self.done)
        except Exception as e:
            logging.warning("Failed to create step instance %s - %s " % (item.id, e))
            item.instance = None

        if old is not None and old.status == StepState.STOP and old.type == item.type:
            item.status = old.status
            item.timer_state = old.timer_state
            if item.instance is not None and item.instance.timer_recovery and item.timer_state:
                item.instance.restore_timer_state(item.timer_state)
        self.profile = list(
            map(lambda old: item if old.id == item.id else old, self.profile)
        )
        await self.save()
        return item

    async def save(self, refresh=True):
        if self._load_failed:
            raise ValueError("Refusing to overwrite a profile that failed to load")
        if self._shutting_down and refresh:
            return
        async with self._save_lock:
            if refresh:
                for item in self.profile:
                    if item.status in (StepState.ACTIVE, StepState.STOP):
                        if item.instance is not None and item.instance.timer_recovery:
                            item.timer_state = item.instance.get_timer_state()
                    else:
                        item.timer_state = None
            data = dict(
                basic=self.basic_data,
                steps=list(map(lambda item: item.to_dict(), self.profile)),
            )
            self._write_profile(data)
        self.push_udpate()

    def _write_profile(self, data):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=os.path.dirname(self.path), prefix=".step_data-",
                suffix=".tmp", delete=False,
            ) as file:
                temporary = file.name
                json.dump(data, file, indent=4, sort_keys=True, allow_nan=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
            if os.name == "posix":
                directory = os.open(os.path.dirname(self.path), os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except (OSError, ValueError, TypeError):
            self.logger.exception("Failed to save brewing profile")
            self.cbpi.notify(
                "Error", "Failed to save brewing profile. Timer recovery may be outdated.",
                NotificationType.ERROR,
            )
            raise
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    async def start(self):

        if self.find_by_status(StepState.ACTIVE) is not None:
            logging.error("Steps already running")
            return

        step = self.find_by_status(StepState.STOP)
        if step is not None:
            logging.info("Resume step")
            self.cbpi.push_update(
                topic="cbpi/notification",
                data=dict(type="info", title="Resume", message="Calling resume step"),
            )
            await self.start_step(step)
            await self.save()
            return

        step = self.find_by_status(StepState.INITIAL)
        if step is not None:
            logging.info("Start Step")
            self.cbpi.push_update(
                topic="cbpi/notification",
                data=dict(type="info", title="Start", message="Calling start step"),
            )
            self.push_udpate(complete=True)
            await self.start_step(step)
            await self.save()
            return
        self.cbpi.notify(
            "Brewing Complete",
            "Now the yeast will take over",
            action=[NotificationAction("OK")],
        )
        self.cbpi.push_update(
            topic="cbpi/notification",
            data=dict(
                type="info",
                title="Brewing completed",
                message="Now the yeast will take over",
            ),
        )
        logging.info("BREWING COMPLETE")

    async def previous(self):
        logging.info("Trigger Previous")

    async def next(self):
        logging.info("Trigger Next")
        # print("\n\n\n\n")
        # print(self.profile)
        # print("\n\n\n\n")
        step = self.find_by_status(StepState.ACTIVE)
        if step is not None:
            if step.instance is not None:
                await step.instance.next()

        step = self.find_by_status(StepState.STOP)
        if step is not None:
            if step.instance is not None:
                step.status = StepState.DONE
                await self.save()
                await self.start()
        else:
            logging.info("No Step is running")

    async def resume(self):
        step = self.find_by_status("P")
        if step is not None:
            instance = step.get("instance")
            if instance is not None:
                await self.start_step(step)
        else:
            logging.info("Nothing to resume")

    async def stop(self):
        step = self.find_by_status(StepState.ACTIVE)
        if step != None:
            logging.info("CALLING STOP STEP")
            try:
                await step.instance.stop()
                self.cbpi.push_update(
                    topic="cbpi/notification",
                    data=dict(type="info", title="Pause", message="Calling paue step"),
                )
                step.status = StepState.STOP

                await self.save()
            except Exception as e:
                logging.error("Failed to stop step - Id: %s" % step.id)

    async def reset_all(self):
        if self.find_by_status(StepState.ACTIVE) is not None:
            logging.error("Please stop before reset")
            return

        for item in self.profile:
            logging.info("Reset %s" % item)
            item.status = StepState.INITIAL
            try:
                await item.instance.reset()
                self.cbpi.push_update(
                    topic="cbpi/notification",
                    data=dict(type="info", title="Stop", message="Calling stop step"),
                )
            except:
                logging.warning("No Step Instance - Id: %s", item.id)
        await self.save()
        self.push_udpate()

    def get_types(self):
        result = {}
        for key, value in self.types.items():
            # if "ferment" not in str(value.get("class")).lower():
            result[key] = dict(
                name=value.get("name"),
                properties=value.get("properties"),
                actions=value.get("actions"),
            )
        return result

    def get_state(self):
        return {
            "basic": self.basic_data,
            "steps": list(map(lambda item: item.to_dict(), self.profile)),
            "types": self.get_types(),
        }

    async def move(self, id, direction: StepMove):
        index = self.get_index_by_id(id)
        if direction not in [-1, 1]:
            self.logger.error("Cant move. Direction 1 and -1 allowed")
            return
        self.profile[index], self.profile[index + direction] = (
            self.profile[index + direction],
            self.profile[index],
        )
        await self.save()
        self.push_udpate()

    async def delete(self, id):
        step = self.find_by_id(id)

        if step is None:
            logging.error("Cant find step - Nothing deleted - Id: %s", id)
            return

        if step.status == StepState.ACTIVE:
            logging.error("Cant delete active Step %s", id)
            return

        self.profile = list(filter(lambda item: item.id != id, self.profile))
        await self.save()

    async def shutdown(self, app=None):
        logging.info("Mash Profile Shutdown")
        if app is not None and self._checkpoint_task is not None:
            self._checkpoint_task.cancel()
            try:
                await self._checkpoint_task
            except asyncio.CancelledError:
                pass
        try:
            await self.save()
        finally:
            self._shutting_down = True
            try:
                for p in self.profile:
                    instance = p.instance
                    if instance is not None and instance.task is not None and not instance.task.done():
                        await instance.stop()
            finally:
                self._shutting_down = False

    def done(self, step, result):
        if result == StepResult.NEXT:
            step_current = self.find_by_id(step.id)
            if (
                self._shutting_down or step_current is None
                or step_current.instance is not step
            ):
                return
            step_current.status = StepState.DONE

            async def wrapper():
                if self._shutting_down or step_current not in self.profile:
                    return
                await self.save()
                if not self._shutting_down and step_current in self.profile:
                    await self.start()

            asyncio.create_task(wrapper())

    def find_by_status(self, status):
        return next((item for item in self.profile if item.status == status), None)

    def find_by_id(self, id):
        return next((item for item in self.profile if item.id == id), None)

    def get_index_by_id(self, id):
        return next((i for i, item in enumerate(self.profile) if item.id == id), None)

    def push_udpate(self, complete=False):
        if complete is True:
            self.cbpi.ws.send(dict(topic="mash_profile_update", data=self.get_state()))
            for item in self.profile:
                self.cbpi.push_update(
                    topic="cbpi/stepupdate/{}".format(item.id), data=(item.to_dict())
                )
        else:
            self.cbpi.ws.send(
                dict(
                    topic="step_update",
                    data=list(map(lambda item: item.to_dict(), self.profile)),
                )
            )

            step = self.find_by_status(StepState.ACTIVE)
            if step != None:
                self.cbpi.push_update(
                    topic="cbpi/stepupdate/{}".format(step.id), data=(step.to_dict())
                )

    async def start_step(self, step):
        try:
            logging.info("Try to start step %s" % step)
            await step.instance.start()
            step.status = StepState.ACTIVE
        except Exception as e:
            self.cbpi.notify(
                "Error",
                "Can't start step. Please check step in Mash Profile",
                NotificationType.ERROR,
            )
            logging.error("Failed to start step %s" % step)

    async def call_action(self, id, action, parameter) -> None:
        logging.info("Step Controller - call all Action {} {}".format(id, action))
        try:
            item = self.find_by_id(id)
            await item.instance.__getattribute__(action)(**parameter)
        except Exception as e:
            logging.error(
                "Step Controller -Failed to call action on {} {} {}".format(
                    id, action, e
                )
            )

    async def load_recipe(self, data):
        await self.shutdown()

        def add_runtime_data(item):
            item["status"] = "I"
            item["id"] = shortuuid.uuid()
            item.pop("timer_state", None)

        list(map(lambda item: add_runtime_data(item), data.get("steps")))
        self._write_profile(data)
        self.load()
        self.push_udpate(complete=True)

    async def clear(self):
        await self.shutdown()

        data = dict(basic=dict(), steps=[])
        self._write_profile(data)

        self.load()
        self.push_udpate(complete=True)

    async def savetobook(self):
        name = shortuuid.uuid()
        path = os.path.join(
            self.cbpi.config_folder.get_file_path("recipes"), "{}.yaml".format(name)
        )
        data = dict(
            basic=self.basic_data,
            steps=list(map(lambda item: item.to_dict(), self.profile)),
        )
        for step in data["steps"]:
            step.pop("timer_state", None)
            step["status"] = StepState.INITIAL.value
        with open(path, "w") as file:
            yaml.dump(data, file)
        self.push_udpate()
