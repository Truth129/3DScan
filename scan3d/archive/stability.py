"""Whether a series has stopped arriving, for archives that do not say.

DICOMweb has no "still receiving" flag. A series counts as stable once this process has seen
the same instance list for a quiet period, like Orthanc's StableAge. A restart forgets every
sighting, so each series waits again: the safe direction.
"""

from collections import OrderedDict
import threading
import time


class QuietPeriod:
    def __init__(self, seconds: float, *, clock=time.monotonic, limit: int = 4096):
        self.seconds = seconds
        self.clock = clock
        self.limit = limit
        self._seen: OrderedDict[tuple, tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def observe(self, key: tuple, digest: str) -> float:
        """Seconds until this series counts as stable; 0 means it already does. A changed list starts over."""
        now = self.clock()
        with self._lock:
            previous = self._seen.pop(key, None)
            since = previous[1] if previous and previous[0] == digest else now
            self._seen[key] = (digest, since)
            # Forget the series looked at least recently, which only makes it wait again.
            while len(self._seen) > self.limit:
                self._seen.popitem(last=False)
        return max(0.0, self.seconds - (now - since))
