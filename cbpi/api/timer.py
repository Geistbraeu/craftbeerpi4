import asyncio
import math
import time
import logging


class Timer(object):

    def __init__(self, timeout, on_done=None, on_update=None, on_change=None) -> None:
        super().__init__()
        self.timeout = timeout
        self._timemout = self.timeout
        self._task = None
        self._callback = on_done
        self._update = on_update
        self.start_time = None
        self.end_time = None
        self.started = False
        self._on_change = on_change
        self._restored_end_time = None

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    def remaining(self):
        if self._restored_end_time is not None:
            return max(0, int(self._restored_end_time) - int(time.time()))
        if self.running and self.end_time is not None:
            return max(0, self.end_time - int(time.time()))
        return max(0, int(round(self._timemout)))

    def snapshot(self):
        return dict(
            started=self.started,
            remaining_seconds=self.remaining(),
            end_time=self.end_time if self.running else self._restored_end_time,
            saved_at=int(time.time()),
        )

    def restore(self, state, count_downtime=False, keep_deadline=False):
        if self.running:
            raise RuntimeError("Cannot restore a running timer")
        self._restored_end_time = None
        self.end_time = None
        self.started = state["started"]
        self._timemout = state["remaining_seconds"]
        deadline = state.get("end_time")
        if count_downtime and self.started and deadline is not None:
            self._timemout = max(0, deadline - int(time.time()))
            if keep_deadline:
                self._restored_end_time = deadline

    async def changed(self):
        if self._on_change is not None:
            try:
                await self._on_change()
            except OSError:
                logging.exception("Timer checkpoint failed; countdown continues")

    def done(self, task):
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logging.error("Timer failed", exc_info=(type(error), error, error.__traceback__))
            return
        if self._callback is not None:
            asyncio.create_task(self._callback(self))

    async def _job(self):
        self.count = max(0, self.end_time - self.start_time)
        try:
            await self.changed()
            while self.count > 0:
                self.count = max(0, self.end_time - int(time.time()))
                if self._update is not None:
                    await self._update(self, self.count)
                if self.count > 0:
                    await asyncio.sleep(1)
        finally:
            self._timemout = max(0, self.end_time - int(time.time()))

    async def add(self, seconds):
        if self.running and self.end_time is not None:
            self.end_time = max(int(time.time()), self.end_time + seconds)
        else:
            self._timemout = max(0, self._timemout + seconds)
            if self._restored_end_time is not None:
                self._restored_end_time += seconds
        await self.changed()

    async def set_remaining(self, seconds):
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
            raise ValueError("Remaining time must be a non-negative integer in seconds")
        if self.running:
            self.end_time = int(time.time()) + seconds
        else:
            self._timemout = seconds
            if self._restored_end_time is not None:
                self._restored_end_time = int(time.time()) + seconds
        await self.changed()

    def start(self):
        if self.running:
            return
        self.start_time = int(time.time())
        self.end_time = (
            self._restored_end_time
            if self._restored_end_time is not None
            else self.start_time + int(round(self._timemout, 0))
        )
        self._restored_end_time = None
        self.started = True
        self._task = asyncio.create_task(self._job())
        self._task.add_done_callback(self.done)

    async def stop(self):
        if self._restored_end_time is not None:
            self._timemout = self.remaining()
            self._restored_end_time = None
        if self._task and self._task.done() is False:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._timemout = max(0, self.end_time - int(time.time()))

    def reset(self):
        if self.running:
            return
        self._timemout = self.timeout
        self.started = False
        self._restored_end_time = None

    def is_running(self):
        return self.running

    def set_time(self, timeout):
        if self.running:
            return
        self.timeout = timeout
        self._timemout = timeout

    def get_time(self):
        return self.format_time(self.remaining())

    @classmethod
    def format_time(cls, time):
        time = max(0, time)
        pattern_h = "{0:02d}:{1:02d}:{2:02d}"
        pattern_d = "{0:02d}D {1:02d}:{2:02d}:{3:02d}"
        seconds = time % 60
        minutes = math.floor(time / 60) % 60
        hours = math.floor(time / 3600) % 24
        days = math.floor(time / 86400)
        if days != 0:
            remaining_time = pattern_d.format(days, hours, minutes, seconds)
        else:
            remaining_time = pattern_h.format(hours, minutes, seconds)
        return remaining_time
