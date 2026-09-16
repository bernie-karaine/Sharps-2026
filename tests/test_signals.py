from datetime import datetime, timedelta, timezone

import pytest

from app.signals import (
    CONFIG,
    ah_line_shift,
    classify_tier,
    compute_match_score,
    data_sufficiency,
    detect_contested,
    limit_drop_pct,
    velocity_shape,
    x2_displacement,
)

KICKOFF = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)


def t(hours_before_kickoff):
    return KICKOFF - timedelta(hours=hours_before_kickoff)


def ml_point(hours_before, fh, fd, fa, limit=2500.0, status="open"):
    return {
        "captured_at": t(hours_before),
        "fair_home_prob": fh,
        "fair_draw_prob": fd,
        "fair_away_prob": fa,
        "limit_amount": limit,
        "status": status,
    }


def sp_point(hours_before, points, limit=5000.0, status="open"):
    return {
        "captured_at": t(hours_before),
        "home_points": points,
        "limit_amount": limit,
        "status": status,
    }


# ---------------------------------------------------------------------------
# ah_line_shift
# ---------------------------------------------------------------------------

def test_ah_line_shift_home_backed():
    series = [sp_point(48, -0.25), sp_point(1, -1.25)]
    result = ah_line_shift(series)
    assert result["shift"] == -1.0
    assert result["direction"] == "home"
    assert result["magnitude"] == 1.0


def test_ah_line_shift_away_backed():
    series = [sp_point(48, -0.5), sp_point(1, 0.25)]
    result = ah_line_shift(series)
    assert result["direction"] == "away"


def test_ah_line_shift_below_threshold_no_direction():
    series = [sp_point(48, -0.5), sp_point(1, -0.6)]
    result = ah_line_shift(series)
    assert result["direction"] is None


def test_ah_line_shift_insufficient_data():
    result = ah_line_shift([])
    assert result["direction"] is None
    assert result["magnitude"] == 0.0


# ---------------------------------------------------------------------------
# x2_displacement
# ---------------------------------------------------------------------------

def test_x2_displacement_home_backed():
    series = [
        ml_point(48, 0.40, 0.28, 0.32),
        ml_point(1, 0.42, 0.27, 0.31),
    ]
    result = x2_displacement(series)
    assert result["home_pp"] == pytest.approx(2.0, abs=0.05)
    assert result["direction"] == "home"


def test_x2_displacement_noise_floor():
    series = [
        ml_point(48, 0.40, 0.30, 0.30),
        ml_point(1, 0.4015, 0.2995, 0.2990),
    ]
    result = x2_displacement(series)
    assert result["direction"] is None


def test_x2_displacement_swung_back_when_move_reverts_by_the_end():
    # a real 5pp home swing happens, then it comes almost all the way back -
    # opening vs current alone shows ~0pp and no direction, hiding that a
    # real move happened along the way
    series = [
        ml_point(48, 0.400, 0.30, 0.300),
        ml_point(24, 0.450, 0.28, 0.270),  # peaked at +5.0pp home
        ml_point(1, 0.402, 0.30, 0.298),   # back to near the opening line
    ]
    result = x2_displacement(series)
    assert result["direction"] is None  # final reading: nothing happened
    assert result["peak_side"] == "home"
    assert result["peak_pp"] == pytest.approx(5.0, abs=0.1)
    assert result["swung_back"] is True


def test_x2_displacement_no_swung_back_when_move_holds():
    series = [
        ml_point(48, 0.40, 0.30, 0.30),
        ml_point(1, 0.46, 0.28, 0.26),  # move happens and stays
    ]
    result = x2_displacement(series)
    assert result["direction"] == "home"
    assert result["swung_back"] is False


def test_x2_displacement_draw_backed():
    # money moves onto the draw, off of both home and away - a real
    # jackpot scenario the old home-vs-away-only comparison could never
    # report, since it never even looked at draw_pp
    series = [
        ml_point(48, 0.34, 0.30, 0.36),
        ml_point(1, 0.31, 0.36, 0.33),
    ]
    result = x2_displacement(series)
    assert result["draw_pp"] == pytest.approx(6.0, abs=0.05)
    assert result["direction"] == "draw"


# ---------------------------------------------------------------------------
# velocity_shape (window default is 3h, per CONFIG["velocity_window_hours"])
# ---------------------------------------------------------------------------

def test_velocity_shape_steam_when_move_concentrated_in_window():
    series = [
        ml_point(48, 0.40, 0.30, 0.30),
        ml_point(5, 0.40, 0.30, 0.30),   # flat right up until the window starts
        ml_point(0.5, 0.46, 0.28, 0.26),  # then the whole move happens inside it
    ]
    result = velocity_shape(series)
    assert result["label"] == "steam"
    assert result["side"] == "home"


def test_velocity_shape_reports_draw_as_the_side():
    # money moves onto the draw this time, not home or away - the old
    # version could never say this, since it only ever compared home vs
    # away and had no "side" output at all
    series = [
        ml_point(48, 0.34, 0.30, 0.36),
        ml_point(5, 0.34, 0.30, 0.36),
        ml_point(0.5, 0.31, 0.36, 0.33),
    ]
    result = velocity_shape(series)
    assert result["side"] == "draw"
    assert result["label"] == "steam"


def test_velocity_shape_drift_when_move_already_settled():
    series = [
        ml_point(48, 0.40, 0.30, 0.30),
        ml_point(40, 0.46, 0.28, 0.26),  # the move already happened, days ago
        ml_point(0.5, 0.46, 0.28, 0.26),  # nothing since - flat inside the window
    ]
    result = velocity_shape(series)
    assert result["label"] == "drift"


def test_velocity_shape_building_when_move_is_split():
    series = [
        ml_point(48, 0.40, 0.30, 0.30),
        ml_point(40, 0.43, 0.29, 0.28),  # half the move happens early
        ml_point(0.5, 0.46, 0.28, 0.26),  # the other half happens inside the window
    ]
    result = velocity_shape(series)
    assert result["label"] == "building"


def test_velocity_shape_reversal_when_recent_move_flips_direction():
    series = [
        ml_point(48, 0.40, 0.30, 0.30),
        ml_point(40, 0.50, 0.26, 0.24),  # moved home a lot, early
        ml_point(0.5, 0.44, 0.29, 0.27),  # recently reversed back toward away
    ]
    result = velocity_shape(series)
    assert result["label"] == "reversal"


def test_velocity_shape_quiet_below_floor():
    series = [ml_point(48, 0.400, 0.30, 0.300), ml_point(0.5, 0.401, 0.30, 0.299)]
    result = velocity_shape(series)
    assert result["label"] == "quiet"


def test_velocity_shape_insufficient_history_when_truly_too_recent():
    # only 18 minutes of total history - even the shrunk adaptive window
    # (see below) can't get above velocity_min_window_hours (15 min) here
    series = [ml_point(0.4, 0.40, 0.30, 0.30), ml_point(0.1, 0.46, 0.28, 0.26)]
    result = velocity_shape(series)
    assert result["label"] == "insufficient_history"


def test_velocity_shape_adaptive_window_still_classifies_a_short_history():
    # only 1.9h of total history - the old fixed-3h-window version would
    # have needed 6h and called this "insufficient_history" outright, even
    # though a real, fast move is sitting right there in the data
    series = [
        ml_point(2, 0.40, 0.30, 0.30),
        ml_point(1.9, 0.40, 0.30, 0.30),  # flat for almost the whole window
        ml_point(0.1, 0.46, 0.28, 0.26),  # then a real move near the end
    ]
    result = velocity_shape(series)
    assert result["label"] == "steam"
    assert result["window_hours"] < CONFIG["velocity_window_hours"]


def test_velocity_shape_swung_back_when_move_reverts_by_the_end():
    # a real 8pp swing happens, then it comes almost all the way back -
    # opening vs current alone would show ~0 change and call this "quiet"
    series = [
        ml_point(48, 0.400, 0.30, 0.300),
        ml_point(24, 0.480, 0.27, 0.250),  # swung hard toward home
        ml_point(0.5, 0.402, 0.30, 0.298),  # and back to near where it started
    ]
    result = velocity_shape(series)
    assert result["label"] == "swung_back"
    assert result["peak_change_pp"] == pytest.approx(8.0, abs=0.1)


def test_velocity_shape_no_label_with_fewer_than_two_snapshots():
    assert velocity_shape([])["label"] is None
    assert velocity_shape([ml_point(1, 0.4, 0.3, 0.3)])["label"] is None


# ---------------------------------------------------------------------------
# limit_drop_pct
# ---------------------------------------------------------------------------

def test_limit_drop_pct_severe():
    series = [sp_point(48, -0.5, limit=5000.0), sp_point(1, -1.0, limit=2000.0)]
    assert limit_drop_pct(series) == 60.0


def test_limit_drop_pct_no_drop_floors_at_zero():
    series = [sp_point(48, -0.5, limit=2000.0), sp_point(1, -0.5, limit=5000.0)]
    assert limit_drop_pct(series) == 0.0


# ---------------------------------------------------------------------------
# contested detection
# ---------------------------------------------------------------------------

def test_detect_contested_true_on_direction_flip():
    series = [
        ml_point(60, 0.40, 0.30, 0.30),
        ml_point(50, 0.44, 0.28, 0.28),  # early: home backed
        ml_point(10, 0.44, 0.28, 0.28),
        ml_point(1, 0.38, 0.30, 0.32),  # late: away backed
    ]
    assert detect_contested(series, KICKOFF) is True


def test_detect_contested_false_when_consistent():
    series = [
        ml_point(60, 0.40, 0.30, 0.30),
        ml_point(50, 0.44, 0.28, 0.28),
        ml_point(10, 0.46, 0.27, 0.27),
        ml_point(1, 0.48, 0.26, 0.26),
    ]
    assert detect_contested(series, KICKOFF) is False


def test_detect_contested_false_without_enough_history_each_side():
    series = [ml_point(50, 0.44, 0.28, 0.28), ml_point(1, 0.38, 0.30, 0.32)]
    assert detect_contested(series, KICKOFF) is False


# ---------------------------------------------------------------------------
# tiers
# ---------------------------------------------------------------------------

def test_classify_tier_boundaries():
    assert classify_tier(0.0) == "no_signal"
    assert classify_tier(1.9) == "no_signal"
    assert classify_tier(2.0) == "watch"
    assert classify_tier(4.5) == "sharp"
    assert classify_tier(7.0) == "strong_sharp"
    assert classify_tier(999) == "strong_sharp"  # capped at 10 before classifying


# ---------------------------------------------------------------------------
# data sufficiency
# ---------------------------------------------------------------------------

def test_data_sufficiency_false_when_too_new():
    now = KICKOFF - timedelta(hours=47.5)
    series = [ml_point(48, 0.4, 0.3, 0.3), ml_point(47.6, 0.4, 0.3, 0.3)]
    assert data_sufficiency(series, [], now) is False


def test_data_sufficiency_true_with_enough_age_and_points():
    now = KICKOFF - timedelta(hours=10)
    series = [ml_point(48, 0.4, 0.3, 0.3), ml_point(10, 0.4, 0.3, 0.3)]
    assert data_sufficiency(series, [], now) is True


# ---------------------------------------------------------------------------
# end-to-end scoring scenarios
# ---------------------------------------------------------------------------

def test_compute_match_score_insufficient_data():
    result = compute_match_score([ml_point(0.1, 0.4, 0.3, 0.3)], [], KICKOFF, now=KICKOFF - timedelta(hours=71.9))
    assert result["tier"] == "insufficient_data"


def test_compute_match_score_strong_sharp_convergence():
    ml = [
        ml_point(48, 0.40, 0.30, 0.30, limit=2500.0),
        ml_point(1, 0.46, 0.28, 0.26, limit=1000.0),  # 60% limit drop, home backed
    ]
    sp = [
        sp_point(48, -0.25, limit=5000.0),
        sp_point(1, -1.5, limit=2000.0),  # syndicate-level AH shift, home backed
    ]
    result = compute_match_score(ml, sp, KICKOFF, now=KICKOFF - timedelta(hours=0.5))
    assert result["tier"] == "strong_sharp"
    assert result["sharp_side"] == "home"
    assert result["convergence_bonus"] == CONFIG["convergence_bonus"]


def test_compute_match_score_quiet_market_is_no_signal():
    ml = [ml_point(48, 0.40, 0.30, 0.30), ml_point(1, 0.401, 0.2995, 0.2995)]
    sp = [sp_point(48, -0.5), sp_point(1, -0.5)]
    result = compute_match_score(ml, sp, KICKOFF, now=KICKOFF - timedelta(hours=0.5))
    assert result["tier"] == "no_signal"
    assert result["sharp_side"] is None


def test_compute_match_score_flat_line_uses_probability_fallback():
    """A flat AH line used to always score 0 for ah_score, even if real
    positioning was happening at that fixed number the whole time - the
    exact blind spot found by backtesting 2 real MJP rounds (see the
    comment above ah_prob in compute_match_score)."""
    ml = [ml_point(48, 0.40, 0.30, 0.30), ml_point(1, 0.401, 0.2995, 0.2995)]  # 1X2 quiet
    sp = [sp_point(48, -0.5, limit=5000.0), sp_point(1, -0.5, limit=5000.0)]  # line never moves
    sp[0]["fair_home_prob"], sp[0]["fair_away_prob"] = 0.55, 0.45
    sp[1]["fair_home_prob"], sp[1]["fair_away_prob"] = 0.58, 0.42  # +3pp home, well past strong

    result = compute_match_score(ml, sp, KICKOFF, now=KICKOFF - timedelta(hours=0.5))
    assert result["ah"]["direction"] is None  # the line itself never moved
    assert result["ah"]["prob_fallback_used"] is True
    assert result["ah_score"] == CONFIG["x2_score_strong"]
    assert result["sharp_side"] == "home"
    assert result["tier"] == "strong_sharp"
