"""
L0 is the only layer that can be proven. It is pure code — deterministic,
no IO, no model calls — which is the entire reason it exists, so it gets
tested and L1/L2 never will be.

`now` is always injected. A gate that reads the wall clock is untestable
at exactly the hours where its bugs live.
"""

from datetime import datetime

import pytest

import heartbeat as hb

G = hb.CONFIG["gate"]
H = hb.CONFIG["heartbeat"]

NOON = datetime(2026, 7, 17, 12, 0, 0)   # safely outside the DND window


def open_state() -> hb.State:
    # passes every rule; each test trips exactly one
    return hb.State(
        presence="active",
        last_proactive=0.0,          # epoch 0 → cooldown long expired
        last_interaction=0.0,
        daily_proactive_count=0,
        activity_absorbed=False,
    )


# ── gate: five rules, each blocking alone ─────────────────────

def test_gate_open_when_nothing_blocks():
    allowed, reason = hb.gate(open_state(), NOON)
    assert allowed
    assert reason == "open"


def test_gate_blocks_while_sleeping():
    s = open_state()
    s.presence = "sleeping"
    allowed, reason = hb.gate(s, NOON)
    assert not allowed
    assert reason == "sleeping"


def test_gate_blocks_inside_dnd():
    s = open_state()
    allowed, reason = hb.gate(s, datetime(2026, 7, 17, 0, 0, 0))
    assert not allowed
    assert reason == "dnd"


def test_gate_blocks_on_cooldown():
    s = open_state()
    s.last_proactive = NOON.timestamp() - 60      # 1 min ago, cap is 90
    allowed, reason = hb.gate(s, NOON)
    assert not allowed
    assert reason.startswith("cooldown")


def test_gate_blocks_on_daily_cap():
    s = open_state()
    s.daily_proactive_count = G["daily_proactive_max"]
    allowed, reason = hb.gate(s, NOON)
    assert not allowed
    assert reason.startswith("daily cap")


def test_gate_blocks_while_absorbed():
    s = open_state()
    s.activity_absorbed = True
    allowed, reason = hb.gate(s, NOON)
    assert not allowed
    assert reason == "absorbed in activity"


# ── the midnight crossing ─────────────────────────────────────
# `dnd_start <= t < dnd_end` is False for every minute of 23:30–06:30.
# The naive form fails only at night, which is the one window that matters.

@pytest.mark.parametrize("hh,mm,blocked", [
    (23, 30, True),    # start, inclusive
    (23, 45, True),
    (23, 59, True),
    (0, 0, True),      # the crossing itself
    (3, 0, True),
    (6, 29, True),
    (6, 30, False),    # end, exclusive
    (7, 0, False),
    (12, 0, False),
    (23, 29, False),   # one minute short of the window
])
def test_dnd_window_crosses_midnight(hh, mm, blocked):
    now = datetime(2026, 7, 17, hh, mm)
    assert hb._in_window(now, G["dnd_start"], G["dnd_end"]) is blocked


@pytest.mark.parametrize("hh,mm,inside", [
    (11, 59, False),
    (12, 0, True),
    (12, 30, True),
    (13, 0, False),
])
def test_in_window_same_day_still_works(hh, mm, inside):
    # guards the non-wrapping branch — a fix for midnight must not eat this
    assert hb._in_window(datetime(2026, 7, 17, hh, mm), "12:00", "13:00") is inside


# ── daily counter rollover ────────────────────────────────────

def test_daily_count_resets_across_date_boundary():
    s = hb.State(daily_proactive_count=G["daily_proactive_max"],
                 daily_count_date="2026-07-16")
    s.roll_day(datetime(2026, 7, 17, 0, 5))
    assert s.daily_count_date == "2026-07-17"
    assert s.daily_proactive_count == 0


def test_daily_count_survives_within_one_day():
    s = hb.State(daily_proactive_count=3, daily_count_date="2026-07-17")
    s.roll_day(datetime(2026, 7, 17, 23, 59))
    assert s.daily_proactive_count == 3


def test_gate_reopens_after_rollover():
    # capped yesterday, released today — the two rules meeting
    s = open_state()
    s.daily_proactive_count = G["daily_proactive_max"]
    s.daily_count_date = "2026-07-16"
    assert hb.gate(s, NOON)[0] is False
    s.roll_day(NOON)
    assert hb.gate(s, NOON)[0] is True


# ── next_interval: every branch ───────────────────────────────

def test_interval_none_while_sleeping():
    s = open_state()
    s.presence = "sleeping"
    assert hb.next_interval(s, NOON) is None


def test_interval_in_conversation():
    s = open_state()
    s.in_conversation = True
    assert hb.next_interval(s, NOON) == H["tick_in_conversation"]


def test_interval_afterglow():
    s = open_state()
    s.last_interaction = NOON.timestamp() - 60          # 1 min < 30
    assert hb.next_interval(s, NOON) == H["tick_afterglow"]


def test_interval_activity():
    s = open_state()
    s.last_interaction = NOON.timestamp() - 3600        # past the afterglow
    s.activity_running = True
    assert hb.next_interval(s, NOON) == H["tick_activity"]


def test_interval_idle_stays_within_jitter_bounds():
    s = open_state()
    s.last_interaction = NOON.timestamp() - 3600
    lo = H["tick_idle"] - H["tick_jitter"]
    hi = H["tick_idle"] + H["tick_jitter"]
    seen = {hb.next_interval(s, NOON) for _ in range(500)}
    assert all(lo <= v <= hi for v in seen)
    assert len(seen) > 1, "jitter is not actually varying"


def test_conversation_outranks_afterglow():
    # ordering is the contract: 15s wins while she's being talked to
    s = open_state()
    s.in_conversation = True
    s.last_interaction = NOON.timestamp() - 60
    s.activity_running = True
    assert hb.next_interval(s, NOON) == H["tick_in_conversation"]


# ── conversation timeout ──────────────────────────────────────

def test_silent_conversation_expires():
    s = open_state()
    s.in_conversation = True
    s.last_proactive = NOON.timestamp()   # cooldown blocks; irrelevant here
    s.last_interaction = NOON.timestamp() - H["conversation_timeout_min"] * 60
    hb.tick(s, NOON)
    assert s.in_conversation is False
    # and the loop escapes the 15s rate
    assert hb.next_interval(s, NOON) != H["tick_in_conversation"]


def test_live_conversation_does_not_expire():
    s = open_state()
    s.in_conversation = True
    s.last_interaction = NOON.timestamp() - 60          # 1 min of quiet
    hb.tick(s, NOON)
    assert s.in_conversation is True
