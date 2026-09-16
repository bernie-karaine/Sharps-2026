"""Sharp-money signal detection and scoring.

Implements the five signals and scoring model exactly as specified by the
user from their prior build:

  1. Limit movement       - % drop in maxRiskStake vs the opening snapshot
  2. AH line shift        - points delta on the main Asian Handicap line
  3. 1X2 fair-prob move   - cumulative pp change in de-vigged 1X2 probs
                            from the true opening line
  4. AH fair-prob move    - same idea on the AH market (confirmation)
  5. Cross-market convergence - all of the above agreeing on one side

IMPORTANT - calibration disclaimer: the *qualitative* thresholds below
(0.25/0.5/1.0 points, 0.30/0.80/1.80pp, 30%/50% limit drop) come directly
from the user's spec. The *numeric score values* each threshold maps to
(e.g. "a 1.0pt AH shift = 5.0 score") are this implementation's own
calibration bridging those qualitative thresholds into the 0-10 scoring
model - they are not empirically derived from real data (none exists yet
for this tool). All of them live in the CONFIG dict below so they can be
retuned once real rounds of data come in, without touching the logic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

CONFIG = {
    "ah_shift_syndicate": 1.0,
    "ah_shift_sharp": 0.5,
    "ah_shift_threshold": 0.25,  # below this, not attributed as a signal at all
    "ah_score_syndicate": 5.0,
    "ah_score_sharp": 3.5,
    "ah_score_threshold": 2.0,
    "x2_strong_pp": 1.80,
    "x2_meaningful_pp": 0.80,
    "x2_noise_floor_pp": 0.30,
    "x2_score_strong": 5.0,
    "x2_score_meaningful": 3.0,
    "x2_score_noise_floor": 1.5,
    "limit_drop_severe_pct": 50.0,
    "limit_drop_significant_pct": 30.0,
    "limit_bonus_severe": 3.0,
    "limit_bonus_significant": 1.5,
    "convergence_bonus": 1.0,
    "tier_strong_sharp": 7.0,
    "tier_sharp": 4.5,
    "tier_watch": 2.0,
    "early_late_split_hours": 24,  # boundary between "early week" and "late window"
    "min_snapshots_for_signal": 2,
    "min_age_hours_for_signal": 2.0,
    # velocity_shape() only - kept separate from x2_noise_floor_pp on
    # purpose so tuning the real scored threshold never silently changes
    # this still-unproven, watch-only signal too.
    "velocity_floor_pp": 0.30,
    "velocity_window_hours": 3.0,
    "velocity_min_window_hours": 0.25,  # 15 min - below this, a window is too short to be meaningful given real poll spacing
    "velocity_steam_ratio": 0.70,
    "velocity_drift_ratio": 0.20,
}

# Rejected: per-league / liquidity-aware thresholds (scaling ah_shift_
# threshold and x2_noise_floor_pp down for thin markets, on the theory
# that a casual bettor can swing a low-limit market by accident, so a
# bigger relative move should be required there to count as a real
# signal). Backtested against 2 real MJP rounds by bucketing matches into
# thin/mid/thick liquidity tiers (opening moneyline limit <$1,000 /
# $1,000-3,000 / >=$3,000): thin markets showed LOWER average 1X2
# displacement (1.90pp) and reached sharp/strong_sharp far less often
# (25%) than mid/thick markets (69-100%) - despite being tracked for MORE
# hours on average (92.7h vs 65.2h), which rules out "shorter observation
# window" as the explanation. The theory predicted thin markets would
# show MORE apparent (noisy) movement, needing a stricter bar; the data
# shows the opposite - thin leagues are simply quieter overall, most
# likely because sharp money concentrates on bigger leagues rather than
# bothering with niche ones. Not implemented.


def _first_last(series: list[dict], field: str) -> tuple[Any, Any]:
    valid = [s for s in series if s.get(field) is not None and s.get("status") != "suspended"]
    if not valid:
        return None, None
    return valid[0][field], valid[-1][field]


def ah_line_shift(spread_series: list[dict]) -> dict:
    """spread_series: chronological list of {captured_at, home_points, status}
    for the main (non-alternate, closest-to-zero) AH line.
    """
    opening, current = _first_last(spread_series, "home_points")
    if opening is None or current is None:
        return {"opening": None, "current": None, "shift": 0.0, "magnitude": 0.0, "direction": None}
    shift = current - opening
    direction = None
    if abs(shift) >= CONFIG["ah_shift_threshold"]:
        # more negative home points => home is being asked to give a bigger
        # start => home favored more => home backed by sharps.
        direction = "home" if shift < 0 else "away"
    return {"opening": opening, "current": current, "shift": shift, "magnitude": abs(shift), "direction": direction}


def x2_displacement(moneyline_series: list[dict]) -> dict:
    """moneyline_series: chronological list of
    {captured_at, fair_home_prob, fair_draw_prob, fair_away_prob, status}.
    Displacement is in percentage points (pp), from the true opening line
    (the first stored snapshot, not an arbitrary baseline).

    Fixed flaw: an earlier version only ever compared home vs away, so
    "draw" could never come out as the direction - even though jackpot
    rounds always include matches that end in draws. The three fair
    probabilities are de-vigged, so they always sum to 1 at both ends:
    home_pp + draw_pp + away_pp ≈ 0, money can't land on one side without
    leaving the others. So whichever of the three has the single most
    POSITIVE pp change is the one actually being backed - that also
    naturally lets draw win when it's the side seeing real money.

    KNOWN LIMITATION, investigated and deliberately left alone: this only
    ever compares the true opening snapshot to the current one, so a move
    that happened and then reversed can look like nothing happened at all.
    `peak_side`/`peak_pp`/`swung_back` below surface that history so it's
    not lost - but they do NOT feed magnitude/direction/the score. Tested
    the alternative (scoring off the peak instead of the final number)
    against 32 real MJP matches with real results: the final number's
    direction matched the actual outcome 13/32 times vs only 7/32 for the
    peak, and on the 9 matches where they disagreed the final number was
    right 7 times to the peak's 1. Small sample, but it points the
    opposite way from the intuition that "the peak is the truer signal" -
    a reversal usually means the market's settled view (wrong-footed
    money got out, or conviction faded) is more informative than the
    high-water mark it passed through. So the peak is shown, not scored.
    """
    result = {
        "home_pp": 0.0, "draw_pp": 0.0, "away_pp": 0.0, "magnitude": 0.0, "direction": None,
        "peak_side": None, "peak_pp": 0.0, "swung_back": False,
    }
    for outcome in ("home", "draw", "away"):
        opening, current = _first_last(moneyline_series, f"fair_{outcome}_prob")
        if opening is not None and current is not None:
            result[f"{outcome}_pp"] = (current - opening) * 100.0

    candidates = {"home": result["home_pp"], "draw": result["draw_pp"], "away": result["away_pp"]}
    leading_side = max(candidates, key=lambda k: candidates[k])
    magnitude = abs(candidates[leading_side])
    result["magnitude"] = magnitude
    if magnitude >= CONFIG["x2_noise_floor_pp"]:
        result["direction"] = leading_side

    peak_candidates = {}
    for outcome in ("home", "draw", "away"):
        field = f"fair_{outcome}_prob"
        valid = [s for s in moneyline_series if s.get(field) is not None and s.get("status") != "suspended"]
        if len(valid) < 2:
            peak_candidates[outcome] = 0.0
            continue
        opening_val = valid[0][field]
        best = max(valid, key=lambda s: (s[field] - opening_val) * 100.0)
        peak_candidates[outcome] = (best[field] - opening_val) * 100.0

    peak_side = max(peak_candidates, key=lambda k: peak_candidates[k])
    peak_pp = peak_candidates[peak_side]
    if abs(peak_pp) >= CONFIG["x2_noise_floor_pp"]:
        result["peak_side"] = peak_side
        result["peak_pp"] = peak_pp
        # the side that peaked has since come most of the way back
        result["swung_back"] = abs(candidates[peak_side]) < CONFIG["x2_noise_floor_pp"]
    return result


def velocity_shape(moneyline_series: list[dict]) -> dict:
    """Classifies HOW a 1X2 move happened over time, not just how big it
    got: a fast move concentrated in the recent window ("steam"), an old
    move that already happened and has since gone quiet ("drift"), a move
    still actively accumulating ("building"), one now heading back the
    other way ("reversal"), or one that swung away from the opening line
    and came most of the way back by the time we're checking ("swung_back").

    WATCH-ONLY - not wired into ah_score, x2_score, or the tier badge.
    Backtested against 2 real MJP rounds (32 matches, one confirmed non-
    jackpot test fixture excluded) on the AH line first: that line only
    ever moved in single atomic 0.25pt jumps, giving this classification
    nothing to actually distinguish. Re-run on the 1X2 series instead
    (real, continuous movement, not quantized steps), across 3h/6h/12h
    windows: "steam" and "building" calls were right about 70-100% of the
    time, "drift" calls only about 20-50% - a real, consistent pattern
    across all three windows tried. But the samples behind each label
    were tiny (as few as 2-5 decisive matches per label per window) -
    promising, not proven. Surfaced on the dashboard so it can keep being
    checked against real outcomes as more rounds come in, exactly like
    every other number in this file that started as a plain guess.

    Three known gaps, found and fixed after the first version shipped:
    - The look-back window used to be fixed (e.g. always 3h), which meant
      any match tracked for under 2x that window got no read at all,
      however big a move happened. It now shrinks to fit whatever history
      actually exists (down to `velocity_min_window_hours`), so an early
      real move still gets classified, just against a shorter recent
      slice - and if there genuinely isn't a usable window yet, it still
      honestly says "insufficient_history" rather than guessing.
    - A big swing that fully reverts by the time of the latest snapshot
      used to be invisible: opening-vs-current would show ~0 change, so
      it was called "quiet" even though something real happened along the
      way. Now the single largest deviation from the opening value,
      anywhere in the whole series, is tracked too - if that peak cleared
      the floor even though the net change didn't, it's labelled
      "swung_back" instead of being silently absorbed into "quiet".
    - It used to only ever compare home vs away, the same flaw
      x2_displacement() had: draw could never be the side a move was
      measured on, and the result never said which side (home/draw/away)
      the move was even about - a "steam" label alone didn't say steam on
      what. Both are fixed the same way: all three pp changes are
      computed, the one with the most POSITIVE change is the side the
      move is about (see x2_displacement()'s docstring for why "most
      positive" is the right test), and that side is now returned as
      `result["side"]`.
    """
    c = CONFIG
    configured_window = c["velocity_window_hours"]
    min_window = c["velocity_min_window_hours"]
    floor = c["velocity_floor_pp"]
    result = {
        "label": None,
        "side": None,
        "total_change_pp": 0.0,
        "recent_change_pp": None,
        "peak_change_pp": None,
        "window_hours": configured_window,
    }

    valid = [
        s for s in moneyline_series
        if s.get("fair_home_prob") is not None and s.get("fair_draw_prob") is not None
        and s.get("fair_away_prob") is not None and s.get("status") != "suspended"
    ]
    if len(valid) < 2:
        return result

    opening, current = valid[0], valid[-1]
    candidates = {
        outcome: (current[f"fair_{outcome}_prob"] - opening[f"fair_{outcome}_prob"]) * 100.0
        for outcome in ("home", "draw", "away")
    }
    side = max(candidates, key=lambda k: candidates[k])
    field = f"fair_{side}_prob"
    total_change = candidates[side]
    result["side"] = side
    result["total_change_pp"] = total_change

    opening_val = opening[field]
    peak_point = max(valid, key=lambda s: abs((s[field] - opening_val) * 100.0))
    peak_change = (peak_point[field] - opening_val) * 100.0
    result["peak_change_pp"] = peak_change

    if abs(total_change) < floor:
        result["label"] = "swung_back" if abs(peak_change) >= floor else "quiet"
        return result

    last_dt, first_dt = current["captured_at"], opening["captured_at"]
    total_history_hours = (last_dt - first_dt).total_seconds() / 3600.0
    effective_window = min(configured_window, total_history_hours / 2.0)
    if effective_window < min_window:
        result["label"] = "insufficient_history"
        return result
    result["window_hours"] = effective_window

    cutoff = last_dt - timedelta(hours=effective_window)
    older = [s for s in valid if s["captured_at"] <= cutoff]
    if not older:
        result["label"] = "insufficient_history"
        return result

    baseline = older[-1]
    recent_change = (current[field] - baseline[field]) * 100.0
    result["recent_change_pp"] = recent_change

    if (recent_change > 0) != (total_change > 0) and abs(recent_change) >= floor:
        result["label"] = "reversal"
        return result

    ratio = abs(recent_change) / abs(total_change) if total_change else 0.0
    if ratio >= c["velocity_steam_ratio"]:
        result["label"] = "steam"
    elif ratio <= c["velocity_drift_ratio"]:
        result["label"] = "drift"
    else:
        result["label"] = "building"
    return result


def limit_drop_pct(series: list[dict]) -> float:
    """Only ever measures a drop - a rise is clamped to 0.0, deliberately.

    A "limit rises when Pinnacle is confident in the price" signal was
    investigated and rejected after checking it against 2 real MJP rounds
    (33 matches): every single match showed a net limit *rise*, never a
    drop, and the rise percentages repeated identically across unrelated
    matches in the same league (e.g. two different Czech First Liga
    matches both went +400% ML, two different Serie A matches both went
    +344.4%). That pattern is consistent with Pinnacle mechanically
    ramping a new market from a small placeholder limit up to its
    standard size on a fixed per-league schedule, independent of any
    money actually landing on that specific match - not with the book
    reacting to real order flow. Reviving a rise-based signal would need
    a real per-league baseline ramp curve to detect deviations *from*,
    not just "did it rise," which the 2-round sample can't support yet.
    """
    opening, current = _first_last(series, "limit_amount")
    if not opening or opening <= 0 or current is None:
        return 0.0
    return max(0.0, (opening - current) / opening * 100.0)


def detect_contested(
    moneyline_series: list[dict], kickoff: datetime, split_hours: float = CONFIG["early_late_split_hours"]
) -> bool:
    """A match is 'contested' when early-week and late-window sharp
    direction disagree (per the user's spec: sharps moved one way, then
    reversed) - each window needs enough of its own history for the
    comparison to mean anything, not just a single crossing snapshot.
    """
    cutoff = kickoff - timedelta(hours=split_hours)
    early = [s for s in moneyline_series if s.get("captured_at") and s["captured_at"] <= cutoff]
    late = [s for s in moneyline_series if s.get("captured_at") and s["captured_at"] > cutoff]
    if len(early) < 2 or len(late) < 2:
        return False
    early_disp = x2_displacement(early)
    late_disp = x2_displacement(late)
    if early_disp["direction"] is None or late_disp["direction"] is None:
        return False
    return early_disp["direction"] != late_disp["direction"]


def _score_ah(magnitude: float) -> float:
    c = CONFIG
    if magnitude >= c["ah_shift_syndicate"]:
        return c["ah_score_syndicate"]
    if magnitude >= c["ah_shift_sharp"]:
        return c["ah_score_sharp"]
    if magnitude >= c["ah_shift_threshold"]:
        return c["ah_score_threshold"]
    return 0.0


def _score_x2(magnitude_pp: float) -> float:
    c = CONFIG
    if magnitude_pp >= c["x2_strong_pp"]:
        return c["x2_score_strong"]
    if magnitude_pp >= c["x2_meaningful_pp"]:
        return c["x2_score_meaningful"]
    if magnitude_pp >= c["x2_noise_floor_pp"]:
        return c["x2_score_noise_floor"]
    return 0.0


def _score_limit(drop_pct: float) -> float:
    c = CONFIG
    if drop_pct >= c["limit_drop_severe_pct"]:
        return c["limit_bonus_severe"]
    if drop_pct >= c["limit_drop_significant_pct"]:
        return c["limit_bonus_significant"]
    return 0.0


def classify_tier(total_score: float) -> str:
    capped = min(total_score, 10.0)
    c = CONFIG
    if capped >= c["tier_strong_sharp"]:
        return "strong_sharp"
    if capped >= c["tier_sharp"]:
        return "sharp"
    if capped >= c["tier_watch"]:
        return "watch"
    return "no_signal"


def data_sufficiency(moneyline_series: list[dict], spread_series: list[dict], now: datetime) -> bool:
    c = CONFIG
    for series in (moneyline_series, spread_series):
        if len(series) >= c["min_snapshots_for_signal"] and series[0].get("captured_at"):
            age_hours = (now - series[0]["captured_at"]).total_seconds() / 3600.0
            if age_hours >= c["min_age_hours_for_signal"]:
                return True
    return False


def compute_match_score(
    moneyline_series: list[dict],
    spread_series: list[dict],
    kickoff: datetime,
    now: datetime | None = None,
) -> dict:
    """Top-level pure scoring entry point. Both series must be chronological
    (oldest first) and pre-filtered to the *main* line for each market type
    (see ingest.select_main_line).
    """
    now = now or datetime.now(timezone.utc)

    if not data_sufficiency(moneyline_series, spread_series, now):
        return {
            "tier": "insufficient_data",
            "sharp_side": None,
            "contested": False,
            "ah": ah_line_shift(spread_series),
            "x2": x2_displacement(moneyline_series),
            "velocity": velocity_shape(moneyline_series),
            "moneyline_limit_drop_pct": limit_drop_pct(moneyline_series),
            "spread_limit_drop_pct": limit_drop_pct(spread_series),
            "ah_score": 0.0,
            "x2_score": 0.0,
            "limit_bonus": 0.0,
            "convergence_bonus": 0.0,
            "total_score": 0.0,
        }

    ah = ah_line_shift(spread_series)
    x2 = x2_displacement(moneyline_series)
    ml_limit_drop = limit_drop_pct(moneyline_series)
    sp_limit_drop = limit_drop_pct(spread_series)
    max_limit_drop = max(ml_limit_drop, sp_limit_drop)

    # Backtested against 2 real MJP rounds: every nonzero AH shift observed
    # was exactly the same 0.25 points, yet the matching probability move on
    # that same market ranged from 0.47pp to 9.73pp - raw points hide a real
    # ~20x spread in how much the move actually meant. The safe fix is NOT
    # to swap points for probability everywhere: once the line has actually
    # stepped, the probability read is contaminated by the target changing
    # underneath it (a harder/easier line mechanically moves the covering
    # probability - see the "Reading the Tape" manual's chart 5 callout).
    # So the probability-based read only ever substitutes in when the line
    # itself hasn't moved - the case the tool currently scores as a flat
    # zero even if real positioning was happening at that fixed line the
    # whole time. x2_displacement() is reused as-is (rather than a new
    # function) since it already degrades gracefully on a two-way market:
    # fair_draw_prob is always None for spread rows, so the draw slot just
    # stays 0.0. Its pp thresholds are reused too, for lack of any real
    # calibration data yet for this specific flat-line case - same
    # provisional-until-backtested status as every other number here.
    ah_prob = x2_displacement(spread_series)
    if ah["magnitude"] >= CONFIG["ah_shift_threshold"]:
        ah_score = _score_ah(ah["magnitude"])
        ah_effective_direction = ah["direction"]
    else:
        ah_score = _score_x2(ah_prob["magnitude"])
        ah_effective_direction = ah_prob["direction"]

    x2_score = _score_x2(x2["magnitude"])
    limit_bonus = _score_limit(max_limit_drop)

    conv_bonus = 0.0
    if (
        ah["magnitude"] >= CONFIG["ah_shift_threshold"]
        and x2["magnitude"] >= CONFIG["x2_meaningful_pp"]
        and max_limit_drop >= CONFIG["limit_drop_significant_pct"]
        and ah["direction"] is not None
        and ah["direction"] == x2["direction"]
    ):
        conv_bonus = CONFIG["convergence_bonus"]

    total = 1.4 * ah_score + 1.0 * x2_score + limit_bonus + conv_bonus
    contested = detect_contested(moneyline_series, kickoff)

    # Sharp side priority: AH shift first (sharps move AH first per the
    # spec), then largest 1X2 displacement. ah_effective_direction falls
    # back to the flat-line probability read (see above) when the line
    # itself hasn't moved, so early positioning at an unchanged number
    # still gets to set the side instead of falling through to 1X2.
    if ah_effective_direction:
        sharp_side = ah_effective_direction
    elif x2["direction"]:
        sharp_side = x2["direction"]
    else:
        sharp_side = None

    ah["prob_fallback_used"] = ah["magnitude"] < CONFIG["ah_shift_threshold"]
    ah["prob_fallback"] = ah_prob

    return {
        "tier": classify_tier(total) if not contested else "contested",
        "sharp_side": "contested" if contested else sharp_side,
        "contested": contested,
        "ah": ah,
        "x2": x2,
        "velocity": velocity_shape(moneyline_series),
        "moneyline_limit_drop_pct": ml_limit_drop,
        "spread_limit_drop_pct": sp_limit_drop,
        "ah_score": ah_score,
        "x2_score": x2_score,
        "limit_bonus": limit_bonus,
        "convergence_bonus": conv_bonus,
        "total_score": total,
    }


# ---------------------------------------------------------------------------
# DB-touching orchestration below.
# ---------------------------------------------------------------------------

from app import db  # noqa: E402


async def fetch_series(matchup_id: int, market_type: str, period: int = 0, is_alternate: bool = False) -> list[dict]:
    rows = await db.fetch(
        """
        select captured_at, home_price, draw_price, away_price, home_points,
               limit_amount, fair_home_prob, fair_draw_prob, fair_away_prob, status
        from market_snapshots
        where matchup_id = $1 and market_type = $2 and period = $3 and is_alternate = $4
        order by captured_at asc
        """,
        matchup_id,
        market_type,
        period,
        is_alternate,
    )
    return [dict(r) for r in rows]


async def compute_and_store_score(matchup_id: int, kickoff: datetime) -> dict:
    moneyline_series = await fetch_series(matchup_id, "moneyline")
    spread_series = await fetch_series(matchup_id, "spread")
    result = compute_match_score(moneyline_series, spread_series, kickoff)

    await db.execute(
        """
        insert into match_scores (
            matchup_id, ah_score, x2_score, limit_bonus, convergence_bonus,
            total_score, tier, sharp_side, contested
        ) values ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """,
        matchup_id,
        result["ah_score"],
        result["x2_score"],
        result["limit_bonus"],
        result["convergence_bonus"],
        result["total_score"],
        result["tier"],
        result["sharp_side"],
        result["contested"],
    )
    await db.execute(
        """
        insert into signals (matchup_id, signal_type, direction, magnitude, detail_json)
        values ($1,'ah_line_shift',$2,$3,$4::jsonb),
               ($1,'x2_displacement',$5,$6,$7::jsonb),
               ($1,'limit_movement',$8,$9,$10::jsonb),
               ($1,'velocity_shape',$11,$12,$13::jsonb)
        """,
        matchup_id,
        result["ah"]["direction"],
        result["ah"]["magnitude"],
        db.to_jsonb(result["ah"]),
        result["x2"]["direction"],
        result["x2"]["magnitude"],
        db.to_jsonb(result["x2"]),
        result["sharp_side"],
        max(result["moneyline_limit_drop_pct"], result["spread_limit_drop_pct"]),
        db.to_jsonb(
            {
                "moneyline_limit_drop_pct": result["moneyline_limit_drop_pct"],
                "spread_limit_drop_pct": result["spread_limit_drop_pct"],
            }
        ),
        # "direction" column repurposed as the shape label (steam/drift/
        # building/reversal/quiet/None) - this signal has no home/away
        # side of its own, it's watch-only, see velocity_shape() docstring.
        result["velocity"]["label"],
        abs(result["velocity"]["total_change_pp"]),
        db.to_jsonb(result["velocity"]),
    )
    return result
