"""Glucose forecasting for the display.

Preferred source: the AID system's own prediction curve, which arrives in
the devicestatus documents we already store — oref-family systems (Trio,
AAPS) upload ``openaps.suggested.predBGs`` and Loop-family systems (twiist)
upload ``loop.predicted``. Those models know the person's insulin activity
and carb absorption, so we surface them rather than reinvent them.

Fallback: when no fresh device prediction exists, we run our own oref0-style
forecast (see oref.py) from bolus history, pump-reported IOB/COB, glucose
deviations, and the person's therapy settings pulled from their Nightscout
profile or Tidepool pump settings. Marked with "~" in the UI — it is a
display hint, not medicine.
"""

import time

from . import oref
from .store import UserSnapshot, parse_time_ms

HORIZONS = (30, 60, 90, 120)          # minutes
STEP_MS = 5 * 60 * 1000               # AID predictions are 5-minute series
MAX_PREDICTION_AGE_MS = 15 * 60 * 1000


def device_series(raw: dict) -> tuple[int, list[float]] | None:
    """Extract (start_ms, values[]) from a devicestatus document, if present."""
    if not isinstance(raw, dict):
        return None
    loop = raw.get("loop") or {}
    predicted = loop.get("predicted") or {}
    if isinstance(predicted, dict) and predicted.get("values"):
        return parse_time_ms(predicted, "startDate"), list(predicted["values"])
    suggested = (raw.get("openaps") or {}).get("suggested") or {}
    pred_bgs = suggested.get("predBGs") or {}
    candidates = [
        list(pred_bgs[key])
        for key in ("COB", "UAM", "IOB", "ZT")
        if pred_bgs.get(key)
    ]
    if not candidates:
        return None
    # oref uploads several scenario curves (carb-aware, unannounced-meal,
    # insulin-only, zero-temp). The pump's own headline outcome is
    # eventualBG — show the curve that ends closest to it, rather than a
    # worst-case scenario pinned at the 39 mg/dL clamp.
    eventual = suggested.get("eventualBG")
    if isinstance(eventual, (int, float)):
        values = min(candidates, key=lambda c: abs(c[-1] - eventual))
    else:
        values = candidates[0]
    start = parse_time_ms(suggested, "timestamp", "deliverAt")
    return start, values


def _oref_series(snap: UserSnapshot, now_ms: int) -> tuple[int, list[float]]:
    values, _curve = oref.predict(
        sgv=snap.sgv,
        history=snap.history,
        boluses=snap.boluses,
        pump_iob=snap.iob,
        cob=snap.cob,
        params=snap.params,
        now_ms=now_ms,
        steps=HORIZONS[-1] // 5,
    )
    # Prepend the current reading so index 0 sits at start_ms, matching the
    # device-series convention used by the horizon/timeline indexing.
    return now_ms, [snap.sgv] + values


def predict(
    snap: UserSnapshot, now_ms: int | None = None
) -> tuple[dict[int, float], list[tuple[int, float]], str] | tuple[None, None, None]:
    """Return ({horizon_min: mg/dL}, [(ms, mg/dL) 5-min series], source).

    source is "device" (the pump's own forecast) or "est" (our fallback).
    Returns (None, None, None) when there is nothing sane to predict from.
    """
    now_ms = now_ms or int(time.time() * 1000)

    series = None
    source = "device"
    if (
        snap.status_raw
        and snap.status_date
        and now_ms - snap.status_date <= MAX_PREDICTION_AGE_MS
    ):
        series = device_series(snap.status_raw)

    if series is None:
        if snap.sgv is None or snap.sgv_date is None:
            return None, None, None
        if now_ms - snap.sgv_date > MAX_PREDICTION_AGE_MS:
            return None, None, None
        series = _oref_series(snap, now_ms)
        source = "est"

    start_ms, values = series
    if not values:
        return None, None, None

    horizons = {}
    for h in HORIZONS:
        idx = round((now_ms + h * 60 * 1000 - start_ms) / STEP_MS)
        if idx < 0:
            continue
        horizons[h] = float(values[min(idx, len(values) - 1)])

    timeline = [
        (start_ms + i * STEP_MS, float(v))
        for i, v in enumerate(values)
        if start_ms + i * STEP_MS > now_ms
        and start_ms + i * STEP_MS <= now_ms + HORIZONS[-1] * 60 * 1000
    ]
    return (horizons or None) and horizons, timeline, source
