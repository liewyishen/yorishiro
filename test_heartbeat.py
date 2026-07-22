"""
L0 is the only layer that can be proven. It is pure code — deterministic,
no IO, no model calls — which is the entire reason it exists, so it gets
tested and L1/L2 never will be.

`now` is always injected. A gate that reads the wall clock is untestable
at exactly the hours where its bugs live.
"""

import json
import logging
import queue
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import heartbeat as hb

# The real model-access functions, captured at import — BEFORE the autouse
# no_test_may_reach_a_real_model fixture repoints hb.call_llm / hb.stream_llm
# to raise. The cache/session tests below exercise these originals directly
# against a fake client (no network, no tokens), which is the only way to
# prove the session_id wiring and usage logging that live INSIDE them.
_REAL_CALL_LLM = hb.call_llm
_REAL_STREAM_LLM = hb.stream_llm

G = hb.CONFIG["gate"]
H = hb.CONFIG["heartbeat"]

NOON = datetime(2026, 7, 17, 12, 0, 0)  # safely outside the DND window


class _RealModelReached(BaseException):
    """
    Deliberately not an Exception. intention() catches Exception so that a
    dead judgment falls silent in production — which would also swallow
    this guard and hide the mistake it exists to expose.
    """


@pytest.fixture(autouse=True)
def no_test_may_reach_a_real_model(monkeypatch):
    """
    tick() calls L1 now, so a test that merely exercises the loop will bill
    a real model call unless something stops it: slow, flaky offline, and
    spending money to assert nothing. Tests that care about what a model
    said monkeypatch over this.
    """

    def forbidden(*_a, **_k):
        raise _RealModelReached("a test reached a real model — monkeypatch it")

    monkeypatch.setattr(hb, "call_llm", forbidden)
    monkeypatch.setattr(hb, "stream_llm", forbidden)


@pytest.fixture(autouse=True)
def memory_under_tmp(tmp_path, monkeypatch):
    """
    Redirect her/memory/ into a per-test tmp dir. Two jobs: no test ever
    writes the real save-file (tick() now appends a urge line every beat), and
    each test starts with no "lately" — so a stray daily reflection from one
    test can never leak into another's L1 context.
    """
    monkeypatch.setattr(hb, "MEMORY_DIR", tmp_path / "memory")


def open_state() -> hb.State:
    # passes every rule; each test trips exactly one
    return hb.State(
        presence="active",
        last_proactive=0.0,  # epoch 0 → cooldown long expired
        last_interaction=0.0,
        daily_proactive_count=0,
        activity_absorbed=False,
    )


def idle_state() -> hb.State:
    """
    Clears the gate AND both L1 skips — the only shape of state where a
    judgment actually happens. last_interaction is pinned rather than left
    at open_state()'s 0.0, which clears the afterglow window only by
    reading as thirty million minutes of silence: true, but by accident.
    """
    s = open_state()
    s.last_interaction = NOON.timestamp() - (H["afterglow_window_min"] + 10) * 60
    return s


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
    s.last_proactive = NOON.timestamp() - 60  # 1 min ago, cap is 90
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


@pytest.mark.parametrize(
    "hh,mm,blocked",
    [
        (23, 30, True),  # start, inclusive
        (23, 45, True),
        (23, 59, True),
        (0, 0, True),  # the crossing itself
        (3, 0, True),
        (6, 29, True),
        (6, 30, False),  # end, exclusive
        (7, 0, False),
        (12, 0, False),
        (23, 29, False),  # one minute short of the window
    ],
)
def test_dnd_window_crosses_midnight(hh, mm, blocked):
    now = datetime(2026, 7, 17, hh, mm)
    assert hb._in_window(now, G["dnd_start"], G["dnd_end"]) is blocked


@pytest.mark.parametrize(
    "hh,mm,inside",
    [
        (11, 59, False),
        (12, 0, True),
        (12, 30, True),
        (13, 0, False),
    ],
)
def test_in_window_same_day_still_works(hh, mm, inside):
    # guards the non-wrapping branch — a fix for midnight must not eat this
    assert hb._in_window(datetime(2026, 7, 17, hh, mm), "12:00", "13:00") is inside


# ── daily counter rollover ────────────────────────────────────


def test_daily_count_resets_across_date_boundary():
    s = hb.State(daily_proactive_count=G["daily_proactive_max"], daily_count_date="2026-07-16")
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
    s.last_interaction = NOON.timestamp() - 60  # 1 min < 30
    assert hb.next_interval(s, NOON) == H["tick_afterglow"]


def test_interval_activity():
    s = open_state()
    s.last_interaction = NOON.timestamp() - 3600  # past the afterglow
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
    s.last_proactive = NOON.timestamp()  # cooldown blocks; irrelevant here
    s.last_interaction = NOON.timestamp() - H["conversation_timeout_min"] * 60
    hb.tick(s, NOON)
    assert s.in_conversation is False
    # and the loop escapes the 15s rate
    assert hb.next_interval(s, NOON) != H["tick_in_conversation"]


def test_live_conversation_does_not_expire(monkeypatch):
    # this tick clears the gate, so it reaches L1. What she wants is not
    # what this test is about — stub it and keep the subject the timeout.
    monkeypatch.setattr(hb, "intention", lambda *_a, **_k: (0.0, "none", "stubbed"))
    s = open_state()
    s.in_conversation = True
    s.last_interaction = NOON.timestamp() - 60  # 1 min of quiet
    hb.tick(s, NOON)
    assert s.in_conversation is True


# ── reply worker: the handoff, without a live model ───────────
# The worker is the one new thread that talks to a model, so the two
# promises worth pinning are: the lifecycle always closes over the
# broadcast, and a dead model kills one reply, not the worker.


def _worker() -> queue.Queue:
    replies: queue.Queue = queue.Queue()
    threading.Thread(target=hb.reply_worker, args=(replies,), daemon=True).start()
    return replies


def _next_lifecycle(sub: queue.Queue) -> list[dict]:
    # journal strings share the pipe; keep only reply events, until end
    events = []
    while True:
        item = sub.get(timeout=5)
        if isinstance(item, dict) and item["event"] == "reply":
            events.append(json.loads(item["data"]))
            if events[-1]["type"] == "reply_end":
                return events


def test_reply_worker_streams_the_lifecycle(monkeypatch):
    monkeypatch.setattr(hb, "stream_llm", lambda *a, **k: iter(["she ", "speaks"]))
    sub = hb.BROADCAST.subscribe()
    try:
        _worker().put(("hello", False))
        events = _next_lifecycle(sub)
        assert events[0]["type"] == "reply_start"
        assert events[0]["initiated"] is False
        assert [e["text"] for e in events if e["type"] == "reply_delta"] == ["she ", "speaks"]
        assert events[0]["id"] == events[-1]["id"]
    finally:
        hb.BROADCAST.unsubscribe(sub)


def test_reply_worker_survives_a_dead_model(monkeypatch):
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("model down")
        return iter(["back"])

    monkeypatch.setattr(hb, "stream_llm", flaky)
    sub = hb.BROADCAST.subscribe()
    try:
        replies = _worker()
        replies.put(("first", False))
        replies.put(("second", False))
        died = _next_lifecycle(sub)
        # the client that saw reply_start is released even though the model died
        assert [e["type"] for e in died] == ["reply_start", "reply_end"]
        lived = _next_lifecycle(sub)
        # and the worker is still standing for the next message
        assert [e["text"] for e in lived if e["type"] == "reply_delta"] == ["back"]
        assert lived[0]["id"] != died[0]["id"]
    finally:
        hb.BROADCAST.unsubscribe(sub)


# ── L1: she can want, and a broken wanting is silence ─────────
# The judgment comes from a small model, so the only thing worth testing
# is what happens when it misbehaves. Every one of these runs without a
# network call: the model is monkeypatched, always.


def _judge(monkeypatch, raw: str) -> tuple[float, str, str]:
    monkeypatch.setattr(hb, "SOUL", "她是小夜")
    monkeypatch.setattr(hb, "call_llm", lambda *a, **k: raw)
    return hb.intention(open_state(), NOON)


def test_a_clean_judgment_is_read(monkeypatch):
    raw = '{"urge": 0.7, "kind": "curious", "reason": "想问问他在忙什么"}'
    assert _judge(monkeypatch, raw) == (0.7, "curious", "想问问他在忙什么")


def test_a_fenced_judgment_is_still_read(monkeypatch):
    # small models wrap JSON in markdown fences nobody asked for
    raw = '```json\n{"urge": 0.4, "kind": "care", "reason": "有点惦记"}\n```'
    assert _judge(monkeypatch, raw) == (0.4, "care", "有点惦记")


@pytest.mark.parametrize(
    "raw,why",
    [
        ("not json at all", "prose"),
        ("", "empty"),
        ("{", "truncated"),
        ('{"urge": "high", "kind": "curious", "reason": "x"}', "urge is not a number"),
        ('{"kind": "curious", "reason": "x"}', "no urge at all"),
        ('{"urge": 0.9, "kind": "想说话", "reason": "x"}', "off-schema kind"),
        ('{"urge": 0.9, "kind": null, "reason": "x"}', "null kind"),
        ('["urge", 0.9]', "right types, wrong shape"),
        ("null", "valid JSON, not an object"),
    ],
)
def test_a_broken_judgment_is_silence(monkeypatch, raw, why):
    # the direction of failure is the whole safety property: unreadable
    # must collapse to silence, never to a default-to-speaking
    urge, kind, _reason = _judge(monkeypatch, raw)
    assert urge == 0.0, why
    assert kind == "none", why


def test_urge_is_clamped_to_its_range(monkeypatch):
    assert _judge(monkeypatch, '{"urge": 1.7, "kind": "share", "reason": "x"}')[0] == 1.0
    assert _judge(monkeypatch, '{"urge": -2, "kind": "share", "reason": "x"}')[0] == 0.0


def test_a_dead_l1_model_is_silence_not_a_crash(monkeypatch):
    def down(*_a, **_k):
        raise RuntimeError("flash model is down")

    monkeypatch.setattr(hb, "SOUL", "她是小夜")
    monkeypatch.setattr(hb, "call_llm", down)
    urge, kind, _reason = hb.intention(open_state(), NOON)
    assert (urge, kind) == (0.0, "none")


def test_no_soul_means_no_judgment_and_no_bill(monkeypatch):
    spent = {"calls": 0}

    def must_not_run(*_a, **_k):
        spent["calls"] += 1
        return '{"urge": 1.0, "kind": "share", "reason": "x"}'

    monkeypatch.setattr(hb, "SOUL", "")
    monkeypatch.setattr(hb, "call_llm", must_not_run)
    assert hb.intention(open_state(), NOON) == (0.0, "none", "no soul loaded")
    assert spent["calls"] == 0, "L1 was billed for a tick it had no self to judge from"


def test_the_soul_is_what_l1_judges_from(monkeypatch):
    seen: dict = {}

    def capture(model, messages, **params):
        seen["messages"] = messages
        seen["model"] = model
        return '{"urge": 0.1, "kind": "none", "reason": "没什么想说的"}'

    monkeypatch.setattr(hb, "SOUL", "她是小夜——夜。")
    monkeypatch.setattr(hb, "call_llm", capture)
    hb.intention(open_state(), NOON)

    system, user = seen["messages"]
    assert seen["model"] == hb.CONFIG["l1"]["model"], "L1 must not spend the pro model"
    assert system["role"] == "system" and user["role"] == "user"
    assert "她是小夜——夜。" in system["content"]
    # the action-bias guard is load-bearing; a refactor that drops it turns
    # a quiet daemon into a chatty one and nothing else would catch it
    assert "什么都不做是完全正常的" in system["content"]
    assert "12:00" in user["content"]


def test_tick_forms_a_wanting_but_spends_nothing(monkeypatch, caplog):
    # THE contract of this step: full-strength wanting, zero action. If this
    # ever fails, her mouth got connected a step early.
    spoke = {"calls": 0}

    def l2_must_not_run(*_a, **_k):
        spoke["calls"] += 1
        return iter([])

    monkeypatch.setattr(hb, "SOUL", "她是小夜")
    monkeypatch.setattr(
        hb, "call_llm", lambda *a, **k: '{"urge": 0.99, "kind": "share", "reason": "很想说"}'
    )
    monkeypatch.setattr(hb, "stream_llm", l2_must_not_run)

    s = idle_state()  # must actually reach L1, so past the afterglow window
    with caplog.at_level(logging.INFO, logger="heartbeat"):
        hb.tick(s, NOON)

    assert "L1: urge=0.99 kind=share — 很想说" in caplog.text
    assert spoke["calls"] == 0, "L2 spoke — her mouth is connected a step early"
    assert s.last_proactive == 0.0, "the proactive budget was spent on a judgment"
    assert s.daily_proactive_count == 0, "the daily count moved without her speaking"


def _spy_on_l1(monkeypatch) -> dict:
    asked = {"n": 0}

    def spy(_state, _now):
        asked["n"] += 1
        return 0.9, "share", "很想说"  # a loud wanting, so a leak would show

    monkeypatch.setattr(hb, "intention", spy)
    return asked


def test_l1_is_not_consulted_during_a_conversation(monkeypatch):
    # while she is being talked to the loop ticks every 15s, so asking here
    # would bill four judgments a minute and stall the beat on each — to
    # decide whether to interrupt someone already talking to her.
    asked = _spy_on_l1(monkeypatch)

    talking = open_state()
    talking.in_conversation = True
    talking.last_interaction = NOON.timestamp() - 60  # 1 min quiet: well inside
    hb.tick(talking, NOON)  # the 20 min timeout
    assert asked["n"] == 0, "L1 was billed during a conversation"
    # and the skip really was the flag, not the conversation decaying out
    assert talking.in_conversation is True

    alone = idle_state()
    assert alone.in_conversation is False
    hb.tick(alone, NOON)
    assert asked["n"] == 1, "L1 must still run when she is alone"


def test_l1_is_not_consulted_in_the_afterglow(monkeypatch):
    # the warm tail of a conversation just ended. Ticks run every 60s here,
    # so this is ~30 judgments in half an hour, each proposing she speak
    # again immediately — the one thing her soul says she does not do.
    asked = _spy_on_l1(monkeypatch)
    s = open_state()
    s.in_conversation = False
    s.last_interaction = NOON.timestamp() - 5 * 60  # 5 min, inside the 30
    hb.tick(s, NOON)
    assert asked["n"] == 0, "L1 was billed during the afterglow"


def test_l1_is_consulted_once_she_is_genuinely_idle(monkeypatch):
    asked = _spy_on_l1(monkeypatch)
    hb.tick(idle_state(), NOON)
    assert asked["n"] == 1


def test_the_afterglow_boundary_is_the_one_next_interval_uses(monkeypatch):
    # the two must agree on what "afterglow" means, or the loop will tick at
    # the afterglow rate while L1 believes she is alone. Walked either side
    # of the real config value rather than a hardcoded 30.
    asked = _spy_on_l1(monkeypatch)
    window = H["afterglow_window_min"]

    inside = open_state()
    inside.last_interaction = NOON.timestamp() - (window - 1) * 60
    hb.tick(inside, NOON)
    assert asked["n"] == 0
    assert hb.next_interval(inside, NOON) == H["tick_afterglow"], "not actually afterglow"

    outside = open_state()
    outside.last_interaction = NOON.timestamp() - (window + 1) * 60
    hb.tick(outside, NOON)
    assert asked["n"] == 1
    assert hb.next_interval(outside, NOON) != H["tick_afterglow"], "still afterglow"


def test_a_decayed_conversation_lands_in_afterglow_not_at_l1(monkeypatch):
    # this reverses the earlier behaviour, deliberately: the tick that ends a
    # conversation used to reach L1 as "she is alone again". It no longer
    # does — 20 min of quiet is inside the 30 min window, so she is still in
    # the warm tail. She reaches out from deep solitude, not from this.
    asked = _spy_on_l1(monkeypatch)
    s = open_state()
    s.in_conversation = True
    s.last_interaction = NOON.timestamp() - H["conversation_timeout_min"] * 60
    hb.tick(s, NOON)
    assert s.in_conversation is False, "the decay itself must still happen"
    assert asked["n"] == 0
    assert H["conversation_timeout_min"] < H["afterglow_window_min"], (
        "a conversation now times out INTO the afterglow; if this ordering is "
        "ever inverted, decayed conversations reach L1 again and this test lies"
    )


def test_a_blocked_tick_never_reaches_l1(monkeypatch):
    # the cascade IS the budget: if L0 stops paying for L1, this file's
    # cost model is wrong and the daemon gets expensive quietly
    spent = {"calls": 0}

    def must_not_run(*_a, **_k):
        spent["calls"] += 1
        return '{"urge": 1.0, "kind": "share", "reason": "x"}'

    monkeypatch.setattr(hb, "SOUL", "她是小夜")
    monkeypatch.setattr(hb, "call_llm", must_not_run)
    s = open_state()
    s.last_proactive = NOON.timestamp() - 60  # inside cooldown → L0 blocks
    hb.tick(s, NOON)
    assert spent["calls"] == 0


# ── L1 → L2: the last link, and the restraint that closes it ──
# These run a real reply_worker against a stubbed stream, so the whole
# path is exercised — dispatch, lifecycle, the worker's report back, and
# the loop spending the budget. No pro tokens are spent.

THRESHOLD = hb.CONFIG["intention"]["urge_threshold"]


def _voice(monkeypatch, chunks=("嗯，", "刚想到你"), dead=False):
    """A live worker with a stubbed L2 stream. Returns (replies, spoken)."""
    seen: dict = {}

    def fake_stream(model, messages, **params):
        seen["model"] = model
        seen["messages"] = messages
        if dead:
            raise RuntimeError("pro model is down")
        return iter(chunks)

    monkeypatch.setattr(hb, "SOUL", "她是小夜")
    monkeypatch.setattr(hb, "stream_llm", fake_stream)
    replies: queue.Queue = queue.Queue()
    spoken: queue.Queue = queue.Queue()
    threading.Thread(target=hb.reply_worker, args=(replies, spoken), daemon=True).start()
    return replies, spoken, seen


def _wants(monkeypatch, urge: float, kind: str = "care", reason: str = "有点惦记他"):
    monkeypatch.setattr(hb, "intention", lambda *_a, **_k: (urge, kind, reason))


def test_a_wanted_message_is_spoken_and_spends_the_budget(monkeypatch):
    _wants(monkeypatch, 0.9)
    replies, spoken, seen = _voice(monkeypatch)
    sub = hb.BROADCAST.subscribe()
    try:
        s = idle_state()
        hb.tick(s, NOON, replies, spoken)
        events = _next_lifecycle(sub)
    finally:
        hb.BROADCAST.unsubscribe(sub)

    # she opened, and the room was told it was her own doing
    assert events[0]["initiated"] is True
    assert "".join(e["text"] for e in events if e["type"] == "reply_delta") == "嗯，刚想到你"
    assert seen["model"] == hb.CONFIG["l2"]["model"], "a reach-out must use her real voice"
    # …and the budget moved, on the loop thread, exactly once
    assert s.daily_proactive_count == 1
    assert s.last_proactive == NOON.timestamp()


def test_the_opening_carries_l1s_seed_and_says_she_is_opening(monkeypatch):
    _wants(monkeypatch, 0.9, kind="curious", reason="想问他今天忙完了没有")
    replies, spoken, seen = _voice(monkeypatch)
    hb.tick(idle_state(), NOON, replies, spoken)

    system, user = seen["messages"]
    assert system["content"] == "她是小夜", "a reach-out is grounded like any other turn"
    assert "想问他今天忙完了没有" in user["content"], "L1's reason must seed L2"
    assert "没有人在跟你说话" in user["content"], "she must know she is opening, not answering"
    assert "在吗" in user["content"], "the one opener she must not use is named explicitly"


def test_a_weak_wanting_is_not_spoken(monkeypatch):
    _wants(monkeypatch, THRESHOLD - 0.01)
    replies, spoken, _seen = _voice(monkeypatch)
    s = idle_state()
    hb.tick(s, NOON, replies, spoken)
    assert replies.empty(), "she spoke below the threshold"
    assert s.last_proactive == 0.0
    assert s.daily_proactive_count == 0


def test_a_shapeless_wanting_is_not_spoken(monkeypatch, caplog):
    # "strongly wants to say nothing in particular" — a judgment that
    # contradicts itself. She would reach L2 with the fallback seed and
    # nothing to seed, and produce an opener written to fill a silence.
    _wants(monkeypatch, 0.9, kind="none", reason="没什么特别的")
    replies, spoken, _seen = _voice(monkeypatch)
    s = idle_state()
    with caplog.at_level(logging.INFO, logger="heartbeat"):
        hb.tick(s, NOON, replies, spoken)

    assert replies.empty(), "she spoke with nothing to say"
    assert s.last_proactive == 0.0
    assert s.daily_proactive_count == 0
    # it still happened, and the journal still saw it happen
    assert "L1: urge=0.90 kind=none — 没什么特别的" in caplog.text


def test_a_shaped_wanting_at_the_same_urge_is_spoken(monkeypatch):
    # the control for the test above: same urge, same everything, only the
    # kind differs — so the guard is what made the difference, not the urge
    _wants(monkeypatch, 0.9, kind="share", reason="想跟他说一件事")
    replies, spoken, _seen = _voice(monkeypatch)
    s = idle_state()
    hb.tick(s, NOON, replies, spoken)
    assert s.daily_proactive_count == 1


def test_the_threshold_itself_is_enough(monkeypatch):
    # >=, not > — pinned because the boundary is a one-character decision
    _wants(monkeypatch, THRESHOLD)
    replies, spoken, _seen = _voice(monkeypatch)
    s = idle_state()
    hb.tick(s, NOON, replies, spoken)
    assert s.daily_proactive_count == 1


def test_a_failed_reach_out_spends_nothing(monkeypatch):
    # the model dies mid-dispatch. She said nothing, so it must not cost her
    # the next 90 minutes — an outage is not a conversation.
    _wants(monkeypatch, 0.9)
    replies, spoken, _seen = _voice(monkeypatch, dead=True)
    sub = hb.BROADCAST.subscribe()
    try:
        s = idle_state()
        hb.tick(s, NOON, replies, spoken)
        events = _next_lifecycle(sub)
    finally:
        hb.BROADCAST.unsubscribe(sub)

    assert [e["type"] for e in events] == ["reply_start", "reply_end"], "lifecycle must close"
    assert s.last_proactive == 0.0, "a reach-out that never landed spent her cooldown"
    assert s.daily_proactive_count == 0, "…and a quarter of her daily budget"


def test_she_cannot_reach_out_twice_in_a_row(monkeypatch):
    # the loop closing: speaking is what silences her. Without this the
    # 0.9 urge below would fire on every idle beat, forever.
    _wants(monkeypatch, 0.9)
    replies, spoken, _seen = _voice(monkeypatch)
    s = idle_state()

    hb.tick(s, NOON, replies, spoken)
    assert s.daily_proactive_count == 1

    allowed, why = hb.gate(s, NOON)
    assert not allowed and why.startswith("cooldown"), why
    hb.tick(s, NOON, replies, spoken)  # same wanting, one beat later
    assert s.daily_proactive_count == 1, "she spoke twice in a row"


def test_the_daily_cap_ends_the_day(monkeypatch):
    _wants(monkeypatch, 0.9)
    replies, spoken, _seen = _voice(monkeypatch)
    s = idle_state()
    s.daily_proactive_count = G["daily_proactive_max"]
    # the date must be today's, or roll_day() — which tick() runs before the
    # gate — resets the very cap this test is about, and she speaks freely
    s.daily_count_date = NOON.strftime("%Y-%m-%d")
    hb.tick(s, NOON, replies, spoken)
    assert replies.empty(), "she spoke past her daily cap"
    assert s.daily_proactive_count == G["daily_proactive_max"]


def test_a_report_the_wait_missed_is_settled_on_the_next_beat(monkeypatch):
    # the rescue path: if the worker's report arrives after the bounded wait
    # gave up, the next beat must still spend it — otherwise a slow model
    # buys her a message that costs nothing, and she can fire again at once.
    _wants(monkeypatch, 0.9)
    s = idle_state()
    spoken: queue.Queue = queue.Queue()
    spoken.put(True)  # a report that landed too late to be waited on
    hb.tick(s, NOON, queue.Queue(), spoken)
    assert s.daily_proactive_count >= 1
    assert s.last_proactive == NOON.timestamp()


def test_the_bounded_wait_cannot_outlast_the_beat():
    # _settle_proactive waits llm.timeout_s after dispatching. If that ever
    # exceeds the shortest interval at which a proactive can be attempted,
    # two dispatches could overlap and the single-voice guarantee breaks.
    shortest_l1_tick = min(H["tick_activity"], H["tick_idle"] - H["tick_jitter"])
    assert hb.CONFIG["llm"]["timeout_s"] < shortest_l1_tick


def test_answering_a_message_never_spends_the_proactive_budget(monkeypatch):
    # the whole point of the budget is to limit her speaking FIRST. If a
    # reply reported itself as proactive, a chatty evening would exhaust it.
    _wants(monkeypatch, 0.0)  # she wants nothing this beat; only the reply matters
    replies, spoken, _seen = _voice(monkeypatch)
    replies.put(("你在吗", False))  # a user message, not her own turn
    sub = hb.BROADCAST.subscribe()
    try:
        _next_lifecycle(sub)
    finally:
        hb.BROADCAST.unsubscribe(sub)
    s = idle_state()
    hb.tick(s, NOON, queue.Queue(), spoken)
    assert s.daily_proactive_count == 0
    assert s.last_proactive == 0.0


# ── the structured heartbeat: one tick event per beat, every path ─────
# The strip is a projection of real state, so every outcome tick() can reach
# emits exactly one named "tick" event — never inferred from log wording
# again. Each path here returns synchronously, so the beat's tick is already
# on the pipe by the time tick() returns; journal strings and any reply
# events are filtered out.


def _tick_events(sub: queue.Queue) -> list[dict]:
    out: list[dict] = []
    while True:
        try:
            item = sub.get_nowait()
        except queue.Empty:
            return out
        if isinstance(item, dict) and item["event"] == "tick":
            out.append(json.loads(item["data"]))


def _one_tick(sub: queue.Queue) -> dict:
    events = _tick_events(sub)
    assert len(events) == 1, f"a beat must emit exactly one tick, got {events}"
    return events[0]


def test_a_blocked_beat_emits_one_intercepted_tick():
    s = open_state()
    s.presence = "sleeping"  # L0 refuses at the gate
    sub = hb.BROADCAST.subscribe()
    try:
        hb.tick(s, NOON)
        ev = _one_tick(sub)
    finally:
        hb.BROADCAST.unsubscribe(sub)
    assert ev["tag"] == "intercepted"
    # content-free by construction: the pipe never learns more than this
    assert set(ev) == {"tag", "at"}


def test_a_beat_in_conversation_emits_one_skipped_tick():
    s = open_state()
    s.in_conversation = True
    s.last_interaction = NOON.timestamp()  # fresh — does not time out
    sub = hb.BROADCAST.subscribe()
    try:
        hb.tick(s, NOON)  # gate opens, then L1 is skipped before it is asked
        ev = _one_tick(sub)
    finally:
        hb.BROADCAST.unsubscribe(sub)
    assert ev["tag"] == "skipped"


def test_a_beat_in_afterglow_also_emits_skipped():
    # the other before-L1 short-circuit shares the tag: still not asked
    s = open_state()
    s.last_interaction = NOON.timestamp() - 60  # 1 min ago, inside afterglow
    sub = hb.BROADCAST.subscribe()
    try:
        hb.tick(s, NOON)
        ev = _one_tick(sub)
    finally:
        hb.BROADCAST.unsubscribe(sub)
    assert ev["tag"] == "skipped"


def test_a_quiet_judgment_emits_one_silent_tick(monkeypatch):
    _wants(monkeypatch, THRESHOLD - 0.01)  # asked, chose not to speak
    sub = hb.BROADCAST.subscribe()
    try:
        hb.tick(idle_state(), NOON, queue.Queue(), queue.Queue())
        ev = _one_tick(sub)
    finally:
        hb.BROADCAST.unsubscribe(sub)
    assert ev["tag"] == "silent"


def test_a_reach_out_emits_one_spoke_tick(monkeypatch):
    _wants(monkeypatch, 0.9, kind="share")
    # stub the settle so the beat neither blocks on a worker nor puts reply
    # events on the pipe; the spoke tick is emitted at dispatch regardless
    monkeypatch.setattr(hb, "_settle_proactive", lambda *_a, **_k: None)
    replies: queue.Queue = queue.Queue()
    spoken: queue.Queue = queue.Queue()
    sub = hb.BROADCAST.subscribe()
    try:
        hb.tick(idle_state(), NOON, replies, spoken)
        ev = _one_tick(sub)
    finally:
        hb.BROADCAST.unsubscribe(sub)
    assert ev["tag"] == "spoke"
    assert replies.qsize() == 1, "she really was dispatched to L2"


def test_a_beat_that_judged_without_a_mouth_emits_no_tick(monkeypatch):
    # the wiring-fault branch: she cleared L1 but no voice is wired. The real
    # daemon can never reach it (main() always wires both queues), so it must
    # not put a phantom beat on the strip — tick-count stays == heartbeat-count.
    _wants(monkeypatch, 0.9, kind="share")
    sub = hb.BROADCAST.subscribe()
    try:
        hb.tick(idle_state(), NOON)  # no replies / spoken
        events = _tick_events(sub)
    finally:
        hb.BROADCAST.unsubscribe(sub)
    assert events == []


# ── the debug backdoor: the whole chain, on demand ────────────
# POST /debug/intention fires one L1→L2 evaluation now so a tester need not
# wait for an idle beat. These go in through the real endpoint (TestClient),
# so the request→loop→response round trip and the real chain are both
# exercised — the loop-side handler is the same run_debug_intention a running
# daemon calls. No pro tokens: L1 is stubbed, L2 is a stubbed stream.


def _debug_loop(inbox, state, replies, spoken, stop, now):
    """
    Stands in for main()'s inbox dispatch of a DebugIntention: pull the
    trigger, call the REAL run_debug_intention on this thread, post the
    summary back. `now` is injected (and fixed) so the gate and the
    last_proactive the settle stamps are deterministic — the same clock
    discipline the rest of the file follows.
    """
    while not stop.is_set():
        try:
            msg = inbox.get(timeout=0.05)
        except queue.Empty:
            continue
        if isinstance(msg, hb.DebugIntention):
            msg.result.put(hb.run_debug_intention(state, now, replies, spoken, msg.force))


def _debug_client(monkeypatch, state, urge, kind, reason, now=NOON):
    """A live L2 worker, a loop stand-in draining the inbox, and the real
    endpoint in front of both. Returns (client, stop) — caller sets stop."""
    _wants(monkeypatch, urge, kind, reason)
    replies, spoken, _seen = _voice(monkeypatch)
    inbox: queue.Queue = queue.Queue()
    stop = threading.Event()
    threading.Thread(
        target=_debug_loop, args=(inbox, state, replies, spoken, stop, now), daemon=True
    ).start()
    return TestClient(hb.build_api(state, inbox)), stop


def test_debug_endpoint_speaks_on_a_shaped_wanting(monkeypatch):
    # the plain case: a shaped, over-threshold wanting → she speaks through
    # the real path, and the budget moves on the loop thread exactly once.
    s = idle_state()
    client, stop = _debug_client(monkeypatch, s, 0.9, "share", "想跟他说一件事")
    try:
        data = client.post("/debug/intention").json()
    finally:
        stop.set()

    assert data["spoke"] is True
    assert data["forced"] is False, "nothing was bypassed — the gate was open"
    assert data["kind"] == "share"
    # the counter spend happened on the loop thread, via the real settle
    assert s.daily_proactive_count == 1
    assert s.last_proactive == NOON.timestamp()


def test_debug_endpoint_stays_silent_on_a_shapeless_wanting(monkeypatch):
    # the kind guard holds through the backdoor too: high urge, kind "none"
    # → she does not speak, and nothing is spent.
    s = idle_state()
    client, stop = _debug_client(monkeypatch, s, 0.9, "none", "没什么特别的")
    try:
        data = client.post("/debug/intention").json()
    finally:
        stop.set()

    assert data["spoke"] is False
    assert data["why"] == "below threshold or shapeless"
    assert s.daily_proactive_count == 0
    assert s.last_proactive == 0.0


def test_debug_endpoint_default_respects_the_cooldown(monkeypatch):
    # honest by default: on cooldown and unforced, she reports blocked and
    # stays silent — exactly as a real tick would, and urge is null because
    # no L1 judgment was billed for a beat L0 had already closed.
    s = idle_state()
    s.last_proactive = NOON.timestamp() - 60  # 1 min ago, cooldown is 90
    client, stop = _debug_client(monkeypatch, s, 0.9, "share", "想说")
    try:
        data = client.post("/debug/intention").json()
    finally:
        stop.set()

    assert data["spoke"] is False
    assert data["forced"] is False
    assert data["why"].startswith("gate: cooldown")
    assert data["urge"] is None, "L1 was billed for a beat the gate had closed"
    assert s.daily_proactive_count == 0


def test_debug_endpoint_force_bypasses_the_cooldown(monkeypatch):
    # ?force=1 lifts the 90-min cooldown so a tester can fire again at once.
    # she still runs the real L1 and speaks through the real chain, the
    # budget still moves so state stays coherent, and the bypass is loud.
    s = idle_state()
    s.last_proactive = NOON.timestamp() - 60  # would block an organic shot
    client, stop = _debug_client(monkeypatch, s, 0.9, "share", "想说")
    try:
        data = client.post("/debug/intention?force=1").json()
    finally:
        stop.set()

    assert data["spoke"] is True
    assert data["forced"] is True, "a forced shot must never read as organic"
    assert data["urge"] == 0.9, "force must still run the real judgment"
    assert s.daily_proactive_count == 1
    assert s.last_proactive == NOON.timestamp()


def test_debug_endpoint_force_never_wakes_her_at_night(monkeypatch):
    # the one bypass force must NOT grant: DND. force lifts the budget, never
    # the hard floors, so a 3am shot stays blocked even when forced.
    s = idle_state()
    night = datetime(2026, 7, 17, 3, 0, 0)  # inside the DND window
    client, stop = _debug_client(monkeypatch, s, 0.9, "share", "想说", now=night)
    try:
        data = client.post("/debug/intention?force=1").json()
    finally:
        stop.set()

    assert data["spoke"] is False
    assert data["forced"] is False, "force lifted a hard floor it must never lift"
    assert data["why"] == "gate: dnd"
    assert s.daily_proactive_count == 0


# ── the ember backdoor: a FABRICATED reach-out ────────────────
# POST /debug/speak forces one self-initiated utterance past L1 entirely, so
# the frontend "she reached out" ember can be tested on demand. These go in
# through the real endpoint (TestClient) against a live worker + fake store, so
# the request→loop→worker→DB round trip is exercised. L2 is a stubbed stream.


def _speak_loop(inbox, state, replies, spoken, stop, now):
    """main()'s DebugSpeak dispatch, stood up on its own thread: pull the
    trigger, run the REAL run_debug_speak on this thread, post the summary
    back. `now` is fixed so the gate is deterministic."""
    while not stop.is_set():
        try:
            msg = inbox.get(timeout=0.05)
        except queue.Empty:
            continue
        if isinstance(msg, hb.DebugSpeak):
            msg.result.put(hb.run_debug_speak(state, now, replies, spoken, msg.kind, msg.reason))


def _speak_client(monkeypatch, state, store, now=NOON, chunks=("嗯，", "我在")):
    """A live worker writing to `store`, a loop stand-in draining the inbox,
    and the real endpoint in front of both. Returns (client, stop)."""

    def fake_stream(model, messages, **params):
        return iter(chunks)

    monkeypatch.setattr(hb, "SOUL", "她是小夜")
    monkeypatch.setattr(hb, "stream_llm", fake_stream)
    replies: queue.Queue = queue.Queue()
    spoken: queue.Queue = queue.Queue()
    threading.Thread(target=hb.reply_worker, args=(replies, spoken, store), daemon=True).start()
    inbox: queue.Queue = queue.Queue()
    stop = threading.Event()
    threading.Thread(
        target=_speak_loop, args=(inbox, state, replies, spoken, stop, now), daemon=True
    ).start()
    return TestClient(hb.build_api(state, inbox)), stop


def test_debug_speak_fabricates_a_self_initiated_reach_out(monkeypatch):
    # the whole point: force her to speak first with NO L1 judgment, stream
    # self=true through the real worker, persist a 'her' row — and, a test
    # artifact, leave her real proactive budget untouched.
    store = _FakeStore()
    s = idle_state()
    client, stop = _speak_client(monkeypatch, s, store)
    sub = hb.BROADCAST.subscribe()
    try:
        data = client.post("/debug/speak?kind=care&reason=想问他睡了没").json()
        events = _next_lifecycle(sub)
    finally:
        hb.BROADCAST.unsubscribe(sub)
        stop.set()

    assert data["spoke"] is True
    assert data["kind"] == "care"
    assert data["text_will_stream"] is True
    assert "fabricated" in data["note"]
    # it dispatched initiated=True through the worker path — the reply streamed
    # marked self, which is what triggers the ember
    assert events[0]["type"] == "reply_start"
    assert events[0]["initiated"] is True, "a fabricated reach-out must stream self=true"
    # her words persist as a 'her' row; the seed is internal, never a 'user' row
    assert store.rows == [("her", "嗯，我在")]
    # …and her real rhythm is untouched — no cooldown spent, no daily count
    assert s.last_proactive == 0.0
    assert s.daily_proactive_count == 0


def test_debug_speak_bypasses_cooldown_without_spending_it(monkeypatch):
    # on cooldown an organic proactive is blocked; the fabricated shot fires
    # anyway (bypass) AND leaves the cooldown exactly where it was.
    store = _FakeStore()
    s = idle_state()
    s.last_proactive = NOON.timestamp() - 60  # 1 min ago; cooldown is 90
    client, stop = _speak_client(monkeypatch, s, store)
    try:
        data = client.post("/debug/speak").json()
    finally:
        stop.set()

    assert data["spoke"] is True
    assert data["bypassed_budget"] is True
    assert store.rows == [("her", "嗯，我在")]
    # bypassed, not consumed: last_proactive is exactly what it was
    assert s.last_proactive == NOON.timestamp() - 60
    assert s.daily_proactive_count == 0


def test_debug_speak_refuses_at_night(monkeypatch):
    # the one thing it must NOT do: fabricate speech during DND. The budget is
    # bypassed; the hard floors are not.
    store = _FakeStore()
    s = idle_state()
    night = datetime(2026, 7, 17, 3, 0, 0)  # inside the DND window
    client, stop = _speak_client(monkeypatch, s, store, now=night)
    try:
        data = client.post("/debug/speak").json()
    finally:
        stop.set()

    assert data["spoke"] is False
    assert data["text_will_stream"] is False
    assert "dnd" in data["note"]
    assert store.rows == [], "nothing was fabricated at night"


# ── memory: a sense of "lately" ───────────────────────────────
# The smallest loop that lets a wanting accumulate: judgments logged raw,
# digested into daily reflections, fed back into the next judgment. The model
# is always stubbed — digestion is summarization, but not on real tokens.


def _urge_line(at, urge=0.2, kind="none", reason="没什么特别想说的") -> dict:
    return {
        "at": at,
        "iso": datetime.fromtimestamp(at).isoformat(timespec="seconds"),
        "urge": urge,
        "kind": kind,
        "reason": reason,
    }


def _seed_urges(ats) -> None:
    path = hb.MEMORY_DIR / "raw" / "urges.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(_urge_line(a)) + "\n" for a in ats), encoding="utf-8")


def test_a_judgment_is_appended_as_a_jsonl_line(monkeypatch):
    # tick() records every judgment it logs. One tick → one line, and the line
    # is real JSON carrying the fields digestion will later read.
    monkeypatch.setattr(hb, "SOUL", "她是小夜")
    monkeypatch.setattr(
        hb, "call_llm", lambda *a, **k: '{"urge": 0.2, "kind": "none", "reason": "在看paper"}'
    )
    hb.tick(idle_state(), NOON)  # no voice wired: it judges, logs, records, stops

    lines = (hb.MEMORY_DIR / "raw" / "urges.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert (rec["urge"], rec["kind"], rec["reason"]) == (0.2, "none", "在看paper")
    assert rec["at"] == NOON.timestamp()
    assert rec["iso"] == NOON.isoformat(timespec="seconds")


def test_digest_with_nothing_new_returns_zero(monkeypatch):
    # no urges at all, so nothing to compress — and no model call is made
    monkeypatch.setattr(hb, "SOUL", "她是小夜")
    assert hb.run_digest(NOON) == {"digested": 0, "why": "nothing new since last digest"}


def test_digest_writes_a_daily_file_and_advances_the_watermark(monkeypatch):
    reflection = "这一天大多是安静的，只在傍晚有点想问问他。"
    monkeypatch.setattr(hb, "SOUL", "她是小夜")
    monkeypatch.setattr(hb, "call_llm", lambda *a, **k: reflection)
    base = NOON.timestamp()
    ats = [base - 3600, base - 1800, base - 60]
    _seed_urges(ats)

    result = hb.run_digest(NOON)
    assert result["digested"] == 3
    assert result["reflection"] == reflection
    day = datetime.fromtimestamp(ats[-1]).strftime("%Y-%m-%d")
    assert result["day"] == day
    assert result["span"] == [_urge_line(ats[0])["iso"], _urge_line(ats[-1])["iso"]]

    # a per-day file exists and holds the reflection…
    daily = hb.MEMORY_DIR / "daily" / f"{day}.md"
    assert result["reflection"] in daily.read_text(encoding="utf-8")
    # …and the watermark advanced to the latest 'at' just digested
    assert float((hb.MEMORY_DIR / ".watermark").read_text().strip()) == ats[-1]


def test_a_second_digest_right_after_finds_nothing_new(monkeypatch):
    # the watermark is the whole contract: digest, then digest again with no
    # new judgments, and the second must find nothing AND spend no model call.
    monkeypatch.setattr(hb, "SOUL", "她是小夜")
    calls = {"n": 0}

    def flash(*_a, **_k):
        calls["n"] += 1
        return "安静的一天。"

    monkeypatch.setattr(hb, "call_llm", flash)
    _seed_urges([NOON.timestamp() - 60])

    first = hb.run_digest(NOON)
    assert first["digested"] == 1
    second = hb.run_digest(NOON)
    assert second == {"digested": 0, "why": "nothing new since last digest"}
    assert calls["n"] == 1, "the empty second digest still billed a model call"


def test_a_new_day_appends_a_section_it_never_overwrites(monkeypatch):
    # a day may be digested in parts. The second part must ADD a section, not
    # erase the first — losing a morning's reflection to digest the afternoon
    # would defeat the entire point of keeping them.
    monkeypatch.setattr(hb, "SOUL", "她是小夜")
    base = NOON.timestamp()
    day = datetime.fromtimestamp(base).strftime("%Y-%m-%d")
    daily = hb.MEMORY_DIR / "daily" / f"{day}.md"

    monkeypatch.setattr(hb, "call_llm", lambda *a, **k: "上午在读论文，安静。")
    _seed_urges([base - 7200])
    hb.run_digest(NOON)
    first_text = daily.read_text(encoding="utf-8")

    monkeypatch.setattr(hb, "call_llm", lambda *a, **k: "下午有点想他。")
    _seed_urges([base - 7200, base - 60])  # the earlier line is now below the watermark
    hb.run_digest(NOON)
    both = daily.read_text(encoding="utf-8")

    assert first_text in both, "the earlier section was overwritten"
    assert "上午在读论文，安静。" in both
    assert "下午有点想他。" in both


def _l1_user_message(monkeypatch) -> str:
    seen: dict = {}

    def capture(_model, messages, **_k):
        seen["messages"] = messages
        return '{"urge": 0.1, "kind": "none", "reason": "x"}'

    monkeypatch.setattr(hb, "SOUL", "她是小夜")
    monkeypatch.setattr(hb, "call_llm", capture)
    hb.intention(open_state(), NOON)
    return seen["messages"][1]["content"]


def test_l1_context_includes_the_recent_block_when_it_exists(monkeypatch):
    # a daily reflection on disk → L1 sees a 最近 block BEFORE 当前情况, which
    # is the feedback that lets a wanting build across days
    (hb.MEMORY_DIR / "daily").mkdir(parents=True, exist_ok=True)
    (hb.MEMORY_DIR / "daily" / "2026-07-20.md").write_text(
        "## 2026-07-20T22:00:00（3 条判断）\n\n这几天很安静，一直在等他消息。\n\n",
        encoding="utf-8",
    )
    user = _l1_user_message(monkeypatch)

    assert user.startswith("最近这些天："), user
    assert "这几天很安静，一直在等他消息。" in user
    assert "2026-07-20" in user, "the day label gives her temporal sense"
    assert "当前情况：" in user, "the original context must still follow the block"
    assert "条判断" not in user, "section-header scaffolding must not be fed to her"


def test_l1_context_omits_the_recent_block_when_there_is_none(monkeypatch):
    # a fresh install has no past. No block, no empty header — just 当前情况.
    user = _l1_user_message(monkeypatch)  # tmp memory dir is empty
    assert user.startswith("当前情况："), user
    assert "最近这些天" not in user


def test_debug_digest_endpoint_round_trips(monkeypatch):
    # the request→loop boundary for digest, proven through the real endpoint:
    # the handler queues a DigestRequest, a loop stand-in runs the REAL
    # run_digest, and the summary comes back over the result queue.
    monkeypatch.setattr(hb, "SOUL", "她是小夜")
    monkeypatch.setattr(hb, "call_llm", lambda *a, **k: "安静的几个小时。")
    _seed_urges([NOON.timestamp() - 60])

    inbox: queue.Queue = queue.Queue()
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                m = inbox.get(timeout=0.05)
            except queue.Empty:
                continue
            if isinstance(m, hb.DigestRequest):
                m.result.put(hb.run_digest(NOON))

    threading.Thread(target=loop, daemon=True).start()
    client = TestClient(hb.build_api(open_state(), inbox))
    try:
        data = client.post("/debug/digest").json()
    finally:
        stop.set()

    assert data["digested"] == 1
    assert data["reflection"] == "安静的几个小时。"


# ── the soul: what L2 is actually sent ────────────────────────
# The one thing worth proving about a system prompt is that it is really
# on the wire. "It looked right in the log" is not evidence — the log is
# deliberately forbidden from containing it.


def _messages_sent(
    monkeypatch, soul: str, msg: str = "hello", initiated: bool = False
) -> list[dict]:
    seen: dict = {}

    def capture(model, messages, **params):
        seen["messages"] = messages
        return iter(["嗯"])

    monkeypatch.setattr(hb, "SOUL", soul)
    monkeypatch.setattr(hb, "stream_llm", capture)
    hb.handle_message(msg, initiated)
    return seen["messages"]


def _write_diary(day: str, reflection: str) -> None:
    d = hb.MEMORY_DIR / "daily"
    d.mkdir(parents=True, exist_ok=True)
    section = f"## {day}T22:00:00（2 条判断）\n\n{reflection}\n\n"
    (d / f"{day}.md").write_text(section, encoding="utf-8")


def test_l2_speaks_grounded_in_the_soul(monkeypatch):
    messages = _messages_sent(monkeypatch, "她是小夜")
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == "她是小夜"
    assert messages[1]["content"] == "hello"


def test_l2_runs_without_a_soul_rather_than_with_a_stand_in(monkeypatch):
    # a fresh clone has no save-file. She still answers — ungrounded, and
    # with no empty system message pretending to be who she is.
    assert _messages_sent(monkeypatch, "") == [{"role": "user", "content": "hello"}]


def test_l2_reply_reads_the_recent_diary(monkeypatch):
    # a diary on disk → her reply is grounded in soul AND lately, and the
    # memory rides in the SYSTEM turn, never in what he said
    _write_diary("2026-07-20", "这几天很安静，一直在等他消息。")
    messages = _messages_sent(monkeypatch, "她是小夜", msg="在吗")

    assert [m["role"] for m in messages] == ["system", "user"]
    system = messages[0]["content"]
    assert system.startswith("她是小夜"), "soul stays the backbone, memory is appended under it"
    assert "最近这些天：" in system
    assert "这几天很安静，一直在等他消息。" in system
    assert messages[1]["content"] == "在吗", "memory must not leak into the user turn"


def test_l2_reply_without_a_diary_is_byte_identical(monkeypatch):
    # no diary → exactly today's no-memory prompt: soul alone, no 最近 header
    assert _messages_sent(monkeypatch, "她是小夜") == [
        {"role": "system", "content": "她是小夜"},
        {"role": "user", "content": "hello"},
    ]


def test_l2_proactive_opening_reads_the_recent_diary(monkeypatch):
    # the self-initiated path shares handle_message, so reaching out she also
    # already knows what her recent days were like
    _write_diary("2026-07-20", "一直在等他消息。")
    opening = hb.opening_prompt("care", "有点惦记他")
    messages = _messages_sent(monkeypatch, "她是小夜", msg=opening, initiated=True)

    system = messages[0]["content"]
    assert system.startswith("她是小夜")
    assert "最近这些天：" in system
    assert "一直在等他消息。" in system
    assert messages[1]["content"] == opening, "the opening seed stays the user turn"


def test_l2_proactive_opening_without_a_diary_is_byte_identical(monkeypatch):
    # the proactive path, no diary → soul alone, same as before memory existed
    opening = hb.opening_prompt("care", "有点惦记他")
    messages = _messages_sent(monkeypatch, "她是小夜", msg=opening, initiated=True)
    assert messages[0] == {"role": "system", "content": "她是小夜"}
    assert messages[1] == {"role": "user", "content": opening}


def test_the_recent_memory_never_reaches_the_journal(monkeypatch, caplog):
    # memory gets the soul's discipline: to the model and nowhere else — not
    # the INFO journal, not the SSE pipe the web room reads
    secret = "她昨天偷偷去了猫咖，没跟任何人说"
    _write_diary("2026-07-20", secret)
    sub = hb.BROADCAST.subscribe()
    try:
        with caplog.at_level(logging.INFO, logger="heartbeat"):
            _messages_sent(monkeypatch, "她是小夜")
        assert caplog.text, "nothing was logged at all — the check would pass vacuously"
        assert secret not in caplog.text
        published = []
        while not sub.empty():
            published.append(sub.get_nowait())
        assert published, "nothing was broadcast at all — the check would pass vacuously"
        assert all(secret not in str(item) for item in published)
    finally:
        hb.BROADCAST.unsubscribe(sub)


def test_a_missing_soul_file_reads_as_no_soul(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "SOUL_PATH", tmp_path / "nothing-here.md")
    assert hb.load_soul() == ""


def test_the_soul_never_reaches_the_journal(monkeypatch, caplog):
    # it is the save-file. It goes to the model and nowhere else — not the
    # log file, and not the SSE pipe the web room reads.
    secret = "她和一个人生活在一起"
    sub = hb.BROADCAST.subscribe()
    try:
        with caplog.at_level(logging.INFO, logger="heartbeat"):
            _messages_sent(monkeypatch, secret)
        assert caplog.text, "nothing was logged at all — the check would pass vacuously"
        assert secret not in caplog.text
        published = []
        while not sub.empty():
            published.append(sub.get_nowait())
        assert published, "nothing was broadcast at all — the check would pass vacuously"
        assert all(secret not in str(item) for item in published)
    finally:
        hb.BROADCAST.unsubscribe(sub)


# ── short-term memory: the running conversation (DB) ──────────
# The last N literal turns reach L2 as prior messages. Tested against an
# in-memory fake store (the ConversationStore interface, no Postgres), so CI
# never needs Docker. The model is always stubbed — no tokens.


class _FakeStore:
    """The store interface in memory. `fail=True` makes every op raise, to
    prove a broken DB never crashes a reply or drops it."""

    def __init__(self, fail: bool = False) -> None:
        self.rows: list[tuple[str, str]] = []
        self.fail = fail

    def add(self, role: str, content: str) -> None:
        if self.fail:
            raise RuntimeError("DB down")
        self.rows.append((role, content))

    def recent(self, n: int) -> list[tuple[str, str]]:
        if self.fail:
            raise RuntimeError("DB down")
        return self.rows[-n:]

    def count(self) -> int | None:
        if self.fail:
            raise RuntimeError("DB down")
        return len(self.rows)


def _l2_messages(monkeypatch, store, msg="hello", initiated=False, soul="她是小夜"):
    seen: dict = {}

    def capture(model, messages, **params):
        seen["messages"] = messages
        return iter(["嗯"])

    monkeypatch.setattr(hb, "SOUL", soul)
    monkeypatch.setattr(hb, "stream_llm", capture)
    said = hb.handle_message(msg, initiated, store)
    return seen.get("messages"), said


def test_a_user_turn_and_a_her_turn_persist(monkeypatch):
    store = _FakeStore()
    _l2_messages(monkeypatch, store, msg="你在吗")
    assert store.rows == [("user", "你在吗"), ("her", "嗯")]


def test_a_proactive_opening_stores_her_words_but_not_the_seed(monkeypatch):
    # the opening seed is internal — never a 'user' row; only her reply is hers
    store = _FakeStore()
    _l2_messages(monkeypatch, store, msg=hb.opening_prompt("care", "x"), initiated=True)
    assert store.rows == [("her", "嗯")]


def test_l2_includes_recent_history_in_order_and_mapped_roles(monkeypatch):
    store = _FakeStore()
    store.rows = [("user", "在忙吗"), ("her", "在改代码"), ("user", "顺利吗")]
    messages, _ = _l2_messages(monkeypatch, store, msg="需要帮忙吗")

    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "在忙吗"}
    assert messages[2] == {"role": "assistant", "content": "在改代码"}  # 'her' → assistant
    assert messages[3] == {"role": "user", "content": "顺利吗"}
    assert messages[-1] == {"role": "user", "content": "需要帮忙吗"}  # the new turn, last


def test_history_is_capped_at_the_configured_turns(monkeypatch):
    # more turns in the store than the cap → only the last N reach the prompt
    n = hb.CONFIG["db"]["history_turns"]
    store = _FakeStore()
    store.rows = [("user", f"m{i}") for i in range(n + 5)]
    messages, _ = _l2_messages(monkeypatch, store, msg="now")

    history_msgs = [m for m in messages[1:] if m["content"] != "now"]  # drop system + new turn
    assert len(history_msgs) == n
    assert history_msgs[0]["content"] == "m5", "kept the last N, not the first"


def test_with_no_history_the_prompt_is_byte_identical_to_today(monkeypatch):
    # empty store → exactly soul + the single turn, no history scaffolding
    store = _FakeStore()
    messages, _ = _l2_messages(monkeypatch, store, msg="hello")
    assert messages == [
        {"role": "system", "content": "她是小夜"},
        {"role": "user", "content": "hello"},
    ]
    # the incoming turn was stored AFTER the fetch, so it never doubled into
    # this prompt — and it is there for next time
    assert store.rows == [("user", "hello"), ("her", "嗯")]


def test_a_db_failure_falls_back_without_crashing_or_dropping_the_reply(monkeypatch):
    # the store raises on every op. She still assembles a prompt (no history),
    # still streams, still reports she spoke — the DB enriches, it is not load-
    # bearing for her to exist.
    store = _FakeStore(fail=True)
    messages, said = _l2_messages(monkeypatch, store, msg="hello")
    assert said is True, "a dead DB dropped her reply"
    assert messages == [
        {"role": "system", "content": "她是小夜"},
        {"role": "user", "content": "hello"},
    ]


def test_build_store_without_a_driver_remembers_nothing(monkeypatch):
    monkeypatch.setattr(hb, "_HAS_DB_DRIVER", False)
    store = hb.build_store()
    assert isinstance(store, hb.NullStore)
    assert store.recent(5) == []


def test_build_store_falls_back_when_the_db_is_unreachable(monkeypatch):
    # driver present, but reaching Postgres / creating the schema fails →
    # NullStore, logged, no crash. No live DB needed to prove it.
    monkeypatch.setattr(hb, "_HAS_DB_DRIVER", True)

    def unreachable(_self):
        raise RuntimeError("could not connect to server")

    monkeypatch.setattr(hb.PostgresStore, "ensure_schema", unreachable)
    assert isinstance(hb.build_store(), hb.NullStore)


# ── /state: the conversation-window readout ───────────────────
# /state carries how full the window is so the instrument overlay can show it.
# Only integer counts ride — never content. count_total is the store's own
# count() (its throwaway read connection); here it's a trivial stub, so no DB
# and no worker are needed to prove the arithmetic and the offline fallback.


def _state_client(state, count_total=None):
    """The real /state endpoint with a stubbed window count. No loop, no
    worker — /state is a pure read, so nothing to drain and nothing to stop."""
    return TestClient(hb.build_api(state, queue.Queue(), count_total))


def test_state_reports_window_usage_below_the_cap():
    cap = hb.CONFIG["db"]["history_turns"]
    data = _state_client(idle_state(), count_total=lambda: 14).get("/state").json()
    assert data["history_cap"] == cap
    assert data["history_total"] == 14
    assert data["history_len"] == 14, "below the cap, the window holds every stored turn"


def test_state_caps_window_usage_at_the_configured_turns():
    # more stored than she can see → history_len saturates at the cap, while
    # history_total still reports the whole log behind it.
    cap = hb.CONFIG["db"]["history_turns"]
    data = _state_client(idle_state(), count_total=lambda: cap + 88).get("/state").json()
    assert data["history_len"] == cap
    assert data["history_total"] == cap + 88


def test_state_marks_the_window_offline_when_the_db_is_down():
    # NullStore.count() → None: the readout goes 'offline', never a false zero.
    # The cap is config, so it's still known with the DB down.
    data = _state_client(idle_state(), count_total=lambda: None).get("/state").json()
    assert data["history_len"] is None
    assert data["history_total"] is None
    assert data["history_cap"] == hb.CONFIG["db"]["history_turns"]


def test_state_survives_a_count_that_raises():
    # a count that throws must degrade to offline, never 500 the vitals poll.
    def boom() -> int | None:
        raise RuntimeError("db exploded mid-poll")

    data = _state_client(idle_state(), count_total=boom).get("/state").json()
    assert data["history_len"] is None
    assert data["history_total"] is None


def test_null_store_reports_no_count():
    # the store-layer half of the offline contract, independent of the endpoint
    assert hb.NullStore().count() is None


def test_conversation_content_never_reaches_the_journal(monkeypatch, caplog):
    # the DB rows are the store; the journal is not. His words and her prior
    # ones must not appear in the INFO log or the SSE pipe — the soul's rule.
    store = _FakeStore()
    store.rows = [("user", "他昨天说他要辞职"), ("her", "嗯嗯我记得")]
    incoming = "这是他刚发来的一句私密的话"
    sub = hb.BROADCAST.subscribe()
    try:
        with caplog.at_level(logging.INFO, logger="heartbeat"):
            _l2_messages(monkeypatch, store, msg=incoming)
        assert caplog.text, "nothing logged — the check would pass vacuously"
        assert incoming not in caplog.text
        assert "他昨天说他要辞职" not in caplog.text
        published = []
        while not sub.empty():
            published.append(sub.get_nowait())
        assert published, "nothing broadcast — the check would pass vacuously"
        # her streamed reply ("嗯") is meant to reach the room; the PROMPT — the
        # incoming turn and the prior history — never is
        assert all(incoming not in str(i) for i in published)
        assert all("他昨天说他要辞职" not in str(i) for i in published)
    finally:
        hb.BROADCAST.unsubscribe(sub)


# ── state.save: atomic, or she loses everything ───────────────
# state.json is the save file, not a cache. There is no acceptable
# partial write, so save() must be all-or-nothing under a hard kill.


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    # her/ does not exist yet — save() is also on the hook for creating it
    path = tmp_path / "her" / "state.json"
    monkeypatch.setattr(hb, "STATE_PATH", path)
    return path


def test_save_round_trips(state_file):
    hb.State(presence="sleeping", daily_proactive_count=4).save()
    loaded = hb.State.load()
    assert loaded.presence == "sleeping"
    assert loaded.daily_proactive_count == 4


def test_save_leaves_no_temp_file_behind(state_file):
    hb.State(daily_proactive_count=1).save()
    assert [p.name for p in state_file.parent.iterdir()] == ["state.json"]


def test_a_torn_file_is_unloadable(state_file):
    # the failure mode being prevented: half a file is not valid JSON, and
    # load() has no recovery path — it raises and she starts from nothing
    hb.State(presence="sleeping", daily_proactive_count=7).save()
    whole = state_file.read_bytes()
    state_file.write_bytes(whole[: len(whole) // 2])
    with pytest.raises(json.JSONDecodeError):
        hb.State.load()


def test_crash_between_write_and_rename_keeps_the_old_state(state_file, monkeypatch):
    hb.State(presence="active", daily_proactive_count=3).save()
    good = state_file.read_bytes()

    def die(*_):
        raise KeyboardInterrupt("^C after the temp file, before the swap")

    # the exact instant the old code corrupted the file: mid-write
    monkeypatch.setattr(hb.os, "replace", die)
    with pytest.raises(KeyboardInterrupt):
        hb.State(presence="sleeping", daily_proactive_count=99).save()

    # the real file was never opened for writing, so it is byte-identical —
    # the doomed save went to the temp file and died there
    assert state_file.read_bytes() == good
    survivor = hb.State.load()
    assert survivor.presence == "active"
    assert survivor.daily_proactive_count == 3


def test_a_torn_write_lands_on_the_temp_file_not_the_real_one(state_file):
    # the crash simulated one level lower than the rename: the write itself
    # dies half-done. Whichever file was open is now torn — the fix is that
    # it can only ever be the temp one.
    hb.State(presence="active", daily_proactive_count=3).save()
    good = state_file.read_bytes()

    real_write_text = Path.write_text

    def half_then_die(self, data, *a, **k):
        real_write_text(self, data[: len(data) // 2])
        raise KeyboardInterrupt("killed with the file open")

    # its own context — the fixture's monkeypatch holds STATE_PATH, and
    # undoing that here would send load() at the real save file
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "write_text", half_then_die)
        with pytest.raises(KeyboardInterrupt):
            hb.State(presence="sleeping", daily_proactive_count=99).save()

    assert state_file.read_bytes() == good
    survivor = hb.State.load()
    assert survivor.presence == "active"
    assert survivor.daily_proactive_count == 3


# ── prompt caching: sticky session routing + usage visibility ──
# DeepSeek's prefix cache on OpenRouter is automatic; the code's job is to
# route stickily (a stable session_id per layer) and make the cache VISIBLE
# (a usage line per call). Both live inside call_llm/stream_llm, so these run
# the real functions (_REAL_*) against a fake client — no network, no tokens,
# and no prompt CONTENT ever reaching the log.


def _usage(prompt: int, cached: int | None) -> SimpleNamespace:
    """A fake CompletionUsage. cached=None means the details object is absent
    entirely (some responses omit it) — which the logger must survive."""
    details = None if cached is None else SimpleNamespace(cached_tokens=cached)
    return SimpleNamespace(prompt_tokens=prompt, prompt_tokens_details=details)


class _RecordingClient:
    """Stands in for the OpenAI client: records every create() kwargs (so the
    session_id on the wire can be inspected) and returns a canned response
    carrying a usage object. Streaming yields content chunks then a trailing
    usage-only chunk, mirroring stream_options.include_usage."""

    def __init__(self, usage, content="嗯", chunks=("嗯，", "在的")):
        self.calls: list[dict] = []
        self._usage = usage
        self._content = content
        self._chunks = chunks
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return self._stream()
        message = SimpleNamespace(content=self._content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=self._usage)

    def _stream(self):
        for c in self._chunks:
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=c))], usage=None
            )
        # the trailing usage-only event include_usage asks for: no choices
        yield SimpleNamespace(choices=[], usage=self._usage)


def _usage_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "usage:" in r.getMessage()]


def test_session_id_is_stable_per_conversation_and_distinct_per_layer(monkeypatch):
    # sticky routing: repeated calls in one process carry the SAME id (never
    # regenerated per request), L1 and L2 carry DIFFERENT ids, and both share
    # the one base uuid — one conversation, two layers.
    client = _RecordingClient(usage=_usage(1000, 900))
    monkeypatch.setattr(hb, "_llm_client", lambda: client)

    _REAL_CALL_LLM("m", [{"role": "user", "content": "a"}], session_id=hb._session("l1"))
    _REAL_CALL_LLM("m", [{"role": "user", "content": "b"}], session_id=hb._session("l1"))
    list(_REAL_STREAM_LLM("m", [{"role": "user", "content": "c"}], session_id=hb._session("l2")))
    list(_REAL_STREAM_LLM("m", [{"role": "user", "content": "d"}], session_id=hb._session("l2")))

    sids = [c["extra_body"]["session_id"] for c in client.calls]
    assert sids[0] == sids[1], "L1 id must be stable across calls, not per-request"
    assert sids[2] == sids[3], "L2 id must be stable across calls"
    assert sids[0] != sids[2], "L1 and L2 must route distinctly"
    assert sids[0].endswith(":l1") and sids[2].endswith(":l2")
    assert sids[0].split(":")[0] == sids[2].split(":")[0], "same conversation, one base id"


def test_usage_line_reports_the_cache_hit_and_never_logs_content(monkeypatch, caplog):
    client = _RecordingClient(usage=_usage(10339, 10318))
    monkeypatch.setattr(hb, "_llm_client", lambda: client)
    with caplog.at_level(logging.INFO, logger="heartbeat"):
        _REAL_CALL_LLM(
            "m", [{"role": "user", "content": "一句私密的话"}], session_id=hb._session("l2")
        )

    lines = _usage_lines(caplog)
    assert lines, "no usage line logged"
    assert lines[0].startswith("L2 usage:")
    assert "prompt=10339" in lines[0]
    assert "cached=10318" in lines[0]
    assert "99.8% hit" in lines[0]
    # the prompt content rode the request, never the log — counts only
    assert all("一句私密的话" not in r.getMessage() for r in caplog.records)


def test_streaming_usage_is_read_from_the_final_chunk(monkeypatch, caplog):
    client = _RecordingClient(usage=_usage(5000, 4800), chunks=("嗯，", "我在"))
    monkeypatch.setattr(hb, "_llm_client", lambda: client)
    with caplog.at_level(logging.INFO, logger="heartbeat"):
        out = list(
            _REAL_STREAM_LLM("m", [{"role": "user", "content": "x"}], session_id=hb._session("l2"))
        )

    assert out == ["嗯，", "我在"], "content must still stream through untouched"
    # include_usage was requested, which is what makes that final chunk arrive
    assert client.calls[0]["stream_options"] == {"include_usage": True}
    lines = _usage_lines(caplog)
    assert lines and "prompt=5000" in lines[0] and "cached=4800" in lines[0]


def test_missing_cache_details_degrade_to_zero(monkeypatch, caplog):
    # a response with usage but no details block: cached counts as 0, no crash
    client = _RecordingClient(usage=_usage(1200, None))
    monkeypatch.setattr(hb, "_llm_client", lambda: client)
    with caplog.at_level(logging.INFO, logger="heartbeat"):
        _REAL_CALL_LLM("m", [{"role": "user", "content": "x"}], session_id=hb._session("l1"))

    line = _usage_lines(caplog)[0]
    assert "cached=0" in line
    assert "0.0% hit" in line  # prompt known, nothing cached — the first-call miss


def test_absent_usage_object_does_not_crash(monkeypatch, caplog):
    # some responses omit usage entirely; the call still returns her words and
    # the log says so plainly instead of throwing
    client = _RecordingClient(usage=None)
    monkeypatch.setattr(hb, "_llm_client", lambda: client)
    with caplog.at_level(logging.INFO, logger="heartbeat"):
        got = _REAL_CALL_LLM("m", [{"role": "user", "content": "x"}], session_id=hb._session("l1"))

    assert got == "嗯"
    assert any("usage: unavailable" in r.getMessage() for r in caplog.records)
