"""Nightscout poller — pulls a user's data from an existing Nightscout site.

For people who already run Nightscout in the cloud (or use a service that
feeds one). The documents come back already in the shapes our store speaks,
so this is a thin fetch-and-ingest loop.

The configured key may be either a classic API secret or an access token;
we try each auth style (SHA-1 api-secret header, token query parameter, raw
header) and remember the one the site accepts. A browser-like User-Agent is
required because hosted Nightscout sites often sit behind Cloudflare, which
rejects default urllib requests outright.

Configure per user in config.json:

    "source": {
      "type": "nightscout",
      "url": "https://mysite.example.com",
      "api_secret": "<api secret or access token>",
      "poll_seconds": 60
    }
"""

import hashlib
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from . import synclog
from .sources import BasePoller
from .store import Store, parse_time_ms

log = logging.getLogger("trio_monitor.nspull")

USER_AGENT = "Mozilla/5.0 (X11; Linux aarch64) TrioMonitor/1.0"
ENTRY_COUNT = 72        # 6 hours of 5-minute readings
TREATMENT_COUNT = 50
DEVICESTATUS_COUNT = 12
PROFILE_EVERY_N_POLLS = 15   # therapy settings change rarely


def params_from_profile(docs: list) -> dict:
    """Extract ISF/CR/DIA from a Nightscout /api/v1/profile.json response."""
    if not docs or not isinstance(docs[0], dict):
        return {}
    doc = docs[0]
    profiles = doc.get("store") or {}
    profile = profiles.get(doc.get("defaultProfile")) or next(iter(profiles.values()), {})
    params = {}

    def first_value(key):
        seq = profile.get(key)
        if isinstance(seq, list) and seq:
            return seq[0].get("value")
        return None

    isf = first_value("sens")
    if isf:
        if str(profile.get("units", "")).lower().startswith("mmol") or isf < 20:
            isf = float(isf) * 18.01559
        params["isf"] = float(isf)
    cr = first_value("carbratio")
    if cr:
        params["cr"] = float(cr)
    dia = profile.get("dia")
    if dia:
        params["dia_hours"] = float(dia)
    return params


class NightscoutPoller(BasePoller):
    def __init__(self, user: str, source: dict, store: Store):
        super().__init__("nightscout", user, source.get("poll_seconds", 60), store)
        self.base = source["url"].rstrip("/")
        key = (source.get("api_secret") or source.get("token") or "").strip()
        # Auth styles to attempt, in order; the first that works sticks.
        if key:
            self._modes = [("sha1", key), ("token", key), ("raw", key)]
        else:
            self._modes = [("none", "")]
        self._mode_idx = 0
        self._profile_countdown = 0

    def _request(self, path: str, params: dict, mode: tuple) -> list:
        kind, key = mode
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if kind == "sha1":
            headers["api-secret"] = hashlib.sha1(key.encode()).hexdigest()
        elif kind == "raw":
            headers["api-secret"] = key
        elif kind == "token":
            params = {**params, "token": key}
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data if isinstance(data, list) else []

    def _get(self, path: str, **params) -> list:
        last_error = None
        for offset in range(len(self._modes)):
            idx = (self._mode_idx + offset) % len(self._modes)
            try:
                data = self._request(path, params, self._modes[idx])
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    last_error = exc
                    continue  # wrong auth style; try the next one
                raise
            if idx != self._mode_idx:
                self._mode_idx = idx
                log.info("[%s] nightscout auth style '%s' accepted",
                         self.user, self._modes[idx][0])
            return data
        raise last_error

    def _poll_once(self) -> None:
        entries = self._get("/api/v1/entries/sgv.json", count=ENTRY_COUNT)
        if entries:
            self.store.add_entries(self.user, entries)
        treatments = self._get("/api/v1/treatments.json", count=TREATMENT_COUNT)
        if treatments:
            self.store.add_treatments(self.user, treatments)
        devicestatus = self._get("/api/v1/devicestatus.json", count=DEVICESTATUS_COUNT)
        if devicestatus:
            self.store.add_devicestatus(self.user, devicestatus)
        newest = max((parse_time_ms(e, "date", "dateString") for e in entries),
                     default=None)
        freshness = f", newest {int((time.time() * 1000 - newest) / 60000)}m old" if newest else ""
        synclog.add("nightscout", self.user,
                    f"pulled {len(entries)} readings, {len(treatments)} treatments, "
                    f"{len(devicestatus)} statuses{freshness}")
        if self._profile_countdown <= 0:
            self._profile_countdown = PROFILE_EVERY_N_POLLS
            try:
                params = params_from_profile(self._get("/api/v1/profile.json"))
                if params:
                    self.store.set_params(self.user, params)
                    log.info("[%s] therapy settings from Nightscout profile: %s",
                             self.user, params)
            except Exception as exc:
                log.debug("[%s] profile fetch failed: %s", self.user, exc)
        self._profile_countdown -= 1
