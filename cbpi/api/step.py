import asyncio
import logging
import math
from abc import abstractmethod

import cbpi
from cbpi.api.base import CBPiBase
from cbpi.api.timer import Timer

__all__ = ["StepResult", "StepState", "StepMove", "CBPiStep", "CBPiFermentationStep"]

from enum import Enum

logging.basicConfig(
    format="%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s",
    datefmt="%Y-%m-%d:%H:%M:%S",
    level=logging.INFO,
)


class StepResult(Enum):
    STOP = 1
    NEXT = 2
    DONE = 3
    ERROR = 4


class StepState(Enum):
    INITIAL = "I"
    DONE = "D"
    ACTIVE = "A"
    ERROR = "E"
    STOP = "S"


class StepMove(Enum):
    UP = -1
    DOWN = 1


class CBPiStep(CBPiBase):

    timer_recovery = False

    def __init__(self, cbpi, id, name, props, on_done) -> None:
        self.name = name
        self.cbpi = cbpi
        self.id = id
        self.timer = None
        self._done_callback = on_done
        self.props = props
        self.cancel_reason: StepResult = None
        self.summary = ""
        self.summary2 = None
        self.task = None
        self.running: bool = False
        self.logger = logging.getLogger(__name__)
        self.recovery_state = None

    def create_timer(self):
        return Timer(
            int(self.props.get("Timer", 0)) * 60,
            on_update=self.on_timer_update,
            on_done=self.on_timer_done,
            on_change=self.save_props,
        )

    def get_timer_state(self):
        if not self.timer_recovery or self.timer is None:
            return None
        return self.timer.snapshot()

    def restore_timer_state(self, state, count_downtime=False, keep_deadline=False):
        if not isinstance(state, dict):
            raise ValueError("Invalid timer state")
        if not isinstance(state.get("started"), bool):
            raise ValueError("Invalid timer started flag")
        for key in ("remaining_seconds", "saved_at"):
            value = state.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Invalid timer %s" % key)
        deadline = state.get("end_time")
        if deadline is not None and (
            isinstance(deadline, bool) or not isinstance(deadline, int)
            or deadline < 0
        ):
            raise ValueError("Invalid timer end_time")
        self.recovery_state = state
        self.timer = self.create_timer()
        self.timer.restore(state, count_downtime, keep_deadline)
        self.summary = self.timer.get_time() if self.timer.started else "Waiting for Target Temp"

    async def on_recovery(self):
        pass

    async def adjust_timer_remaining(self, minutes=None, subtract_seconds=0):
        from cbpi.api.dataclasses import NotificationType

        step = self.cbpi.step.find_by_id(self.id)
        if step is None or step.instance is not self or step.status not in (
            StepState.ACTIVE, StepState.STOP
        ):
            self.cbpi.notify(
                self.name, "Start the step before changing its remaining time.",
                NotificationType.WARNING,
            )
            return
        seconds = None
        if minutes is not None:
            try:
                if isinstance(minutes, bool):
                    raise ValueError("Boolean time is not valid")
                value = float(minutes)
                if not math.isfinite(value) or value < 0 or not math.isfinite(value * 60):
                    raise ValueError("Invalid remaining time")
                seconds = round(value * 60)
            except (TypeError, ValueError, OverflowError):
                self.cbpi.notify(
                    self.name, "Enter a finite, non-negative remaining time in minutes.",
                    NotificationType.WARNING,
                )
                return
        if self.timer is None:
            self.timer = self.create_timer()
        if seconds is None:
            seconds = max(0, self.timer.remaining() - subtract_seconds)
        await self.timer.set_remaining(seconds)
        if seconds == 0:
            self.timer.started = True
        await self.on_timer_update(self.timer, seconds)
        self.recovery_state = self.get_timer_state()
        await self.save_props()
        self.cbpi.notify(
            self.name, "Remaining time set to %s" % self.timer.get_time(),
            NotificationType.INFO,
        )

    def _done(self, task):
        if self._done_callback is not None:
            try:
                result = self.cancel_reason if task.cancelled() else task.result()
                self._done_callback(self, result)
            except Exception as e:
                self.logger.error(e)

    async def start(self):
        self.logger.info("Start {}".format(self.name))
        self.running = True
        self.task = asyncio.create_task(self._run())
        self.task.add_done_callback(self._done)

    async def next(self):
        self.running = False
        self.cancel_reason = StepResult.NEXT
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass

    async def stop(self):
        try:
            self.running = False
            if self.task is not None and self.task.done() is False:
                self.cancel_reason = StepResult.STOP
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
                if self.timer_recovery and self.timer is not None:
                    await self.timer.stop()
                    self.recovery_state = self.get_timer_state()
                    self.summary = self.timer.get_time()
        except Exception as e:
            logging.error(e)

    async def reset(self):
        pass

    async def on_props_update(self, props):
        self.props = {**self.props, **props}

    async def save_props(self):
        await self.cbpi.step.save()

    async def push_update(self):
        self.cbpi.step.push_udpate()

    async def on_start(self):
        pass

    async def on_stop(self):
        pass

    async def _run(self):
        initializing = False
        try:
            if self.recovery_state is not None:
                await self.on_recovery()
            if (
                self.timer_recovery and self.timer is not None
                and self.timer.started and self.timer.remaining() == 0
            ):
                self.cbpi.notify(self.name, "Step time elapsed while inactive")
                self.cancel_reason = StepResult.NEXT
                self.running = False
                return self.cancel_reason
            initializing = True
            await self.on_start()
            if self.timer_recovery:
                await self.save_props()
            await self.run()
            self.cancel_reason = StepResult.DONE
        except asyncio.CancelledError as e:
            pass
        finally:
            if initializing:
                await self.on_stop()

        return self.cancel_reason

    @abstractmethod
    async def run(self):
        pass

    def __str__(self):
        return "name={} props={}, type={}".format(
            self.name, self.props, self.__class__.__name__
        )


class CBPiFermentationStep(CBPiBase):

    def __init__(self, cbpi, fermenter, step, props, on_done) -> None:
        self.fermenter = fermenter
        self.name = step.get("name")
        self.cbpi = cbpi
        self.id = step.get("id")
        self.timer = None
        self._done_callback = on_done
        self.props = props
        self.endtime = int(step.get("endtime"))
        self.cancel_reason: StepResult = None
        self.summary = ""
        self.task = None
        self.running: bool = False
        self.logger = logging.getLogger(__name__)
        self.step = step
        self.update_key = "fermenterstepupdate"

    def _done(self, task):
        if self._done_callback is not None:
            try:
                result = task.result()
                logging.info(result)
                logging.info(self.fermenter.id)
                fermenter = self.fermenter.id
                self._done_callback(self, result, fermenter)
            except Exception as e:
                self.logger.error(e)

    async def start(self):
        self.logger.info("Start {}".format(self.name))
        self.running = True
        self.task = asyncio.create_task(self._run())
        self.task.add_done_callback(self._done)

    async def next(self, fermenter=None):
        if fermenter is None:
            self.running = False
            self.cancel_reason = StepResult.NEXT
            self.task.cancel()
            await self.task
        else:
            await self.cbpi.fermenter.next(fermenter)

    async def stop(self):
        try:
            self.running = False
            if self.task is not None and self.task.done() is False:
                self.cancel_reason = StepResult.STOP
                logging.info(self.cancel_reason)
                self.task.cancel()
                await self.task
        except Exception as e:
            logging.error(e)

    async def reset(self):
        pass

    async def on_props_update(self, props):
        self.props = {**self.props, **props}

    async def update_endtime(self):
        await self.cbpi.fermenter.update_endtime(
            self.fermenter.id, self.id, self.endtime
        )

    async def save_props(self):
        self.cbpi.fermenter.save()

    async def push_update(self):
        self.cbpi.fermenter.push_update(self.update_key)

    async def on_start(self):
        pass

    async def on_stop(self):
        pass

    async def _run(self):
        try:
            await self.on_start()
            await self.run()
            self.cancel_reason = StepResult.DONE
        except asyncio.CancelledError as e:
            pass
        finally:
            await self.on_stop()

        return self.cancel_reason

    @abstractmethod
    async def run(self):
        pass

    def __str__(self):
        return "name={} props={}, type={}".format(
            self.name, self.props, self.__class__.__name__
        )
