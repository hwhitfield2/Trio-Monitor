"""Pull-based data sources: shared poller machinery and dispatch.

Users whose device pushes Nightscout payloads at us need nothing here.
Users on pumps/services we must poll configure a "source" in config.json;
start_pollers() spawns the right poller thread per user.
"""

import logging
import threading
import urllib.error

from . import synclog
from .store import Store

log = logging.getLogger("trio_monitor.sources")

ERROR_BACKOFF_SECONDS = 300


class BasePoller(threading.Thread):
    """Poll loop with error backoff; subclasses implement _poll_once()."""

    def __init__(self, kind: str, user: str, poll_seconds: int, store: Store):
        super().__init__(name=f"{kind}-{user}", daemon=True)
        self.kind = kind
        self.user = user
        self.poll_seconds = max(30, int(poll_seconds))
        self.store = store
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _poll_once(self) -> None:
        raise NotImplementedError

    def run(self) -> None:
        log.info("[%s] %s poller started (every %ds)",
                 self.user, self.kind, self.poll_seconds)
        while not self._stop.is_set():
            delay = self.poll_seconds
            try:
                self._poll_once()
            except Exception as exc:
                # Back off hard on auth errors so we never lock an account out.
                auth_error = (
                    isinstance(exc, urllib.error.HTTPError)
                    and exc.code in (401, 403)
                )
                delay = ERROR_BACKOFF_SECONDS if auth_error else min(
                    ERROR_BACKOFF_SECONDS, self.poll_seconds * 3
                )
                log.warning("[%s] %s poll failed: %s (retry in %ds)",
                            self.user, self.kind, exc, delay)
                synclog.add(self.kind, self.user,
                            f"poll failed: {exc} (retry in {delay}s)", ok=False)
            self._stop.wait(delay)


def start_pollers(users, store: Store) -> list[BasePoller]:
    from .nspull import NightscoutPoller
    from .tidepool import TidepoolPoller

    pollers = []
    for user in users:
        source = user.source or {}
        kind = source.get("type")
        poller = None
        if kind == "tidepool" and source.get("email") and source.get("password"):
            poller = TidepoolPoller(user.name, source, store)
        elif kind == "nightscout" and source.get("url"):
            poller = NightscoutPoller(user.name, source, store)
        elif kind in ("tidepool", "nightscout"):
            log.warning("[%s] %s source is missing credentials/url; not polling",
                        user.name, kind)
        if poller:
            poller.start()
            pollers.append(poller)
    return pollers
