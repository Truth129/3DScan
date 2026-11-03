"""Requests in flight, in all and per case, held in memory.

A purge waits for its own case, and the shared-cache steps wait for everything, so neither removes a
file a request is reading. A restart starts from nothing, so a stuck count never makes a case
undeletable for good.
"""

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
import threading

# The cases the current request is making. A sync handler runs in a copy of the gate's context, so the list is shared.
_HELD = ContextVar("scan3d_held_cases", default=None)


class Activity:
    def __init__(self):
        self._changed = threading.Condition()
        self._total = 0
        self._cases = Counter()
        self._quiet = False

    def enter(self, case_id=None, *, wait=True):
        """Count a request. While a maintenance step runs, a new request waits here, for seconds at most.

        `wait=False` is for work started inside a counted request, which no maintenance step can be holding off.
        """
        with self._changed:
            if wait:
                self._changed.wait_for(lambda: not self._quiet)
            self._total += 1
            if case_id:
                self._cases[case_id] += 1

    def leave(self, case_id=None):
        with self._changed:
            self._total -= 1
            if case_id:
                self._cases[case_id] -= 1
                if not self._cases[case_id]:
                    del self._cases[case_id]

    def busy(self, case_id):
        with self._changed:
            return case_id in self._cases

    @contextmanager
    def quiet(self, own=0):
        """Hold new requests back while no other is in flight. Yields False, holding nothing, if one is.

        `own` is how many of the counted requests belong to the caller, such as the Clear caches request itself.
        """
        with self._changed:
            idle = self._total <= own and not self._quiet
            self._quiet = self._quiet or idle
        try:
            yield idle
        finally:
            if idle:
                with self._changed:
                    self._quiet = False
                    self._changed.notify_all()

    @contextmanager
    def request(self):
        """Where `hold` records the cases a request makes. They count until the request ends."""
        held = []
        token = _HELD.set(held)
        try:
            yield
        finally:
            _HELD.reset(token)
            for case_id in held:
                self.leave(case_id)

    def hold(self, case_id):
        """Count a case a request is making until that request ends. On the command line nothing counts."""
        held = _HELD.get()
        if held is not None:
            self.enter(case_id, wait=False)
            held.append(case_id)
