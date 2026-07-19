"""
yorishiro — heartbeat

Phase 1, step 1: L0 only.
Gate answers exactly one question: "is it ALLOWED now?"
Never "should she?" — that's L1's job, and it isn't wired yet.

Success criterion for this file:
    a log line reading `tick blocked: cooldown`
    Prove she can be silent before she can speak.
"""

import asyncio
import json
import logging
import os
import queue
import random
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from typing import cast

import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam
from sse_starlette.sse import EventSourceResponse

load_dotenv()  # before anything reads os.environ — no --env-file, ever

ROOT = Path(__file__).resolve().parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())
STATE_PATH = ROOT / "her" / "state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(ROOT / "her" / "logs" / "heartbeat.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("heartbeat")


# ─────────────────────────────────────────────── log fan-out


class Broadcast(logging.Handler):
    """
    Fans her log out to every open /events stream. Queue items are either
    plain strings (journal lines, sent as default SSE messages) or dicts
    (structured events, sent as named SSE events) — the stream endpoint
    tells them apart by type.
    """

    def __init__(self) -> None:
        super().__init__()
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        self._fan_out(self.format(record))

    def publish(self, event: str, payload: dict) -> None:
        """Structured events ride the same pipe as the journal lines."""
        self._fan_out({"event": event, "data": json.dumps(payload, ensure_ascii=False)})

    def _fan_out(self, item: str | dict) -> None:
        with self._lock:
            for q in self._subs:
                try:
                    q.put_nowait(item)
                except queue.Full:
                    pass  # a stalled reader must never stall the heartbeat

    def subscribe(self) -> queue.Queue:
        # deep enough that a real reply never drops: a 280-token turn is a
        # few hundred deltas, so 4096 holds a >10× longer reply plus the
        # journal around it. drop-on-full stays — a dead reader caps out
        # and starts losing items, it never grows memory unbounded.
        q: queue.Queue = queue.Queue(maxsize=4096)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)


# attached to her logger only — uvicorn's chatter is not her voice
BROADCAST = Broadcast()
BROADCAST.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
log.addHandler(BROADCAST)


# ─────────────────────────────────────────────── state


@dataclass
class State:
    presence: str = "active"  # active | sleeping
    last_proactive: float = 0.0  # epoch
    last_interaction: float = 0.0  # epoch
    daily_proactive_count: int = 0
    daily_count_date: str = ""  # YYYY-MM-DD, for rollover
    in_conversation: bool = False
    activity_running: bool = False
    activity_absorbed: bool = False  # focused — don't self-interrupt

    @classmethod
    def load(cls) -> "State":
        if STATE_PATH.exists():
            return cls(**json.loads(STATE_PATH.read_text()))
        return cls()

    def save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(asdict(self), indent=2))

    def roll_day(self, now: datetime) -> None:
        today = now.strftime("%Y-%m-%d")
        if self.daily_count_date != today:
            self.daily_count_date = today
            self.daily_proactive_count = 0


# ─────────────────────────────────────────────── L0 — pure code, cannot fail


def _in_window(now: datetime, start: str, end: str) -> bool:
    """Handles windows that cross midnight."""
    s = dtime.fromisoformat(start)
    e = dtime.fromisoformat(end)
    t = now.time()
    return (s <= t or t < e) if s > e else (s <= t < e)


def gate(state: State, now: datetime) -> tuple[bool, str]:
    """
    Objective facts only. If a rule needs 'understanding context',
    it does not belong in L0.
    Returns (allowed, reason).
    """
    g = CONFIG["gate"]

    if state.presence == "sleeping":
        return False, "sleeping"

    if _in_window(now, g["dnd_start"], g["dnd_end"]):
        return False, "dnd"

    elapsed_min = (now.timestamp() - state.last_proactive) / 60
    if elapsed_min < g["cooldown_min"]:
        return False, f"cooldown ({elapsed_min:.0f}/{g['cooldown_min']} min)"

    if state.daily_proactive_count >= g["daily_proactive_max"]:
        return False, f"daily cap ({state.daily_proactive_count}/{g['daily_proactive_max']})"

    if state.activity_absorbed:
        return False, "absorbed in activity"

    return True, "open"


# ─────────────────────────────────────────────── adaptive tick


def next_interval(state: State, now: datetime) -> float | None:
    h = CONFIG["heartbeat"]

    if state.presence == "sleeping":
        return None  # suspended until wake

    if state.in_conversation:
        return h["tick_in_conversation"]

    idle_min = (now.timestamp() - state.last_interaction) / 60
    if idle_min < h["afterglow_window_min"]:
        return h["tick_afterglow"]

    if state.activity_running:
        return h["tick_activity"]

    # jitter is mandatory. fixed intervals produce clockwork timing;
    # once the pattern is spotted, "alive" dies.
    return h["tick_idle"] + random.uniform(-h["tick_jitter"], h["tick_jitter"])


# ─────────────────────────────────────────────── model access

_client: OpenAI | None = None


def _llm_client() -> OpenAI:
    # built lazily: a missing key should cost one reply, not the import
    global _client
    if _client is None:
        c = CONFIG["llm"]
        key = os.environ.get(c["api_key_env"])
        if not key:
            raise RuntimeError(f"{c['api_key_env']} is not set")
        _client = OpenAI(base_url=c["base_url"], api_key=key, timeout=c["timeout_s"])
    return _client


def call_llm(model: str, messages: list[dict], **params) -> str:
    """
    The only place a model is ever called. Every layer routes through here,
    so provider changes stay in config.yaml and out of the code.

    No `reasoning` parameter, deliberately — she is speaking, not solving.
    """
    resp = _llm_client().chat.completions.create(
        model=model,
        # the signature stays plain dicts on purpose — callers must not have
        # to import OpenAI's TypedDicts. Narrowed here, at the one line that
        # actually touches the SDK, so the coupling stops in this function.
        messages=cast(list[ChatCompletionMessageParam], messages),
        **params,
    )
    return (resp.choices[0].message.content or "").strip()


def stream_llm(model: str, messages: list[dict], reasoning: bool = True, **params) -> Iterator[str]:
    """
    call_llm's streaming twin — yields text chunks as the provider sends
    them. Lives beside it so model access still stops at this section.

    reasoning=False disables chain-of-thought explicitly on the wire
    (OpenRouter's unified `reasoning` parameter) — a hybrid model left to
    its defaults will sit and think before it says a word.
    """
    if not reasoning:
        params.setdefault("extra_body", {})["reasoning"] = {"enabled": False}
    stream = _llm_client().chat.completions.create(
        model=model,
        messages=cast(list[ChatCompletionMessageParam], messages),
        stream=True,
        **params,
    )
    for chunk in stream:
        # providers slip in housekeeping chunks with no choices — skip them
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


# ─────────────────────────────────────────────── loop


def tick(state: State, now: datetime) -> None:
    state.roll_day(now)

    # a silent conversation has to decay on its own — nothing else clears the
    # flag, and next_interval would pin the loop at the 15s rate until dawn.
    if state.in_conversation:
        idle_min = (now.timestamp() - state.last_interaction) / 60
        if idle_min >= CONFIG["heartbeat"]["conversation_timeout_min"]:
            state.in_conversation = False
            log.info("conversation timed out after %.0f min idle", idle_min)

    allowed, reason = gate(state, now)
    if not allowed:
        # the record of choosing not to disturb is evidence of existence
        log.info("tick blocked: %s", reason)
        return

    log.info("tick passed gate → L1 (not wired yet) → rest")
    # TODO Phase 1 step 2 — L1, still unwired:
    #   urge, action, why = intention(state, now)      # small model, JSON only
    #   if action == "initiate" and urge > CONFIG["intention"]["urge_threshold"]:
    #       call_llm(...)                              # L2, retains veto power
    #       state.last_proactive = now.timestamp()     # ONLY this path spends
    #       state.daily_proactive_count += 1           # the proactive budget


def handle_message(msg: str, initiated: bool = False) -> None:
    # an inbound message is consent — it skips both vetoes and lands on L2.
    # `initiated` marks the other reply path: an L2 message she started
    # herself (the future L1 wire routes through here with True).
    #
    # Runs on the reply worker, never the loop thread — she keeps living
    # while she speaks. Takes no State on purpose: the loop is the sole
    # writer, and a signature without state can't be tempted to write it.
    c = CONFIG["l2"]
    rid = uuid.uuid4().hex[:12]
    parts: list[str] = []
    # the lifecycle always closes: a client that saw reply_start must see
    # reply_end even when the model dies mid-stream, or it waits forever
    BROADCAST.publish("reply", {"type": "reply_start", "id": rid, "initiated": initiated})
    try:
        for piece in stream_llm(
            c["model"],
            # minimal for now. persona and retrieved memory assemble here later —
            # the workbench is built per call, never accumulated.
            [{"role": "user", "content": msg}],
            reasoning=bool(c.get("reasoning", True)),
            temperature=c["temperature"],
            max_tokens=c["max_tokens"],
        ):
            parts.append(piece)
            BROADCAST.publish("reply", {"type": "reply_delta", "id": rid, "text": piece})
    except Exception:
        # a failed model call costs one reply, never the beat
        log.exception("L2 stream failed after %d chunks", len(parts))
    finally:
        BROADCAST.publish("reply", {"type": "reply_end", "id": rid})

    reply = "".join(parts)
    if reply:
        print(reply, flush=True)
        log.info("L2 replied (%d chars)", len(reply))

    # answering is not initiating: last_proactive and daily_proactive_count
    # stay untouched, or replying would quietly spend the budget that exists
    # to stop her from speaking first.


def reply_worker(replies: queue.Queue) -> None:
    """
    Replies stream here so the heartbeat never waits on a model. One
    worker, FIFO: a message that lands mid-reply queues and is answered
    next — she finishes a sentence before starting another, and two
    interleaved streams would fight over the single voice anyway.

    State discipline: this thread never touches State or state.json — the
    loop stays the sole writer. When the L1 self-initiated path lands, the
    worker will report "I spoke first" back through a loop-bound queue
    (drained at the top of each beat, like the inbox), and the loop will
    spend last_proactive / daily_proactive_count itself.
    """
    while True:
        msg, initiated = replies.get()
        try:
            handle_message(msg, initiated)
        except Exception:
            # handle_message guards the stream itself; this catches
            # everything outside it. a broken reply costs that reply,
            # never the worker — and never, ever the beat.
            log.exception("reply worker error")


def stdin_reader(inbox: queue.Queue) -> None:
    # one line = one message, so the loop can be exercised by hand
    for line in sys.stdin:
        line = line.strip()
        if line:
            inbox.put(line)


# ─────────────────────────────────────────────── local API


def build_api(state: State, inbox: queue.Queue) -> FastAPI:
    """
    Reads state, writes only into the inbox. The loop remains the sole
    writer of state.json — an endpoint that mutated her directly would
    put two hands on the same file.
    """
    app = FastAPI(title="yorishiro")
    # the web room runs on its own dev port; bound to 127.0.0.1 there is
    # no cross-site surface worth defending, so the door stays open
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    @app.get("/state")
    def read_state() -> dict:
        # the interval is derived, never stored — computed here so clients
        # don't have to mirror config.yaml's tick table
        return {
            **asdict(state),
            "tick_interval_seconds": next_interval(state, datetime.now()) or 60.0,
        }

    @app.post("/message")
    async def post_message(request: Request) -> dict:
        # body parsed by hand: curl on a local socket often omits
        # content-type, and being strict about it buys nothing here.
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "body must be JSON") from exc
        text = str(body.get("text", "")).strip()
        if not text:
            raise HTTPException(400, "text is required")
        inbox.put(text)  # same door the stdin reader uses
        return {"queued": text}

    @app.get("/events")
    async def events(request: Request) -> EventSourceResponse:
        q = BROADCAST.subscribe()

        async def stream():
            try:
                while not await request.is_disconnected():
                    try:
                        item = q.get_nowait()
                    except queue.Empty:
                        # polled, not blocked: q is a thread queue and
                        # waiting on it would stall the event loop
                        await asyncio.sleep(0.2)
                        continue
                    # journal lines ride as default messages; structured
                    # events (reply lifecycle) as named SSE events
                    yield item if isinstance(item, dict) else {"data": item}
            finally:
                BROADCAST.unsubscribe(q)

        return EventSourceResponse(stream())

    return app


def serve_api(app: FastAPI) -> None:
    c = CONFIG["api"]
    uvicorn.Server(uvicorn.Config(app, host=c["host"], port=c["port"], log_level="warning")).run()


def main() -> None:
    state = State.load()
    inbox: queue.Queue = queue.Queue()
    replies: queue.Queue = queue.Queue()  # (msg, initiated) → reply worker

    threading.Thread(target=reply_worker, args=(replies,), daemon=True).start()
    threading.Thread(target=stdin_reader, args=(inbox,), daemon=True).start()
    threading.Thread(target=serve_api, args=(build_api(state, inbox),), daemon=True).start()
    log.info("begin heartbeat — api on %(host)s:%(port)s", CONFIG["api"])

    while True:
        interval = next_interval(state, datetime.now())
        if interval is None:
            log.info("sleeping — heartbeat suspended")
            interval = 60  # poll for wake

        # a heartbeat is not "wake every N seconds" — it is "wait until
        # something happens, or the interval expires."
        try:
            msg = inbox.get(timeout=interval)
        except queue.Empty:
            msg = None

        try:
            if msg is None:
                tick(state, datetime.now())  # the timeout IS the heartbeat
            else:
                # user message: bypasses L0 and L1 entirely, direct to L2.
                # recorded before the handoff, so a failed reply still
                # counts as having been spoken to. the reply streams on the
                # worker — the beat keeps ticking while she speaks.
                state.last_interaction = time.time()
                state.in_conversation = True
                log.info("message in → L2: %r", msg)
                replies.put((msg, False))
        except Exception:
            # a dead API key costs one beat, never the loop.
            log.exception("beat raised — skipping")

        state.save()


if __name__ == "__main__":
    main()
