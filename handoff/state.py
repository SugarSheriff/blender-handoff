# SPDX-License-Identifier: GPL-3.0-or-later
"""Session state shared between operators, panels, and the upload worker thread.

bpy is not thread-safe, so the worker never touches it. It only writes here, under a
lock, and a bpy.app.timers callback on the main thread redraws the panel.
"""

import threading
import time
from collections import deque

findings = []
preflight_ran = False


class PublishState:
    def __init__(self):
        self._lock = threading.Lock()
        self._lines = deque(maxlen=40)
        self.status = "idle"  # idle | running | done | failed | cancelled
        self.result = None
        self.cancel = threading.Event()

    @property
    def running(self):
        return self.status == "running"

    def start(self):
        with self._lock:
            self._lines.clear()
            self.status = "running"
            self.result = None
            self.cancel = threading.Event()

    def log(self, message):
        with self._lock:
            self._lines.append(f"{time.strftime('%H:%M:%S')}  {message}")

    def finish(self, status, result=None):
        with self._lock:
            self.status = status
            self.result = result

    def lines(self):
        with self._lock:
            return list(self._lines)


publish = PublishState()
