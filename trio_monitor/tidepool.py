"""Tidepool poller — pulls data for pumps that upload to Tidepool (e.g. twiist).

The twiist AID system is "powered by Tidepool": the pump's phone app uploads
CGM readings, boluses, carbs, and dosing decisions (IOB/COB) to the wearer's
Tidepool account automatically. This module logs in with that account, polls
the data API, converts the documents to the Nightscout-style shapes the rest
of the app already understands, and feeds them into the shared store.

Configure per user in config.json:

    "source": {
      "type": "tidepool",
      "email": "cassidy@example.com",
      "password": "...",
      "poll_seconds": 60
    }
"""

import base64
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from . import synclog
from .sources import BasePoller
from .store import Store, parse_time_ms

log = logging.getLogger("trio_monitor.tidepool")

API_BASE = "https://api.tidepool.org"
MGDL_PER_MMOLL = 18.01559
FETCH_WINDOW_HOURS = 6


def to_mgdl(value: float, units: str | None) -> float:
    if units and units.lower().startswith("mmol"):
        return value * MGDL_PER_MMOLL
    return value


def direction_from_rate(rate_per_5min: float | None) -> str | None:
    """Map a mg/dL-per-5-minutes slope onto Nightscout trend-arrow names."""
    if rate_per_5min is None:
        return None
    r = rate_per_5min
    if r > 17:
        return "DoubleUp"
    if r > 10:
        return "SingleUp"
    if r > 5:
        return "FortyFiveUp"
    if r < -17:
        return "DoubleDown"
    if r < -10:
        return "SingleDown"
    if r < -5:
        return "FortyFiveDown"
    return "Flat"


def transform(docs: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Convert Tidepool documents to (entries, treatments, devicestatus)."""
    entries, treatments, devicestatus = [], [], []

    cbg = sorted(
        (d for d in docs if d.get("type") == "cbg" and d.get("value") is not None),
        key=lambda d: parse_time_ms(d, "time"),
    )
    prev_ms = prev_mgdl = None
    for doc in cbg:
        ms = parse_time_ms(doc, "time")
        mgdl = to_mgdl(float(doc["value"]), doc.get("units"))
        rate = None
        if prev_ms is not None and 0 < ms - prev_ms <= 15 * 60 * 1000:
            rate = (mgdl - prev_mgdl) / ((ms - prev_ms) / (5 * 60 * 1000))
        entries.append(
            {
                "type": "sgv",
                "sgv": round(mgdl),
                "date": ms,
                "dateString": doc.get("time"),
                "direction": direction_from_rate(rate),
                "device": "tidepool",
            }
        )
        prev_ms, prev_mgdl = ms, mgdl

    for doc in docs:
        dtype = doc.get("type")
        doc_id = doc.get("id") or doc.get("guid")
        if dtype == "bolus":
            insulin = (doc.get("normal") or 0) + (doc.get("extended") or 0)
            if insulin > 0:
                treatments.append(
                    {
                        "_id": doc_id,
                        "eventType": "Bolus",
                        "insulin": insulin,
                        "created_at": doc.get("time"),
                    }
                )
        elif dtype == "food":
            carbs = ((doc.get("nutrition") or {}).get("carbohydrate") or {}).get("net")
            if carbs:
                treatments.append(
                    {
                        "_id": doc_id,
                        "eventType": "Carb Correction",
                        "carbs": carbs,
                        "created_at": doc.get("time"),
                    }
                )
        elif dtype == "dosingDecision":
            iob = (doc.get("insulinOnBoard") or {}).get("amount")
            cob = (
                (doc.get("carbsOnBoard") or {}).get("amount")
                if isinstance(doc.get("carbsOnBoard"), dict)
                else None
            )
            if cob is None:
                cob = (doc.get("carbohydratesOnBoard") or {}).get("amount")
            food = ((doc.get("food") or {}).get("nutrition") or {}).get("carbohydrate")
            if cob is None and isinstance(food, dict):
                cob = food.get("net")
            if iob is not None or cob is not None:
                devicestatus.append(
                    {
                        "device": "tidepool",
                        "created_at": doc.get("time"),
                        "openaps": {"suggested": {"IOB": iob, "COB": cob}},
                    }
                )

    return entries, treatments, devicestatus


def params_from_pumpsettings(docs: list[dict]) -> dict:
    """Extract ISF/CR from the newest Tidepool pumpSettings document."""
    settings = [d for d in docs if d.get("type") == "pumpSettings"]
    if not settings:
        return {}
    doc = max(settings, key=lambda d: parse_time_ms(d, "time"))

    def first_amount(singular: str, plural: str):
        value = doc.get(singular)
        if isinstance(value, list) and value:
            return value[0].get("amount")
        value = doc.get(plural)
        if isinstance(value, dict) and value:
            schedule = next(iter(value.values()), None)
            if isinstance(schedule, list) and schedule:
                return schedule[0].get("amount")
        return None

    params = {}
    isf = first_amount("insulinSensitivity", "insulinSensitivities")
    if isf:
        # Tidepool normalizes glucose to mmol/L
        params["isf"] = float(isf) * (MGDL_PER_MMOLL if isf < 20 else 1)
    cr = first_amount("carbRatio", "carbRatios")
    if cr:
        params["cr"] = float(cr)
    return params


class TidepoolPoller(BasePoller):
    def __init__(self, user: str, source: dict, store: Store):
        super().__init__("tidepool", user, source.get("poll_seconds", 60), store)
        self.email = source["email"]
        self.password = source["password"]
        self._token: str | None = None
        self._userid: str | None = None
        self._settings_countdown = 0

    # ---- HTTP ----

    def _login(self) -> None:
        creds = base64.b64encode(f"{self.email}:{self.password}".encode()).decode()
        req = urllib.request.Request(
            f"{API_BASE}/auth/login",
            method="POST",
            headers={"Authorization": f"Basic {creds}"},
            data=b"",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            self._token = resp.headers.get("x-tidepool-session-token")
            self._userid = json.loads(resp.read()).get("userid")
        if not self._token or not self._userid:
            raise RuntimeError("Tidepool login gave no session token/userid")
        log.info("[%s] logged in to Tidepool as %s", self.user, self.email)

    def _fetch(self) -> list[dict]:
        start = (
            datetime.now(timezone.utc) - timedelta(hours=FETCH_WINDOW_HOURS)
        ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        url = (
            f"{API_BASE}/data/{self._userid}"
            f"?type=cbg,bolus,food,dosingDecision&startDate={start}"
        )
        req = urllib.request.Request(
            url, headers={"x-tidepool-session-token": self._token}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())

    # ---- main loop ----

    def _poll_once(self) -> None:
        if not self._token:
            self._login()
        try:
            docs = self._fetch()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                self._token = None  # session expired; re-login next round
            raise
        entries, treatments, devicestatus = transform(docs)
        if entries:
            self.store.add_entries(self.user, entries)
        if treatments:
            self.store.add_treatments(self.user, treatments)
        if devicestatus:
            self.store.add_devicestatus(self.user, devicestatus)
        if self._settings_countdown <= 0:
            self._settings_countdown = 15  # therapy settings change rarely
            try:
                url = f"{API_BASE}/data/{self._userid}?type=pumpSettings&latest=true"
                req = urllib.request.Request(
                    url, headers={"x-tidepool-session-token": self._token}
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    settings_docs = json.loads(resp.read())
                params = params_from_pumpsettings(settings_docs or [])
                if params:
                    self.store.set_params(self.user, params)
                    log.info("[%s] therapy settings from Tidepool: %s",
                             self.user, params)
            except Exception as exc:
                log.debug("[%s] pumpSettings fetch failed: %s", self.user, exc)
        self._settings_countdown -= 1
        log.debug(
            "[%s] tidepool poll: %d entries, %d treatments, %d devicestatus",
            self.user, len(entries), len(treatments), len(devicestatus),
        )
        newest = max((e["date"] for e in entries), default=None)
        import time as _time
        freshness = (f", newest {int((_time.time() * 1000 - newest) / 60000)}m old"
                     if newest else "")
        synclog.add("tidepool", self.user,
                    f"pulled {len(entries)} readings, {len(treatments)} treatments, "
                    f"{len(devicestatus)} statuses{freshness}")
