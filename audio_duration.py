"""Read-only selected-audio metadata cache and coalesced background requests."""
from collections import OrderedDict
import os
from pathlib import Path
import re
import stat
import threading
import time

UNKNOWN = '--:--:--'
DISPLAY = re.compile(r'\d{2,}:[0-5]\d:[0-5]\d')


def probe_duration(audio):
    # Reuse configured external ffprobe, bounded probing, 2s timeout and no window.
    from studio import quick_duration
    return quick_duration(audio)


class DurationCache:
    def __init__(self, probe=None, max_entries=256, failure_ttl=30):
        self.probe = probe or probe_duration
        self.max_entries, self.failure_ttl = max_entries, failure_ttl
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    @staticmethod
    def fingerprint(audio):
        # Normalize without opening another handle solely to resolve final paths.
        path = Path(os.path.abspath(os.fspath(audio)))
        info = path.stat()
        if not stat.S_ISREG(info.st_mode): raise OSError('Not a regular audio file')
        return (os.path.normcase(str(path)), info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)

    def read(self, audio):
        try:
            key = self.fingerprint(audio)
            with self.lock:
                cached = self.entries.get(key)
                if cached and (cached[1] is None or time.monotonic() < cached[1]):
                    self.entries.move_to_end(key)
                    return cached[0]
            try:
                value = self.probe(os.path.abspath(os.fspath(audio)))
            except Exception:
                value = UNKNOWN
            if not isinstance(value, str) or not DISPLAY.fullmatch(value): value = UNKNOWN
            if self.fingerprint(audio) != key: return UNKNOWN
            expires = time.monotonic() + self.failure_ttl if value == UNKNOWN else None
            with self.lock:
                for old in list(self.entries):
                    if old[0] == key[0]: del self.entries[old]
                self.entries[key] = value, expires
                while len(self.entries) > self.max_entries: self.entries.popitem(last=False)
            return value
        except (OSError, ValueError, TypeError):
            return UNKNOWN


class DurationReader:
    """One probe at a time; pending selections replace each other, no Tk calls."""
    def __init__(self, results, cache=None):
        self.results, self.cache = results, cache or DurationCache()
        self.condition = threading.Condition()
        self.version = 0; self.pending = None; self.closed = False
        self.started = False
        # Finish any bounded probe before process exit; no orphan ffprobe on close.
        self.thread = threading.Thread(target=self._work, name='EarlySakura duration', daemon=False)

    def request(self, work_id, audio):
        with self.condition:
            self.version += 1
            if not self.closed:
                self.pending = (self.version, work_id, audio) if work_id is not None else None
                if work_id is not None and not self.started:
                    self.started = True; self.thread.start()
                self.condition.notify()
            return self.version

    def _work(self):
        while True:
            with self.condition:
                while self.pending is None and not self.closed: self.condition.wait()
                if self.closed: return
                request, self.pending = self.pending, None
            value = self.cache.read(request[2])
            with self.condition:
                if self.closed: return
                self.results.put((*request, value))

    def close(self):
        with self.condition:
            self.closed = True; self.pending = None; self.condition.notify()
