"""oref0-style glucose prediction (openaps `determine-basal` flavor).

Implements the core forecasting math of oref0 for display purposes:

- Exponential insulin-activity model (oref0 lib/iob/calculate.js): each bolus
  contributes activity over the insulin duration with a configurable peak.
- Blood-glucose impact (BGI): -activity * ISF per 5-minute step.
- Deviations: how much recent BG movement differs from what insulin alone
  explains — oref's signal for carb absorption / unannounced meals.
- Prediction arrays: IOBpredBG (insulin only), COBpredBG (carb absorption
  decaying linearly, total area = COB * CSF), UAMpredBG (deviation decaying
  over 60 minutes) — clamped to oref's 39..401 display range.

Inputs come from what a monitor can see: bolus history, pump-reported
IOB/COB, and therapy settings (ISF/CR/DIA) pulled from the user's Nightscout
profile or Tidepool pump settings, with defaults when unknown. Computed IOB
from known boluses is rescaled to match the pump's reported IOB, which
absorbs what we can't see (basal modulation, micro-boluses).

This is for display only — it informs a wall monitor, not dosing.
"""

import math
from dataclasses import dataclass

MIN_5M_CARBIMPACT = 8.0     # oref0 default: mg/dL per 5m assumed while COB>0
UAM_DECAY_STEPS = 12        # unannounced-meal deviation decays over 60 min
CLAMP_LO, CLAMP_HI = 39, 401
STEP_MIN = 5.0


@dataclass
class Therapy:
    isf: float = 50.0        # mg/dL per U
    cr: float = 10.0         # g per U
    dia_hours: float = 6.0
    peak_min: float = 75.0   # rapid-acting default (oref0 exponential model)


def therapy_from_params(params: dict | None) -> Therapy:
    """Build therapy settings, accepting only physiologically plausible
    values — profile endpoints sometimes carry placeholder junk (e.g. Trio
    uploads a dummy Nightscout profile with sens=720, carbratio=200)."""
    params = params or {}
    t = Therapy()

    def plausible(key, lo, hi):
        value = params.get(key)
        return float(value) if value and lo <= value <= hi else None

    t.isf = plausible("isf", 10, 400) or t.isf
    t.cr = plausible("cr", 2, 50) or t.cr
    t.dia_hours = plausible("dia_hours", 3, 10) or t.dia_hours
    t.peak_min = plausible("peak_min", 30, 120) or t.peak_min
    return t


def insulin_model(td_min: float, tp_min: float):
    """oref0 exponential insulin curves. Returns (activity(t,u), iob_frac(t)).

    activity is per-minute glucose-lowering activity for a bolus of u units
    at age t minutes; iob_frac is the fraction of a bolus still active.
    """
    td, tp = td_min, tp_min
    tau = tp * (1 - tp / td) / (1 - 2 * tp / td)
    a = 2 * tau / td
    s = 1 / (1 - a + (1 + a) * math.exp(-td / tau))

    def activity(t: float, u: float) -> float:
        if t <= 0 or t >= td:
            return 0.0
        return u * (s / tau**2) * t * (1 - t / td) * math.exp(-t / tau)

    def iob_frac(t: float) -> float:
        if t <= 0:
            return 1.0
        if t >= td:
            return 0.0
        return 1 - s * (1 - a) * (
            (t**2 / (tau * td * (1 - a)) - t / tau - 1) * math.exp(-t / tau) + 1
        )

    return activity, iob_frac


def predict(
    sgv: float,
    history: list[tuple[int, float]],
    boluses: list[tuple[int, float]],
    pump_iob: float | None,
    cob: float | None,
    params: dict | None,
    now_ms: int,
    steps: int = 24,
) -> tuple[list[float], str]:
    """Return (values, curve_name) — 5-min predBGs starting at now+5m.

    curve_name is which oref array was chosen for display:
    "COB", "UAM", or "IOB".
    """
    therapy = therapy_from_params(params)
    td = therapy.dia_hours * 60
    activity_fn, iob_frac = insulin_model(td, therapy.peak_min)

    # --- effective bolus list, rescaled to the pump's reported IOB ---
    known = [
        ((now_ms - t) / 60000.0, u)
        for t, u in boluses
        if 0 <= (now_ms - t) / 60000.0 < td and u > 0
    ]
    computed_iob = sum(u * iob_frac(age) for age, u in known)
    scale = 1.0
    if pump_iob is not None:
        if computed_iob > 0.1:
            scale = max(0.25, min(4.0, pump_iob / computed_iob))
        else:
            # No visible boluses: model the reported IOB as one synthetic
            # bolus about an hour old (mid-decay). Works for negative IOB
            # too, which then correctly pushes predictions upward.
            known = [(60.0, pump_iob / max(iob_frac(60.0), 0.05))]

    def activity_at(minutes_ahead: float) -> float:
        return scale * sum(
            activity_fn(age + minutes_ahead, u) for age, u in known
        )

    # --- deviations: actual BG movement minus insulin-explained movement ---
    recent = [(t, v) for t, v in history if now_ms - t <= 45 * 60 * 1000]
    deviations = []
    for (t0, v0), (t1, v1) in zip(recent, recent[1:]):
        gap_min = (t1 - t0) / 60000.0
        if not 2 <= gap_min <= 12:
            continue
        age_mid = (now_ms - (t0 + t1) / 2) / 60000.0
        expected = -scale * sum(
            activity_fn(age - age_mid, u) for age, u in known
        ) * therapy.isf * gap_min
        actual = v1 - v0
        deviations.append((actual - expected) * (STEP_MIN / gap_min))
    avg_dev = sum(deviations) / len(deviations) if deviations else 0.0

    # --- carb impact (oref: deviation-driven, floored while COB remains) ---
    cob = max(0.0, cob or 0.0)
    csf = therapy.isf / therapy.cr           # mg/dL per gram
    ci = max(avg_dev, MIN_5M_CARBIMPACT if cob > 0 else 0.0)
    # Linear decay duration so the area under predCI equals COB * CSF.
    cob_steps = (2 * cob * csf / ci) / STEP_MIN if (cob > 0 and ci > 0) else 0.0

    iob_pred, cob_pred, uam_pred = [sgv], [sgv], [sgv]
    for i in range(steps):
        bgi = -activity_at((i + 0.5) * STEP_MIN) * therapy.isf * STEP_MIN
        pred_ci = ci * max(0.0, 1 - i / cob_steps) if cob_steps > 0 else 0.0
        uam_ci = avg_dev * max(0.0, 1 - i / UAM_DECAY_STEPS)
        clamp = lambda v: max(CLAMP_LO, min(CLAMP_HI, v))
        iob_pred.append(clamp(iob_pred[-1] + bgi))
        cob_pred.append(clamp(cob_pred[-1] + bgi + pred_ci))
        uam_pred.append(clamp(uam_pred[-1] + bgi + uam_ci))

    if cob > 0:
        return cob_pred[1:], "COB"
    if abs(avg_dev) > 2:
        return uam_pred[1:], "UAM"
    return iob_pred[1:], "IOB"
