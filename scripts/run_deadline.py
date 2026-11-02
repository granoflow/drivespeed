"""One monotonic deadline shared by every job in a test, independent of polling."""
import threading
import time

from system_io import BenchError, stop_process


class RunDeadline:
    def __init__(self, seconds):
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            raise ValueError("A positive total test deadline is required")
        self.seconds = seconds
        self.started = time.monotonic()
        self.expired = threading.Event()
        self.lock = threading.Lock()
        self.child = None
        self.timer = threading.Timer(seconds, self._expire)
        self.timer.daemon = True
        self.timer.start()

    def _expire(self):
        with self.lock:
            self.expired.set()
            child = self.child
        # Stop only this run's registered fio process/group, even if the main
        # thread is blocked inside a sensor query. No global process search.
        if child is not None:
            stop_process(child)

    def check(self):
        if self.expired.is_set() or time.monotonic() - self.started >= self.seconds:
            raise BenchError("total_timeout", "The total test time limit was reached", 6)

    def attach(self, child):
        with self.lock:
            expired = self.expired.is_set()
            if not expired:
                self.child = child
        if expired:
            stop_process(child)
        self.check()

    def detach(self, child):
        with self.lock:
            if self.child is child:
                self.child = None

    def close(self):
        self.timer.cancel()
        self.timer.join()
