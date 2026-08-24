"""In-memory sync activity log, shown at /log in the web UI.

Every data-source event (push received, poll completed, poll failed) lands
here so the user can see at a glance whether each source is flowing.
Ring buffer only — restarts clear it, which is fine for a health view.
"""

import collections
import threading
import time

_lock = threading.Lock()
_entries: collections.deque = collections.deque(maxlen=400)


def add(source: str, user: str, message: str, ok: bool = True) -> None:
    with _lock:
        _entries.appendleft({
            "ts": int(time.time() * 1000),
            "source": source,
            "user": user,
            "message": message,
            "ok": ok,
        })


def recent(limit: int = 250) -> list[dict]:
    with _lock:
        return list(_entries)[:limit]
