import asyncio
import json
import os
import tempfile
import unittest
import yaml
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cbpi.api.dataclasses import Props, Step
from cbpi.api.step import CBPiStep, StepResult, StepState
from cbpi.api.timer import Timer
from cbpi.controller.recipe_controller import RecipeController
from cbpi.controller.step_controller import StepController
from cbpi.extension.mashstep import ActorStep, BoilStep, MashStep, WaitStep


class RecoveryTestStep(CBPiStep):
    timer_recovery = True

    async def on_start(self):
        self.cbpi.equipment_started()
        if self.timer is None:
            self.timer = self.create_timer()
        self.timer.start()

    async def on_timer_update(self, timer, seconds):
        self.summary = Timer.format_time(seconds)

    async def on_timer_done(self, timer):
        await self.next()

    async def on_stop(self):
        if self.timer is not None:
            await self.timer.stop()

    async def run(self):
        await asyncio.Event().wait()

    async def reset(self):
        self.recovery_state = None
        self.timer = self.create_timer()


class TimerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_preserves_adjustments_and_never_completes_step(self):
        done = AsyncMock()
        timer = Timer(600, on_done=done)
        with patch("cbpi.api.timer.time.time", return_value=1000):
            timer.start()
            await asyncio.sleep(0)
            await timer.add(300)
        with patch("cbpi.api.timer.time.time", return_value=1100):
            await timer.stop()
            await asyncio.sleep(0)
            self.assertEqual(timer.remaining(), 800)
            done.assert_not_called()
            timer.start()
            await asyncio.sleep(0)
            self.assertEqual(timer.end_time, 1900)
            await timer.add(-300)
            self.assertEqual(timer.remaining(), 500)
            await timer.stop()
            timer.reset()
            self.assertEqual(timer.remaining(), 600)
            self.assertFalse(timer.started)

    async def test_natural_expiry_calls_done_once_and_clamps_display(self):
        done = AsyncMock()
        timer = Timer(60, on_done=done)
        with patch("cbpi.api.timer.time.time", return_value=1000):
            timer.start()
            await asyncio.sleep(0)
            await timer.add(-300)
            self.assertEqual(timer.remaining(), 0)
            self.assertEqual(Timer.format_time(-1), "00:00:00")
        await asyncio.wait_for(timer._task, 2)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        done.assert_awaited_once()

    async def test_checkpoint_failure_does_not_stop_countdown(self):
        timer = Timer(60, on_change=AsyncMock(side_effect=OSError("disk full")))
        with self.assertLogs(level="ERROR"):
            timer.start()
            await asyncio.sleep(0)
        self.assertTrue(timer.running)
        await timer.stop()

    async def test_restore_deadline_or_remaining_without_starting(self):
        state = dict(started=True, remaining_seconds=600, end_time=1600, saved_at=1000)
        with patch("cbpi.api.timer.time.time", return_value=1200):
            timer = Timer(3600)
            timer.restore(state)
            self.assertEqual(timer.remaining(), 600)
            timer.restore(state, count_downtime=True)
            self.assertEqual(timer.remaining(), 400)
            self.assertFalse(timer.running)
        with patch("cbpi.api.timer.time.time", return_value=1400):
            self.assertEqual(timer.remaining(), 400)

    async def test_manual_stop_freezes_restored_deadline_before_timer_start(self):
        timer = Timer(3600)
        state = dict(started=True, remaining_seconds=600, end_time=1600, saved_at=1000)
        with patch("cbpi.api.timer.time.time", return_value=1200):
            timer.restore(state, count_downtime=True, keep_deadline=True)
            await timer.stop()
        with patch("cbpi.api.timer.time.time", return_value=1500):
            self.assertEqual(timer.remaining(), 400)
            self.assertIsNone(timer.snapshot()["end_time"])


class StepRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "step_data.json")
        self.cbpi = SimpleNamespace(
            app=SimpleNamespace(on_cleanup=[]),
            config_folder=SimpleNamespace(get_file_path=lambda name: os.path.join(self.directory.name, name)),
            notify=Mock(),
            push_update=Mock(),
            ws=SimpleNamespace(send=Mock()),
            equipment_started=Mock(),
        )
        self.controller = StepController(self.cbpi)
        self.cbpi.step = self.controller
        self.controller.types["TestStep"] = {"class": RecoveryTestStep}

    async def asyncTearDown(self):
        if self.controller._load_failed:
            with self.assertRaises(ValueError):
                await self.controller.shutdown(self.cbpi.app)
        else:
            await self.controller.shutdown(self.cbpi.app)
        await asyncio.sleep(0)

    def write_step(self, mode="Paused", downtime="No", status="A", state=None):
        data = dict(
            basic={"name": "Test brew"},
            steps=[dict(
                id="step1", name="Rest", type="TestStep", status=status,
                props={"Timer": 60, "RecoveryMode": mode, "RecoveryDowntime": downtime},
                timer_state=state,
            )],
        )
        with open(self.path, "w") as file:
            json.dump(data, file)

    def state(self, remaining=600, deadline=1600, started=True):
        return dict(
            started=started, remaining_seconds=remaining,
            end_time=deadline, saved_at=1000,
        )

    async def test_all_four_recovery_combinations(self):
        for mode in ("Paused", "Automatic"):
            for downtime in ("No", "Yes"):
                with self.subTest(mode=mode, downtime=downtime):
                    self.write_step(mode, downtime, state=self.state())
                    self.controller.load()
                    with patch("cbpi.api.timer.time.time", return_value=1200):
                        await self.controller.restore_steps()
                        await asyncio.sleep(0)
                        step = self.controller.profile[0]
                        self.assertEqual(
                            step.status, StepState.ACTIVE if mode == "Automatic" else StepState.STOP,
                        )
                        self.assertEqual(step.instance.timer.remaining(), 400 if downtime == "Yes" else 600)
                        self.assertEqual(step.instance.timer.running, mode == "Automatic")
                        if mode == "Paused":
                            self.cbpi.equipment_started.assert_not_called()
                        await self.controller.shutdown()
                    self.cbpi.equipment_started.reset_mock()

    async def test_manual_pause_overrides_automatic_and_downtime(self):
        self.write_step("Automatic", "Yes", status="S", state=self.state())
        with patch("cbpi.api.timer.time.time", return_value=1200):
            await self.controller.init()
        step = self.controller.profile[0]
        self.assertEqual(step.status, StepState.STOP)
        self.assertEqual(step.instance.timer.remaining(), 600)
        self.cbpi.equipment_started.assert_not_called()

    async def test_paused_deadline_freezes_across_time_and_second_restart(self):
        self.write_step(downtime="Yes", state=self.state())
        with patch("cbpi.api.timer.time.time", return_value=1200):
            await self.controller.init()
        with patch("cbpi.api.timer.time.time", return_value=1500):
            self.assertEqual(self.controller.profile[0].instance.timer.remaining(), 400)
            self.controller.load()
            await self.controller.restore_steps()
            self.assertEqual(self.controller.profile[0].instance.timer.remaining(), 400)
            await self.controller.start()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(self.controller.profile[0].instance.timer.end_time, 1900)

    async def test_expired_automatic_step_never_starts_equipment(self):
        self.write_step("Automatic", "Yes", state=self.state())
        with patch("cbpi.api.timer.time.time", return_value=1700):
            await self.controller.init()
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(self.controller.profile[0].status, StepState.DONE)
            self.cbpi.equipment_started.assert_not_called()
            self.assertIsNone(self.controller.profile[0].timer_state)

    async def test_expired_paused_step_waits_for_user(self):
        self.write_step(downtime="Yes", state=self.state())
        with patch("cbpi.api.timer.time.time", return_value=1700):
            await self.controller.init()
            step = self.controller.profile[0]
            self.assertEqual(step.status, StepState.STOP)
            self.assertEqual(step.instance.timer.remaining(), 0)
            self.assertEqual(step.instance.summary, "00:00:00")
            await self.controller.start()
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(step.status, StepState.DONE)
            self.cbpi.equipment_started.assert_not_called()

    async def test_legacy_and_invalid_checkpoints_restore_paused(self):
        for state in (None, {"started": True}, self.state(remaining=-1)):
            with self.subTest(state=state):
                self.write_step("Automatic", "Yes", state=state)
                self.controller.load()
                await self.controller.restore_steps()
                self.assertEqual(self.controller.profile[0].status, StepState.STOP)
                self.cbpi.equipment_started.assert_not_called()
                self.assertIsNone(self.controller.profile[0].timer_state)
                self.assertTrue(self.cbpi.notify.called)

    async def test_timer_start_adjustment_and_pause_save_immediately(self):
        self.write_step(status="I")
        self.controller.load()
        with patch("cbpi.api.timer.time.time", return_value=1000):
            await self.controller.start()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            with open(self.path) as file:
                self.assertTrue(json.load(file)["steps"][0]["timer_state"]["started"])
            await self.controller.profile[0].instance.timer.add(-300)
            with open(self.path) as file:
                state = json.load(file)["steps"][0]["timer_state"]
                self.assertEqual(state["remaining_seconds"], 3300)
                self.assertEqual(state["end_time"], 4300)
            await self.controller.stop()
            with open(self.path) as file:
                step = json.load(file)["steps"][0]
                self.assertEqual(step["status"], "S")
                self.assertEqual(step["timer_state"]["remaining_seconds"], 3300)
            await self.controller.reset_all()
            with open(self.path) as file:
                self.assertIsNone(json.load(file)["steps"][0]["timer_state"])

    async def test_orderly_shutdown_preserves_active_deadline(self):
        self.write_step("Automatic", "Yes", state=self.state())
        with patch("cbpi.api.timer.time.time", return_value=1200):
            await self.controller.init()
            await asyncio.sleep(0)
            await self.controller.shutdown(self.cbpi.app)
            with open(self.path) as file:
                step = json.load(file)["steps"][0]
                self.assertEqual(step["status"], "A")
                self.assertEqual(step["timer_state"]["end_time"], 1600)
            self.assertFalse(self.controller.profile[0].instance.timer.running)
            self.assertTrue(self.controller._checkpoint_task.done())

    async def test_checkpoint_interval_is_sixty_seconds(self):
        self.write_step(state=self.state())
        self.controller.load()
        calls = []

        async def sleep(seconds):
            calls.append(seconds)
            if len(calls) == 2:
                raise asyncio.CancelledError

        with patch("cbpi.controller.step_controller.asyncio.sleep", side_effect=sleep):
            with patch.object(self.controller, "save", new_callable=AsyncMock) as save:
                with self.assertRaises(asyncio.CancelledError):
                    await self.controller._checkpoint_loop()
                save.assert_awaited_once()
        self.assertEqual(calls, [60, 60])

    async def test_stopping_before_step_task_runs_stays_paused(self):
        self.write_step(status="I")
        self.controller.load()
        await self.controller.start()
        await self.controller.stop()
        await asyncio.sleep(0)
        self.assertEqual(self.controller.profile[0].status, StepState.STOP)
        self.cbpi.equipment_started.assert_not_called()

    async def test_failed_shutdown_checkpoint_still_stops_equipment(self):
        self.write_step("Automatic", state=self.state())
        await self.controller.init()
        await asyncio.sleep(0)
        with patch("cbpi.controller.step_controller.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.controller.shutdown(self.cbpi.app)
        self.assertFalse(self.controller.profile[0].instance.running)
        self.assertFalse(self.controller.profile[0].instance.timer.running)

    async def test_recovery_write_failure_does_not_start_equipment(self):
        self.write_step("Automatic", "Yes", state=self.state())
        self.controller.load()
        with patch("cbpi.controller.step_controller.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.controller.restore_steps()
        self.assertEqual(self.controller.profile[0].status, StepState.STOP)
        self.assertFalse(self.controller.profile[0].instance.timer.running)
        self.cbpi.equipment_started.assert_not_called()

    async def test_atomic_write_failure_preserves_existing_file(self):
        self.write_step(state=self.state())
        self.controller.load()
        with open(self.path, "rb") as file:
            before = file.read()
        with patch("cbpi.controller.step_controller.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.controller.save()
        with open(self.path, "rb") as file:
            self.assertEqual(file.read(), before)
        self.assertEqual(os.listdir(self.directory.name), ["step_data.json"])
        self.assertTrue(self.cbpi.notify.called)

    async def test_invalid_profile_is_not_overwritten(self):
        with open(self.path, "w") as file:
            file.write("{broken")
        with self.assertRaises(ValueError):
            self.controller.load()
        with open(self.path) as file:
            self.assertEqual(file.read(), "{broken")
        with self.assertRaises(ValueError):
            await self.controller.shutdown()
        with open(self.path) as file:
            self.assertEqual(file.read(), "{broken")

    async def test_loading_recipe_and_exporting_strip_runtime_only(self):
        self.write_step(state=self.state())
        self.controller.load()
        recipes = os.path.join(self.directory.name, "recipes")
        os.mkdir(recipes)
        await self.controller.savetobook()
        with open(os.path.join(recipes, os.listdir(recipes)[0])) as file:
            recipe = yaml.safe_load(file)
        self.assertNotIn("timer_state", recipe["steps"][0])
        self.assertEqual(recipe["steps"][0]["props"]["RecoveryMode"], "Paused")
        recipe["steps"][0]["timer_state"] = self.state()
        await self.controller.load_recipe(recipe)
        self.assertEqual(self.controller.profile[0].status, StepState.INITIAL)
        self.assertIsNone(self.controller.profile[0].timer_state)

    async def test_recipe_api_save_strips_execution_state_without_mutating_input(self):
        recipe_path = os.path.join(self.directory.name, "recipe.yaml")
        self.cbpi.config_folder.get_recipe_file_by_id = lambda name: recipe_path
        data = dict(basic={}, steps=[dict(status="A", props={"RecoveryMode": "Automatic"},
                                         timer_state=self.state())])
        await RecipeController(self.cbpi).save("recipe", data)
        with open(recipe_path) as file:
            saved = yaml.safe_load(file)
        self.assertNotIn("timer_state", saved["steps"][0])
        self.assertEqual(saved["steps"][0]["status"], "I")
        self.assertEqual(data["steps"][0]["status"], "A")
        self.assertIn("timer_state", data["steps"][0])

    async def test_paused_edit_preserves_timer_and_active_edit_is_rejected(self):
        self.write_step(state=self.state())
        await self.controller.init()
        edited = Step("step1", "Renamed", Props({"Timer": 60}), "TestStep")
        await self.controller.update(edited)
        self.assertEqual(edited.status, StepState.STOP)
        self.assertEqual(edited.instance.timer.remaining(), 600)
        await self.controller.start()
        with self.assertRaises(ValueError):
            await self.controller.update(edited)

    async def test_built_in_settings_and_waiting_temperature(self):
        for cls in (MashStep, BoilStep, WaitStep, ActorStep):
            labels = [prop.label for prop in cls.cbpi_parameters]
            self.assertIn("RecoveryMode", labels)
            self.assertIn("RecoveryDowntime", labels)
        mash = MashStep(self.cbpi, "mash", "Mash", Props({"Timer": 60}), None)
        with patch("cbpi.api.timer.time.time", return_value=1200):
            mash.restore_timer_state(self.state(3600, None, started=False), True)
        self.assertEqual(mash.timer.remaining(), 3600)
        self.assertFalse(mash.timer.running)
        self.assertEqual(mash.summary, "Waiting for Target Temp")
        await mash.start_timer()
        self.assertFalse(mash.timer.running)

    async def test_boil_preserves_alerts_and_reports_overdue_sixth_hop(self):
        boil = BoilStep(
            self.cbpi, "boil", "Boil",
            Props({"Timer": 60, "Hop_1": 15, "Hop_6": 5, "Hop_6_text": "Aroma"}),
            None,
        )
        state = self.state(600, 1600)
        state["hops_added"] = [True, False, False, False, False, False]
        state["first_wort_hop_flag"] = True
        with patch("cbpi.api.timer.time.time", return_value=1400):
            boil.restore_timer_state(state, True)
        await boil.on_recovery()
        self.assertEqual(boil.get_timer_state()["hops_added"], [True, False, False, False, False, True])
        self.assertTrue(boil.get_timer_state()["first_wort_hop_flag"])
        messages = [call.args for call in self.cbpi.notify.call_args_list]
        self.assertTrue(any("Aroma" in args[1] and args[0] == "Overdue Hop Alert" for args in messages))
        self.cbpi.notify.reset_mock()
        await boil.on_recovery()
        self.cbpi.notify.assert_not_called()

    async def test_mash_and_boil_resume_only_when_temperature_is_reached(self):
        kettle = SimpleNamespace(id="kettle", target_temp=0, instance=None)
        self.cbpi.kettle = SimpleNamespace(find_by_id=lambda id: kettle)
        self.cbpi.config = SimpleNamespace(get=lambda key, default=None: default)
        sensor = {"value": 50}
        self.cbpi.sensor = SimpleNamespace(get_sensor_value=lambda id: sensor)
        for cls in (MashStep, BoilStep):
            with self.subTest(step=cls.__name__):
                step = cls(
                    self.cbpi, "temperature", "Temperature",
                    Props({"Timer": 60, "Temp": 65, "Sensor": "sensor", "Kettle": "kettle"}),
                    None,
                )
                with patch("cbpi.api.timer.time.time", return_value=1200):
                    step.restore_timer_state(self.state(), True)
                    await step.start()
                    await asyncio.sleep(1.1)
                    self.assertFalse(step.timer.running)
                    sensor["value"] = 70
                    await asyncio.sleep(1.1)
                    self.assertTrue(step.timer.running)
                    self.assertEqual(step.timer.remaining(), 400)
                    await step.stop()
                    self.assertFalse(step.timer.running)
                sensor["value"] = 50


if __name__ == "__main__":
    unittest.main()
