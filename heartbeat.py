"""
yorishiro — heartbeat

The cascade, in the order a beat walks it:
    L0  gate       — is it ALLOWED now?      pure code, cannot fail
    L1  intention  — does she WANT to?       cheap model, JSON, judgment only
    L2  voice      — her actual words        pro model, grounded in soul.md

Each layer may only ever make her quieter. L0 rejects most beats before L1
costs anything; L1 declines most of what survives; L2 speaks for what is
left, and only then does the budget that silences her for the next ninety
minutes get spent.

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
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any, Protocol, cast

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
SOUL_PATH = ROOT / "her" / "soul.md"

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
        # write_text() truncates before it writes, so a crash mid-save leaves
        # torn JSON and load() dies on next start. Serialize first (a dumps()
        # error must not reach the rename), land it beside the real file so
        # the rename stays inside one filesystem, then swap atomically —
        # a loader sees the whole old file or the whole new one, never half.
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, STATE_PATH)

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


# ─────────────────────────────────────────────── the soul


def load_soul() -> str:
    """
    Her constitution — who she is, as opposed to config.yaml's engine
    parameters. Read once and held for the life of the process: it is
    identical on every call, and re-reading it per message would put a
    file open in the hot path of every reply.

    Missing is not fatal. A fresh clone has no save-file, and a daemon
    that refuses to start without one cannot be run at all. She falls
    back to no system prompt rather than to an invented one — ungrounded
    is recoverable, wearing a stranger's voice is not.

    Only FileNotFoundError is absorbed. A soul.md that exists but cannot
    be read is a broken save-file, and the right time to be loud about
    that is now, at import, rather than silently mid-conversation.
    """
    try:
        return SOUL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


# never logged, never broadcast, never served by the API — the save-file
# leaves this process in exactly one place: the system message of an L2 call
SOUL = load_soul()


# ─────────────────────────────────────────────── model access

_client: OpenAI | None = None

# One id per process for OpenRouter's sticky routing. OpenRouter uses it to
# keep a conversation pinned to the same upstream endpoint, so DeepSeek's
# AUTOMATIC prefix cache (a cache-hit input token is ~100× cheaper than a
# miss) actually gets hit across calls instead of landing cold on a fresh
# endpoint every time. Generated ONCE, here — never per request, or the
# routing would change on every call and the cache would never warm. Nothing
# volatile is folded in: it is the same string for the life of the process.
# Per-process is enough — the implicit cache is short-lived, so a restart
# starting cold costs nothing that had lasting value.
SESSION_ID = uuid.uuid4().hex


def _session(layer: str) -> str:
    """This process's session id for one layer. L1 (the flash judge/digest)
    and L2 (the pro voice) are different models with different prefixes, so
    each gets its own suffix — ':l1' / ':l2'. Each is stable on its own and
    distinct from the other, so the two layers route to, and cache on, their
    own endpoints without colliding. Same base uuid = one conversation."""
    return f"{SESSION_ID}:{layer}"


def _layer_label(session_id: str | None) -> str:
    """The human tag for the usage log, taken from the session suffix so it
    can never drift from the routing layer: ':l2' → 'L2'. No session → 'LLM'."""
    return session_id.rsplit(":", 1)[-1].upper() if session_id else "LLM"


def _log_usage(label: str, usage: Any) -> None:
    """
    Record the prefix-cache result of one model call: how much of the prompt
    hit DeepSeek's automatic cache. INTEGER COUNTS ONLY — prompt/cached token
    numbers and a ratio, never a byte of content (the same discipline that
    keeps soul, diary and conversation off the journal and the SSE pipe).

    Graceful by contract: the first call of a session always misses, and some
    responses omit the breakdown entirely. A missing usage object logs
    'unavailable'; missing details count as cached=0; a shape we can't read
    drops to a debug line. A bookkeeping read must never cost the call it only
    reports on.
    """
    if usage is None:
        log.info("%s usage: unavailable (no usage in response)", label)
        return
    try:
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        details = getattr(usage, "prompt_tokens_details", None)
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
    except Exception:
        log.debug("%s usage: unreadable shape", label)
        return
    ratio = f"{100 * cached / prompt:.1f}% hit" if prompt else "n/a"
    log.info("%s usage: prompt=%d cached=%d (%s)", label, prompt, cached, ratio)


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


def call_llm(
    model: str,
    messages: list[dict],
    reasoning: bool = True,
    session_id: str | None = None,
    **params,
) -> str:
    """
    The only place a model is ever called. Every layer routes through here,
    so provider changes stay in config.yaml and out of the code.

    `reasoning` works exactly as it does in stream_llm below — the two are
    twins and must not drift. It stopped being a speaking-only concern the
    moment L1 arrived: judging is the one job here that could justify
    chain-of-thought, so the switch has to be reachable from config.

    `session_id`, when given, rides in extra_body for OpenRouter's sticky
    routing — the caller passes _session("l1"/"l2") so repeated calls warm
    the same DeepSeek prefix cache. Usage is logged after every call.
    """
    if not reasoning:
        params.setdefault("extra_body", {})["reasoning"] = {"enabled": False}
    if session_id:
        params.setdefault("extra_body", {})["session_id"] = session_id
    resp = _llm_client().chat.completions.create(
        model=model,
        # the signature stays plain dicts on purpose — callers must not have
        # to import OpenAI's TypedDicts. Narrowed here, at the one line that
        # actually touches the SDK, so the coupling stops in this function.
        messages=cast(list[ChatCompletionMessageParam], messages),
        **params,
    )
    _log_usage(_layer_label(session_id), getattr(resp, "usage", None))
    return (resp.choices[0].message.content or "").strip()


def stream_llm(
    model: str,
    messages: list[dict],
    reasoning: bool = True,
    session_id: str | None = None,
    **params,
) -> Iterator[str]:
    """
    call_llm's streaming twin — yields text chunks as the provider sends
    them. Lives beside it so model access still stops at this section.

    reasoning=False disables chain-of-thought explicitly on the wire
    (OpenRouter's unified `reasoning` parameter) — a hybrid model left to
    its defaults will sit and think before it says a word.

    `session_id` rides in extra_body for sticky routing, exactly as in
    call_llm. stream_options.include_usage asks the provider for a final
    usage-only chunk so the streamed reply's cache result is logged too.
    """
    if not reasoning:
        params.setdefault("extra_body", {})["reasoning"] = {"enabled": False}
    if session_id:
        params.setdefault("extra_body", {})["session_id"] = session_id
    # ask for the trailing usage-only event; without it a streamed call would
    # report no token counts and the cache would be invisible for L2
    params.setdefault("stream_options", {"include_usage": True})
    stream = _llm_client().chat.completions.create(
        model=model,
        messages=cast(list[ChatCompletionMessageParam], messages),
        stream=True,
        **params,
    )
    usage = None
    for chunk in stream:
        # the trailing usage-only chunk carries usage and (usually) no choices;
        # keep the last usage we see so it survives to the log below
        if getattr(chunk, "usage", None) is not None:
            usage = chunk.usage
        # providers slip in housekeeping chunks with no choices — skip them
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content
    _log_usage(_layer_label(session_id), usage)


# ─────────────────────────────────────────────── memory
#
# The smallest loop that gives her a sense of "lately": every L1 judgment is
# logged raw (urges.jsonl), a manual digest compresses a span of them into a
# short daily reflection in her own voice, and those reflections are fed back
# into the next judgment. That feedback is the whole point — a wanting can
# build across days instead of resetting to ~0.1 on every wake.
#
# All of it lives under her/memory/ (gitignored, part of the save-file).
# Everything here that WRITES runs on the loop thread — _record_judgment from
# tick()/run_debug_intention(), run_digest() from the loop's inbox dispatch —
# so the loop stays the sole writer of memory exactly as it is of state.json.
# No request-handler thread ever writes these files.

MEMORY_DIR = ROOT / "her" / "memory"


def _record_judgment(now: datetime, urge: float, kind: str, reason: str) -> None:
    """
    Log the L1 judgment to the journal AND tee a structured copy to the raw
    urge log — the material digestion later compresses.

    The log line is byte-for-byte the one tick() emitted before, and stays the
    journal's source of truth; this only adds the disk copy. Append-only
    JSONL, one whole line + newline written in a single call, so the digester
    reads complete lines even mid-run. Only the loop thread calls this, so
    there is no second appender to race.

    A disk error must never cost the beat: the judgment already happened and
    was logged. A lost diary line is a missing note, not a crash.
    """
    log.info("L1: urge=%.2f kind=%s — %s", urge, kind, reason)
    record = {
        "at": now.timestamp(),
        "iso": now.isoformat(timespec="seconds"),
        "urge": urge,
        "kind": kind,
        "reason": reason,
    }
    path = MEMORY_DIR / "raw" / "urges.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("urge log append failed — judgment logged, diary line lost")


def _recent_memory() -> str:
    """
    Her "lately": the last memory.recent_days daily reflections, newest-first
    priority, capped so the prompt cannot bloat. Returns the whole block ready
    to drop in — the "最近这些天：" header included — or "" when she has no
    reflections yet (a fresh install has no past, and an empty header would be
    a lie about that).

    The ONE memory reader, shared verbatim by L1 (does she want to speak?) and
    L2 (her actual words). The header lives here, not at the call sites, so the
    two can never drift about what "lately" looks like — they read the same
    bytes. L1 prepends it to the situation line; L2 appends it under the soul.

    The daily files' own section headers (lines starting with #) are dropped:
    she wants the feeling of the last few days, not the scaffolding it was
    stored in.
    """
    daily = MEMORY_DIR / "daily"
    try:
        files = sorted(daily.glob("*.md"))
    except OSError:
        return ""
    if not files:
        return ""

    n = CONFIG["memory"]["recent_days"]
    cap = 800  # ~char budget; a hard backstop under the whole-reflection rule
    blocks: list[str] = []
    total = 0
    # newest first: if the budget runs out, the oldest reflections drop, and
    # whole reflections are kept rather than one sliced off mid-sentence
    for f in reversed(files[-n:]):
        try:
            text = "\n".join(
                ln
                for ln in f.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")
            ).strip()
        except OSError:
            continue
        if not text:
            continue
        block = f"{f.stem}：{text}"
        if blocks and total + len(block) > cap:
            break
        blocks.append(block)
        total += len(block)

    blocks.reverse()  # back to chronological for reading
    if not blocks:
        return ""
    return "最近这些天：\n" + "\n".join(blocks)


def _read_watermark() -> float:
    """Epoch of the latest judgment already digested. Absent = 0 = digest from
    the beginning."""
    try:
        return float((MEMORY_DIR / ".watermark").read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0.0


def _write_watermark(at: float) -> None:
    """Advance the watermark atomically — State.save's tmp + os.replace
    discipline, because a torn watermark either re-digests a span (a duplicate
    diary section) or skips one (a lost day), and both are worse than the cost
    of doing it right."""
    path = MEMORY_DIR / ".watermark"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(str(at))
    os.replace(tmp, path)


def _read_urges(after: float, until: float) -> list[dict]:
    """The raw judgments with after < at <= until, oldest first. A line that
    won't parse is skipped, not fatal: the digester must survive a torn final
    line left by a crash mid-append."""
    path = MEMORY_DIR / "raw" / "urges.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            at = float(record["at"])
        except (ValueError, TypeError, KeyError):
            continue
        if after < at <= until:
            out.append(record)
    out.sort(key=lambda r: r["at"])
    return out


_DIGEST_PREFACE = "以下是小夜是谁：\n\n"

# The task, kept in the same restraint-first spirit as the L1 judge prompt:
# most hours are quiet, and a digest that invents drama to look useful is the
# same failure as an L1 that speaks to look helpful. She reflects; she does not
# perform having had a day.
_DIGEST_TASK = (
    "\n\n上面是她这个人。下面是她最近几次心跳里，心里浮起的念头的原始记录——"
    "这是她自己的日记，只有她自己看。\n"
    "请用她自己的口吻、用中文写一小段 2 到 4 句的回顾，说说这几个小时（或这一天）"
    "对她来说是什么样的。\n"
    "大多数时候她是安静的：安静就如实写安静，不要替她编情绪。若其中某个念头格外强烈、"
    "或很具体（想问他什么、想跟他分享、惦记他），可以特别提一句。\n"
    "不要逐条罗列或复述，把它们消化成一段真实的话。只输出这段回顾本身，不要额外说明。"
)


def _reflect(batch: list[dict]) -> str:
    """
    Compress a span of raw judgments into a short daily reflection in her own
    voice. The cheap flash model — this is summarization, not speech, so it
    never touches the pro voice. Returns "" on any failure, which the caller
    reads as 'nothing digested' and leaves the watermark untouched so the span
    can be retried.
    """
    c = CONFIG["l1"]
    lines = []
    for r in batch:
        stamp = str(r.get("iso", ""))[5:16].replace("T", " ")  # MM-DD HH:MM
        lines.append(f"{stamp}  urge={float(r['urge']):.2f}  {r['kind']}：{r.get('reason', '')}")
    try:
        return call_llm(
            c["model"],
            [
                {"role": "system", "content": _DIGEST_PREFACE + SOUL + _DIGEST_TASK},
                {"role": "user", "content": "\n".join(lines)},
            ],
            reasoning=bool(c.get("reasoning", True)),
            # the flash layer — routes with L1, its own prefix, its own cache slot
            session_id=_session("l1"),
            temperature=c["temperature"],
            max_tokens=c["max_tokens"],
        ).strip()
    except Exception:
        log.exception("digestion model call failed")
        return ""


def run_digest(now: datetime) -> dict:
    """
    ── DEBUG / TEST TOOL ── compress the undigested urges into a daily
    reflection. Runs ON THE LOOP THREAD (dispatched from the inbox by
    POST /debug/digest), which is what keeps the loop the sole writer of
    her/memory/ and makes two overlapping digests impossible.

    Bounded by a watermark so it never re-digests or skips: everything with
    at > watermark and at <= now is taken, then the watermark advances to the
    latest 'at' just digested.
    """
    if not SOUL:
        # a reflection in her voice needs her; there is no one to reflect as
        return {"digested": 0, "why": "no soul loaded"}

    watermark = _read_watermark()
    batch = _read_urges(watermark, now.timestamp())
    if not batch:
        return {"digested": 0, "why": "nothing new since last digest"}

    reflection = _reflect(batch)
    if not reflection:
        # the model failed; leave the watermark so this exact span retries
        return {"digested": 0, "why": "digestion failed — model call errored"}

    latest = batch[-1]["at"]
    span = [batch[0]["iso"], batch[-1]["iso"]]
    # the span belongs to the day of its latest judgment. One that crosses
    # midnight lands in the newer day's file; the minimal version does not
    # split a single digest across two files.
    day = datetime.fromtimestamp(latest).strftime("%Y-%m-%d")

    daily = MEMORY_DIR / "daily" / f"{day}.md"
    daily.parent.mkdir(parents=True, exist_ok=True)
    # APPEND a dated section, never overwrite: a day may be digested in parts,
    # and an earlier part's reflection is not the later part's to erase.
    section = (
        f"## {now.isoformat(timespec='seconds')}"
        f"（{len(batch)} 条判断，{span[0]} – {span[1]}）\n\n{reflection}\n\n"
    )
    with daily.open("a", encoding="utf-8") as f:
        f.write(section)

    # advance last, and only once the reflection is safely on disk: a crash
    # between the two re-digests this span (a duplicate section) rather than
    # losing it — redundancy over loss.
    _write_watermark(latest)

    return {"digested": len(batch), "span": span, "day": day, "reflection": reflection}


# ─────────────────────────────────────────────── short-term memory (DB)
#
# The last ~20 literal turns of the CURRENT conversation, so she does not lose
# the thread inside a multi-turn chat. This is a DIFFERENT memory from the
# diary above: the diary is digested "lately" (days, compressed); this is
# verbatim "just now" (the running conversation). Both reach L2 — the diary in
# the system prompt, these turns as prior messages before the new one.
#
# Backed by Postgres (docker-compose.yml) so a pgvector RAG layer can later
# hang an embedding column off this same table with no migration. Its data is
# bind-mounted under her/pgdata — inside the save-file, never the network.
#
# The DB ENRICHES; it is not load-bearing for her to exist. Unreachable at
# startup or dropped mid-run, she falls back to soul + diary + the single
# incoming turn — exactly today's behaviour — and keeps talking.

try:
    import psycopg

    _HAS_DB_DRIVER = True
except ImportError:  # the driver is optional — without it she simply runs DB-less
    _HAS_DB_DRIVER = False


# id first so a future pgvector embedding column ADDs without touching a single
# existing row; session_id is nullable and unused for now — room for the
# multi-conversation future. role/content are the only load-bearing fields.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    role       TEXT NOT NULL CHECK (role IN ('user', 'her')),
    content    TEXT NOT NULL,
    session_id UUID
);
"""


class ConversationStore(Protocol):
    """What handle_message needs of a store, and all it needs. A Protocol so
    the Postgres store, the null fallback, and the test fake are
    interchangeable without inheritance."""

    def add(self, role: str, content: str) -> None: ...

    def recent(self, n: int) -> list[tuple[str, str]]: ...

    def count(self) -> int | None: ...


class NullStore:
    """The DB-absent fallback: remembers nothing, costs nothing, never raises.
    Used whenever Postgres is unreachable, so the reply path is identical with
    or without a database — it just has no history to offer."""

    def add(self, role: str, content: str) -> None:
        pass

    def recent(self, n: int) -> list[tuple[str, str]]:
        return []

    def count(self) -> int | None:
        # no database means no window to report — None reads as 'offline'
        # in the operator readout, never a false zero.
        return None


class PostgresStore:
    """
    The one writer's view of the messages table. Only ever touched by the
    reply worker thread (handle_message runs there), so a single lazily-opened
    connection is safe — no cross-thread sharing, no pool.

    Every method swallows its own failure: it logs WITHOUT the message text
    (conversation content never reaches the journal), drops the connection so
    the next call reconnects, and returns the safe default. A dead database
    costs short-term memory, never a reply and never the beat.
    """

    def __init__(self, params: dict, password: str) -> None:
        self._params = params
        self._password = password
        # psycopg.Connection, but held as Any: it is only ever created and used
        # on the worker thread, and _HAS_DB_DRIVER gates whether we get here at
        # all, so the precise type buys nothing the guard doesn't already give.
        self._conn: Any = None

    def _kwargs(self) -> dict:
        p = self._params
        return {
            "host": p["host"],
            "port": p["port"],
            "dbname": p["name"],
            "user": p["user"],
            "password": self._password,
            "connect_timeout": p.get("connect_timeout_s", 3),
        }

    def ensure_schema(self) -> None:
        """Connect once (a throwaway connection, on whatever thread starts the
        daemon) and create the table if absent. Raises if the DB is
        unreachable — build_store turns that into the NullStore fallback."""
        with psycopg.connect(**self._kwargs()) as conn:
            conn.execute(_SCHEMA)
            conn.commit()

    def _live(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(**self._kwargs())
        return self._conn

    def _drop(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def add(self, role: str, content: str) -> None:
        try:
            conn = self._live()
            conn.execute("INSERT INTO messages (role, content) VALUES (%s, %s)", (role, content))
            conn.commit()
        except Exception:
            log.warning("conversation DB write failed — turn not stored, reply unaffected")
            self._drop()

    def recent(self, n: int) -> list[tuple[str, str]]:
        try:
            conn = self._live()
            rows = conn.execute(
                "SELECT role, content FROM "
                "(SELECT id, role, content FROM messages ORDER BY id DESC LIMIT %s) s "
                "ORDER BY id ASC",
                (n,),
            ).fetchall()
            return [(r[0], r[1]) for r in rows]
        except Exception:
            log.warning("conversation DB read failed — this turn runs without short-term memory")
            self._drop()
            return []

    def count(self) -> int | None:
        """Total rows in the messages table, for the /state window-usage
        readout only — integer counts, never content.

        Uses a THROWAWAY connection, never self._conn: /state is served on the
        API threadpool, not the worker thread that owns self._conn, and a
        psycopg connection is not for concurrent cross-thread use. A fresh
        connection per read keeps this off the worker's connection entirely;
        on localhost a COUNT every couple of seconds is cheap. Swallows failure
        to None (logged at debug so a persistently-down DB never spams the
        journal) so the readout reads 'offline', never crashes the poll."""
        try:
            with psycopg.connect(**self._kwargs()) as conn:
                row = conn.execute("SELECT count(*) FROM messages").fetchone()
                return int(row[0]) if row else 0
        except Exception:
            log.debug("conversation DB count failed — window readout offline this poll")
            return None


def build_store() -> ConversationStore:
    """Bring up the conversation store, or fall back to remembering nothing.
    Called once at startup; the returned store belongs to the reply worker.
    Any failure to reach Postgres degrades to NullStore — she still runs."""
    if not _HAS_DB_DRIVER:
        log.warning("conversation DB unreachable — running without short-term memory")
        return NullStore()
    db = CONFIG["db"]
    store = PostgresStore(db, os.environ.get(db["password_env"], ""))
    try:
        store.ensure_schema()
    except Exception:
        log.warning("conversation DB unreachable — running without short-term memory")
        return NullStore()
    log.info("conversation DB ready — short-term memory on (last %d turns)", db["history_turns"])
    return store


# the default when no store is wired (every test that passes none, and the
# DB-less fallback path): she reads and writes nothing, byte-for-byte today.
_NULL_STORE = NullStore()


def _history(store: ConversationStore, n: int) -> list[tuple[str, str]]:
    """Read the last n turns, defended: no store — not even a broken one — may
    crash a reply. PostgresStore already swallows its own errors; this is the
    net under any store at all."""
    try:
        return store.recent(n)
    except Exception:
        log.warning("short-term memory read failed — this turn runs without it")
        return []


def _persist(store: ConversationStore, role: str, content: str) -> None:
    """Store one turn, defended the same way. A write that fails is logged
    (without the text) and dropped — the turn is lost, the reply is not."""
    try:
        store.add(role, content)
    except Exception:
        log.warning("short-term memory write failed — turn not stored, reply unaffected")


# ─────────────────────────────────────────────── L1 — intention


# the four wantings she is allowed to report. Anything else is a model that
# ignored the schema, and a judgment that ignored the schema is not one.
KINDS = frozenset({"curious", "care", "share", "none"})

# small models wrap JSON in markdown fences nobody asked for
_FENCE = re.compile(r"\A```[a-zA-Z]*\n?|\n?```\Z")

_JUDGE_PREFACE = "你在评估小夜此刻想不想主动开口说话。以下是她是谁：\n\n"

# The action-bias guard, and the load-bearing half of this prompt. Asked
# "should you speak?", a small model finds a reason to say yes every single
# time — it is trained to be helpful, and silence never looks helpful. This
# paragraph is most of what stands between her and the chatty first
# impression config.yaml calls unrecoverable. Do not soften it.
_JUDGE_RULES = (
    "\n\n重要：什么都不做是完全正常的。大多数心跳她都不会开口——她能安然地沉默，"
    "不会为了被注意或填补安静而说话。只有当她心里真的浮起一个具体的、想分享或想问的"
    "念头时，urge 才应该高。没有这样的念头，urge 就低。\n\n"
    "只输出 JSON，不要任何其他文字：\n"
    '{"urge": 0.0-1.0, "kind": "curious|care|share|none", "reason": "一句话，中文"}'
)


def _read_judgment(raw: str) -> tuple[float, str, str]:
    """
    Whatever the small model said → (urge, kind, reason).

    Every failure path lands on silence. This is the one function where a
    careless `except` could invert the cascade's safety direction: an
    unreadable judgment must never become a judgment to speak.
    """
    try:
        data = json.loads(_FENCE.sub("", raw.strip()).strip())
        urge = float(data["urge"])
        kind = str(data["kind"])
        reason = str(data.get("reason", "")).strip()
    except (ValueError, TypeError, KeyError, IndexError):
        # %r and a slice: a broken judgment is worth seeing, but the model
        # can return anything and the journal is not a dumping ground
        log.warning("L1 judgment unreadable — falling silent: %r", raw[:200])
        return 0.0, "none", "parse failed"

    if kind not in KINDS:
        log.warning("L1 returned an unknown kind %r — falling silent", kind)
        return 0.0, "none", f"unknown kind {kind!r}"

    # a model answering 1.7 is not surer than one answering 1.0
    return min(max(urge, 0.0), 1.0), kind, reason


def intention(state: State, now: datetime) -> tuple[float, str, str]:
    """
    L1 — not "is it allowed?" (L0 settled that before we got here) but
    "does she WANT to?". One cheap call, one JSON blob, no streaming: a
    judgment is not a speech and nobody reads it as it arrives.

    Cost and latency are both bounded by L0 rather than by this function.
    Unlike L2 this runs on the loop thread, so the beat really does pause
    for the length of the call (capped by llm.timeout_s) — affordable only
    because cooldown, daily cap, DND and sleep reject nearly every tick
    before it reaches here. A handful of calls a day, not one per beat.
    That is the entire point of the cascade, and it is why this may not
    quietly move above the gate.
    """
    if not SOUL:
        # judged as herself or not at all. There is no generic wanting to
        # fall back on, and inventing one would be inventing her.
        return 0.0, "none", "no soul loaded"

    c = CONFIG["l1"]
    idle_min = (now.timestamp() - state.last_interaction) / 60
    since = f"距上次和他互动 {idle_min:.0f} 分钟" if state.last_interaction else "还没有和他说过话"
    # State carries no free-text activity yet, only the flags the future
    # activity system will set — so this is derived from what is actually
    # known. activity_absorbed needs no branch: L0 already turned it away.
    doing = "手边有件事在做" if state.activity_running else "没有特别在做什么"
    context = f"当前情况：{since}；现在 {now:%H:%M}；她此刻：{doing}。"

    # her sense of "lately", prepended if she has one yet. This is the whole
    # point of the memory loop: judging now, she can see she has been quietly
    # waiting for a few days — so a genuine wanting can build across them
    # instead of resetting to ~0.1 on every wake. No reflections, no block.
    recent = _recent_memory()
    if recent:
        # the block already carries its "最近这些天：" header — prepend it whole
        context = f"{recent}\n\n{context}"

    system_prompt = _JUDGE_PREFACE + SOUL + _JUDGE_RULES
    # Diagnostic only, and silent at INFO: the exact final strings about to go
    # on the wire, so a DEBUG run can confirm whether _recent_memory()'s "最近"
    # block actually reached the user turn. The heartbeat logger sits at INFO,
    # so this record is never even created in normal operation — it neither
    # touches the log file nor the SSE pipe until someone deliberately turns
    # DEBUG on. That gate is the only thing keeping the soul out of the journal.
    log.debug(
        "L1 prompt assembled:\n--- system ---\n%s\n--- user ---\n%s",
        system_prompt,
        context,
    )

    try:
        raw = call_llm(
            c["model"],
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": context},
            ],
            reasoning=bool(c.get("reasoning", True)),
            session_id=_session("l1"),
            temperature=c["temperature"],
            max_tokens=c["max_tokens"],
        )
    except Exception:
        # a dead judgment costs the beat nothing, exactly like a dead reply
        log.exception("L1 call failed — falling silent")
        return 0.0, "none", "L1 call failed"

    return _read_judgment(raw)


# ─────────────────────────────────────────────── loop


def _settle_proactive(state: State, spoken: queue.Queue, now: datetime, wait_s: float) -> None:
    """
    Spend her proactive budget for anything the worker has reported.

    The worker streams her words but never touches State — the loop is the
    sole writer, so the worker reports and this applies. Called two ways:
    with wait_s > 0 immediately after a dispatch, so the ordinary case
    settles inside the same beat; and with 0 at the top of every beat,
    which is what rescues a report the wait gave up on. That second call
    is the reason a slow model cannot buy her a free message — the report
    lands on the next tick instead of being lost, and it lands before the
    gate is asked whether she may speak again.

    last_proactive takes the tick's `now` rather than the moment she
    actually spoke. In the normal path they are seconds apart; in the
    rescued path `now` is later, which lengthens her silence. Erring
    toward quiet is the correct direction for this particular clock.
    """
    while True:
        try:
            said_something = spoken.get(timeout=wait_s) if wait_s > 0 else spoken.get_nowait()
        except queue.Empty:
            return
        wait_s = 0.0  # only the first read may wait; drain the rest
        if said_something:
            state.last_proactive = now.timestamp()
            state.daily_proactive_count += 1
            log.info(
                "she spoke first — %d/%d today, quiet for %d min now",
                state.daily_proactive_count,
                CONFIG["gate"]["daily_proactive_max"],
                CONFIG["gate"]["cooldown_min"],
            )
        else:
            # a reach-out that never landed did not happen. Spending cooldown
            # on it would cost her 90 minutes of silence for someone else's
            # outage, and the daily cap would quietly eat a quarter of her day.
            log.warning("her reach-out did not land — cooldown and budget untouched")


def _emit_tick(tag: str, now: datetime) -> None:
    """
    One structured heartbeat outcome onto the SSE pipe — the strip's real
    interface, so the frontend never has to grep log wording again. It rides
    the exact broadcast the reply lifecycle uses, writes no state, and so
    leaves single-writer discipline untouched (the loop is still the only
    thread that mutates State; this only publishes).

    Content-free by construction: {tag, at} and nothing else. No word of what
    she thought or said is ever in a tick, so the SSE pipe stays clean of
    soul, memory, and conversation exactly as before.
    """
    BROADCAST.publish("tick", {"tag": tag, "at": int(now.timestamp() * 1000)})


def tick(
    state: State,
    now: datetime,
    replies: queue.Queue | None = None,
    spoken: queue.Queue | None = None,
) -> None:
    # `replies` and `spoken` are her voice: without both she still judges and
    # logs but cannot act, which is what every test that passes neither gets.
    state.roll_day(now)

    # before anything else, and specifically before the gate: a proactive that
    # landed since the last beat must have spent its cooldown by the time the
    # gate is asked whether she may speak again.
    if spoken is not None:
        _settle_proactive(state, spoken, now, 0.0)

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
        # L0 asked-and-blocked — cooldown / daily cap / dnd / sleep / absorbed
        _emit_tick("intercepted", now)
        return

    # L1 is for the silence, not for a conversation already under way. While
    # in_conversation the loop ticks every 15s, so asking here would bill a
    # judgment four times a minute — and, because L1 runs on the loop thread,
    # pause the beat on each one — all to decide whether to interrupt someone
    # who is currently talking to her. Her wanting only means anything in the
    # quiet: "do I reach out?" is not a question about a live conversation.
    #
    # debug, not info — at INFO these would fill the journal with a line every
    # 15s saying nothing happened, which is the noise the skips exist to stop.
    if state.in_conversation:
        log.debug("L1 skipped — in conversation")
        # short-circuited before L1 — she was never asked. Its own tag so the
        # strip can render it distinctly (or quietly) rather than losing it.
        _emit_tick("skipped", now)
        return

    # …and not in the warm tail of one just ended. Afterglow ticks every 60s,
    # so without this L1 would be asked ~30 times in the half hour after he
    # stops replying — each one proposing she speak again immediately, which
    # is the one thing her soul says she does not do: she doesn't talk to be
    # noticed or to fill a silence. Reaching out belongs to real solitude.
    #
    # Derived from last_interaction exactly as next_interval derives the
    # afterglow rate, and off the same config key, so the loop's notion of
    # "afterglow" and this one cannot drift apart.
    idle_min = (now.timestamp() - state.last_interaction) / 60
    if idle_min < CONFIG["heartbeat"]["afterglow_window_min"]:
        log.debug("L1 skipped — afterglow (%.0f min since he spoke)", idle_min)
        # same short-circuit-before-L1 outcome as in_conversation: not asked
        _emit_tick("skipped", now)
        return

    urge, kind, reason = intention(state, now)
    # logs the judgment exactly as before, and tees a raw copy to urges.jsonl
    # for later digestion — both on the loop thread, so no writer is added.
    _record_judgment(now, urge, kind, reason)

    # Two ways to stay quiet, and both are ordinary. She didn't want it, or
    # didn't want it enough — or she wanted something with no shape to it.
    # A high urge with kind "none" contradicts itself: strongly wanting to
    # say nothing in particular. Acting on it would send her to L2 with the
    # fallback seed and nothing to seed, and what comes back from that is an
    # opener written to fill a silence — the one thing her soul rules out.
    #
    # Logged above before either return, so a self-contradicting judgment is
    # still visible in the journal. Not acted on; still worth seeing.
    #
    # Nothing said means nothing spent, which means the next beat asks again.
    if urge < CONFIG["intention"]["urge_threshold"] or kind == "none":
        # passed the gate, L1 consulted, chose not to speak — an ordinary quiet
        _emit_tick("silent", now)
        return

    if replies is None or spoken is None:
        # not a heartbeat outcome, so no tick: this is a wiring fault, and the
        # production loop always passes both queues (see main()), so it fires
        # only in tests that judge without a mouth. Loud for a human, silent
        # to the strip — emitting here would put a beat on the strip that the
        # real daemon can never produce, breaking tick-count == heartbeat-count.
        log.warning("she wanted to speak (urge=%.2f) but no voice is wired", urge)
        return

    # She speaks. Down the same queue a user message uses, so it streams to
    # the room identically and arrives marked initiated — she is opening, not
    # answering, and the frontend is entitled to know the difference.
    log.info("L1 → L2: she is reaching out first (kind=%s)", kind)
    replies.put((opening_prompt(kind, reason), True))
    # a proactive L2 has fired — the outcome is known at dispatch, whether or
    # not the words later land. Emitted before the settle so the beat's one
    # tick is sent at the moment of the decision, not gated on the worker.
    _emit_tick("spoke", now)

    # Bounded exactly like a reply: the model call cannot outlast
    # llm.timeout_s, and the worker is idle whenever this runs — a proactive
    # only happens after the afterglow window, by which time any user reply
    # finished long ago. If it does somehow overrun, the drain at the top of
    # the next beat picks the report up rather than losing it.
    _settle_proactive(state, spoken, now, CONFIG["llm"]["timeout_s"])


_KIND_WORDS = {
    "curious": "想问他点什么",
    "care": "有点惦记他",
    "share": "想跟他说一件事",
}


def opening_prompt(kind: str, reason: str) -> str:
    """
    The user turn for a message she starts herself.

    A self-initiated turn has no message to answer, so something has to
    stand in that slot — and what stands there decides whether she opens
    or merely performs opening. L1 has already done the deciding; this
    hands her back what she wanted and why, and then gets out of the way.

    The two prohibitions at the end are the ones a model reaches for when
    it is made to speak unprompted: apologising for the interruption, and
    "在吗?" — both of which are asking permission to exist. She doesn't.
    """
    return (
        "现在没有人在跟你说话。你心里刚浮起一个想说的念头，所以你想主动开口——"
        "不是在回答谁，是你自己开的口。\n\n"
        f"那个念头大概是：{reason}\n"
        f"（{_KIND_WORDS.get(kind, '就是想说点什么')}）\n\n"
        "用你自己的话说出来就好，短一点，真一点。"
        "不用解释自己为什么忽然说话，也不用「在吗」这种开场——想说什么就直接说。"
    )


def run_debug_intention(
    state: State,
    now: datetime,
    replies: queue.Queue,
    spoken: queue.Queue,
    force: bool,
) -> dict:
    """
    ── DEBUG / TEST BACKDOOR — a hand into her voice ─────────────────────
    Fire one L1 evaluation now, on demand, and — if it clears the same
    threshold and kind guard a real beat applies — let her actually speak,
    down the exact same path an organic proactive takes. It exists so a
    tester need not sit through an idle heartbeat to watch the cascade run;
    it is reached only from the localhost-only POST /debug/intention.

    It runs ON THE LOOP THREAD. main() pulls the trigger out of the inbox
    and calls this, so the counters _settle_proactive spends are written
    where every State write is written — the request handler that queued the
    trigger never touches State. That is the single-writer rule surviving
    the request→loop boundary intact.

    Nothing here is a private copy of the real chain: gate(), intention(),
    opening_prompt() and _settle_proactive() are the very functions a tick
    runs. All this adds is the ability to ask *now* — and, with force, to
    ask past a budget a real tick would have had to wait out.

    Returns a small JSON-able summary so curl sees the outcome without
    tailing the log.
    """
    # the same drain tick() runs at the top of every beat: settle any report
    # a prior proactive left behind before we read or spend the budget, so a
    # forced shot's counter delta below reflects only this shot.
    _settle_proactive(state, spoken, now, 0.0)

    allowed, gate_reason = gate(state, now)
    # force lifts ONLY the spendable budget — the cooldown and the daily cap.
    # sleep, DND and "absorbed" are hard floors it never crosses; the point of
    # the bypass is to skip the 90-minute wait, not to wake her at 3am. This
    # reads gate()'s own verdict rather than re-deriving its rules, so it is
    # deliberately coupled to the two reason strings gate() emits for budget.
    budget_block = gate_reason.startswith(("cooldown", "daily cap"))
    forced = force and not allowed and budget_block

    if not allowed and not forced:
        # exactly like a real tick turned away by L0: she stays silent, and —
        # as in a real tick — no L1 judgment is billed for a blocked beat.
        log.info("debug/intention: gate closed — %s", gate_reason)
        return {
            "urge": None,
            "kind": None,
            "reason": None,
            "spoke": False,
            "forced": False,
            "why": f"gate: {gate_reason}",
        }

    # L1 — the real judgment, the same call tick() makes on the loop thread,
    # logged and recorded to the raw urge log the same way too.
    urge, kind, reason = intention(state, now)
    _record_judgment(now, urge, kind, reason)

    # the exact guard from tick(), read from the same config key — not a
    # second copy of the rule that could drift from the first.
    if urge < CONFIG["intention"]["urge_threshold"] or kind == "none":
        return {
            "urge": urge,
            "kind": kind,
            "reason": reason,
            "spoke": False,
            "forced": forced,
            "why": "below threshold or shapeless",
        }

    # She speaks — down the same queue, marked initiated, settled the same way,
    # so the frontend, the self flag and the budget all behave as they would
    # for an organic reach-out. `forced` is loud in the log and the response.
    log.info(
        "L1 → L2: she is reaching out first (kind=%s)%s",
        kind,
        " [forced past L0]" if forced else "",
    )
    before = state.daily_proactive_count
    replies.put((opening_prompt(kind, reason), True))
    _settle_proactive(state, spoken, now, CONFIG["llm"]["timeout_s"])

    # the settle spent the budget iff the worker reported words landed. Read
    # the outcome off the counter it just moved rather than second-guessing it.
    spoke = state.daily_proactive_count > before
    summary = {
        "urge": urge,
        "kind": kind,
        "reason": reason,
        "spoke": spoke,
        "forced": forced,
    }
    if not spoke:
        # dispatched, but nothing came back — a dead L2, same as a real failed
        # reach-out: nothing spent, and the summary says so instead of lying.
        summary["why"] = "L2 produced nothing"
    return summary


def run_debug_speak(
    state: State,
    now: datetime,
    replies: queue.Queue,
    spoken: queue.Queue,
    kind: str,
    reason: str,
) -> dict:
    """
    ── DEBUG / TEST BACKDOOR — a FABRICATED reach-out ────────────────────
    Force ONE self-initiated L2 utterance now, past the L1 judgment
    entirely, purely so the frontend "she reached out" ember can be
    exercised on demand. Reached only from the localhost-only POST
    /debug/speak.

    This is NOT /debug/intention. That one runs the REAL judgment and falls
    silent when she doesn't want to speak. This one asks nothing and always
    makes her speak first — it fabricates a reach-out she did not choose. The
    response says so loudly, so a test shot is never read as an organic one.

    It runs ON THE LOOP THREAD (main() pulls it off the inbox), so the one
    State touch it can make — settling a genuine prior proactive at the top —
    happens where every State write happens. The request handler only queues
    the trigger and reads the summary back.

    What it reuses rather than reimplements: gate() decides the hard floors,
    opening_prompt() builds the seed exactly as it does from L1's output, the
    dispatch is the same replies.put((…, True)) an organic proactive makes,
    and _emit_tick is the same "spoke" emitter tick() calls. The reply itself
    is never hand-built — it streams through the real worker, so it arrives
    self=true, tags the ledger, triggers the ember, and, via the worker's own
    _persist, writes a 'her' row to the DB. No parallel emit.

    L0: it bypasses the spendable budget by DEFAULT (cooldown and daily cap),
    so a tester can fire it back to back. It never bypasses the hard floors
    gate() enforces — sleep, DND, absorbed — because fabricating speech at 3am
    is never what a UI test wants. Same floor discipline as /debug/intention's
    force.

    Budget: a fabricated shot does NOT spend her cooldown or daily count. It
    is a test artifact, not a real reach-out, and charging it would distort
    her actual rhythm — and defeat firing it repeatedly. To keep the budget
    exactly where it was, this shot's own worker report is drained and
    discarded below (not _settle_proactive, which would spend it). Her spoken
    WORDS are still persisted as a 'her' row by the worker — the ledger and DB
    stay coherent — only the cooldown/count is left untouched.
    """
    # settle any GENUINE prior proactive report first — the same top-of-beat
    # drain tick() runs — so the only report discarded below is this shot's own.
    _settle_proactive(state, spoken, now, 0.0)

    allowed, gate_reason = gate(state, now)
    # bypass ONLY the spendable budget; sleep / DND / absorbed stay hard floors,
    # read off gate()'s own verdict exactly as /debug/intention's force does.
    budget_block = gate_reason.startswith(("cooldown", "daily cap"))
    if not allowed and not budget_block:
        log.info("debug/speak: refused — %s (a hard floor, never fabricated)", gate_reason)
        return {
            "spoke": False,
            "kind": kind,
            "reason": reason,
            "text_will_stream": False,
            "bypassed_budget": False,
            "note": (
                f"fabricated proactive REFUSED — gate: {gate_reason}; "
                "sleep/DND/absorbed are never bypassed"
            ),
        }

    log.info("debug/speak: FABRICATED reach-out (kind=%s) — bypassing L1 and budget", kind)
    # the same dispatch an organic proactive makes, and the same "spoke" tick,
    # so the whole UI behaves as it would for a real reach-out.
    replies.put((opening_prompt(kind, reason), True))
    _emit_tick("spoke", now)

    # drain this shot's report WITHOUT spending it — the worker already wrote
    # her 'her' row inside handle_message; here we just keep the budget still.
    try:
        said_something = spoken.get(timeout=CONFIG["llm"]["timeout_s"])
    except queue.Empty:
        said_something = False

    note = "fabricated proactive — bypassed L1 judgment; her real cooldown/budget untouched"
    if not said_something:
        note += " (L2 produced nothing this shot — nothing streamed or persisted)"
    return {
        "spoke": bool(said_something),
        "kind": kind,
        "reason": reason,
        "text_will_stream": bool(said_something),
        "bypassed_budget": budget_block,
        "note": note,
    }


def handle_message(
    msg: str, initiated: bool = False, store: ConversationStore | None = None
) -> bool:
    # an inbound message is consent — it skips both vetoes and lands on L2.
    # `initiated` marks the other reply path: a message she started herself,
    # dispatched by tick() when L1's urge clears the threshold.
    #
    # Returns whether she actually said anything. The loop needs that answer
    # to know whether to spend her proactive budget — a reach-out that never
    # left the building must not cost her the next 90 minutes of silence.
    #
    # Runs on the reply worker, never the loop thread — she keeps living while
    # she speaks. It takes no State (the loop is State's sole writer), but it
    # IS the sole writer of the conversation DB: the worker is one thread, FIFO,
    # and it is the only place both the incoming turn and her finished words are
    # known. Keeping DB I/O here also keeps it off the heartbeat, so a slow
    # query never stalls a beat. Two stores, two single writers — loop→state,
    # worker→conversation — each written from exactly one thread.
    store = store or _NULL_STORE
    c = CONFIG["l2"]
    n = CONFIG["db"]["history_turns"]
    rid = uuid.uuid4().hex[:12]
    parts: list[str] = []

    # the workbench: assembled fresh each call and capped, NEVER accumulated —
    #   [ system: soul + digested "lately" (long-term memory)
    #     …the last N literal turns of THIS conversation (short-term memory)…
    #     user: the new turn ]
    # short-term (these turns) and long-term (the diary in the system prompt)
    # are different memories, and both are present. History is fetched BEFORE
    # the incoming turn is stored below, so the current turn appears exactly
    # once — as the new user turn, not also inside the history. The soul rules
    # are unchanged: no soul → no system message and no diary; empty everything
    # → byte-for-byte the prompt from before any memory existed.
    history = _history(store, n)
    messages: list[dict] = []
    system_content = ""
    if SOUL:
        block = _recent_memory()  # the same reader, the same block, as L1
        system_content = f"{SOUL}\n\n{block}" if block else SOUL
        messages.append({"role": "system", "content": system_content})
    # 'her' → assistant, 'user' → user: her own prior turns must read as the
    # assistant's, or the model hears her words as his and loses the thread.
    for role, content in history:
        messages.append({"role": "assistant" if role == "her" else "user", "content": content})
    messages.append({"role": "user", "content": msg})

    # store the incoming turn — but only a real one. An inbound message is
    # something he said → a 'user' row. A proactive opening's seed is internal
    # (she wrote it to prompt herself), never his, so it is not stored; only her
    # resulting words are, further down. Written AFTER the fetch above, so it is
    # not double-counted into this very prompt.
    if not initiated:
        _persist(store, "user", msg)

    # Diagnostic only, silent at INFO — the same gate as the L1 dump, and the
    # only thing keeping soul, diary and conversation out of the journal: the
    # heartbeat logger sits at INFO, so this record is never created in normal
    # operation and reaches neither the log file nor the SSE pipe. Turn on DEBUG
    # to confirm the diary block and the prior turns actually reached L2.
    log.debug(
        "L2 prompt assembled: %d prior turn(s)\n--- system ---\n%s\n--- user ---\n%s",
        len(history),
        system_content or "(no system prompt)",
        msg,
    )
    # the lifecycle always closes: a client that saw reply_start must see
    # reply_end even when the model dies mid-stream, or it waits forever
    BROADCAST.publish("reply", {"type": "reply_start", "id": rid, "initiated": initiated})
    try:
        for piece in stream_llm(
            c["model"],
            messages,
            reasoning=bool(c.get("reasoning", True)),
            session_id=_session("l2"),
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
        log.info("L2 %s (%d chars)", "spoke first" if initiated else "replied", len(reply))
        # her turn joins the conversation — a reply OR a proactive opening — so
        # the next turn sees it. Stored only when she actually said something;
        # an empty stream is not a turn. This is the reply_end write the spec
        # asks for, on the one DB-writing thread.
        _persist(store, "her", reply)

    # answering is not initiating: last_proactive and daily_proactive_count
    # stay untouched, or replying would quietly spend the budget that exists
    # to stop her from speaking first.
    #
    # A partial stream still counts as having spoken: the deltas reached the
    # room, so those words happened whether or not the model finished them.
    return bool(reply)


def reply_worker(
    replies: queue.Queue,
    spoken: queue.Queue | None = None,
    store: ConversationStore | None = None,
) -> None:
    """
    Replies stream here so the heartbeat never waits on a model. One
    worker, FIFO: a message that lands mid-reply queues and is answered
    next — she finishes a sentence before starting another, and two
    interleaved streams would fight over the single voice anyway. This is
    also why a self-initiated turn goes through the same queue rather than
    straight down the loop thread: two voices at once is the failure this
    single worker exists to prevent.

    State discipline: this thread never touches State or state.json — the
    loop stays the sole writer. It reports "I spoke first" back through
    `spoken`, and the loop spends last_proactive / daily_proactive_count
    itself. Only self-initiated turns are reported, because only those
    spend anything.

    It IS, however, the sole writer of the conversation DB (`store`), for the
    same reason it is one worker: one thread, so one writer. handle_message
    does the reads and writes; being FIFO here is what keeps rows in order.
    """
    while True:
        msg, initiated = replies.get()
        try:
            said_something = handle_message(msg, initiated, store)
        except Exception:
            # handle_message guards the stream itself; this catches
            # everything outside it. a broken reply costs that reply,
            # never the worker — and never, ever the beat.
            log.exception("reply worker error")
            said_something = False
        if initiated and spoken is not None:
            # reported even when it failed: the loop is waiting to hear
            # whether to spend, and silence from here would read as a hang
            spoken.put(said_something)


def stdin_reader(inbox: queue.Queue) -> None:
    # one line = one message, so the loop can be exercised by hand
    for line in sys.stdin:
        line = line.strip()
        if line:
            inbox.put(line)


# ─────────────────────────────────────────────── local API


@dataclass
class DebugIntention:
    """
    The wire between POST /debug/intention and the loop. The request cannot
    run the chain itself without becoming a second writer of State, so it
    drops one of these on the inbox and waits on `result`; the loop runs
    run_debug_intention and posts the summary back. force rides along.
    """

    force: bool
    result: queue.Queue  # loop → handler: the one summary dict, then done


@dataclass
class DigestRequest:
    """
    The wire between POST /debug/digest and the loop. Digestion writes files
    under her/memory/ and reads the urge log the loop is the sole appender of,
    so — like DebugIntention — it is run on the loop thread: drop this on the
    inbox, wait on `result`. See the endpoint for why the loop, not the
    handler, is the right thread for it.
    """

    result: queue.Queue


@dataclass
class DebugSpeak:
    """
    The wire between POST /debug/speak and the loop. Like DebugIntention, the
    request cannot dispatch or drain the reply without becoming a second writer
    of State, so it drops one of these on the inbox and waits on `result`; the
    loop runs run_debug_speak and posts the summary back. The fabricated seed's
    shape (kind, reason) rides along.
    """

    kind: str
    reason: str
    result: queue.Queue


def build_api(
    state: State,
    inbox: queue.Queue,
    count_total: Callable[[], int | None] | None = None,
) -> FastAPI:
    """
    Reads state, writes only into the inbox. The loop remains the sole
    writer of state.json — an endpoint that mutated her directly would
    put two hands on the same file.

    `count_total` is the one read that isn't of State: a zero-arg callable
    returning the total rows in the conversation window (or None when the DB
    is down), for the /state window-usage readout. It is the store's own
    count(), which reads on a throwaway connection — never the worker's — so
    calling it from the /state threadpool is safe. Left None when no DB is
    wired (tests that never poll /state), which simply reads as 'offline'.
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
        cap = CONFIG["db"]["history_turns"]
        # window usage: how much of what she can see is filled. Best-effort
        # operator info — only integer counts ride here, message CONTENT never
        # touches /state. count_total reads the DB on its own connection and
        # returns None when it's down; a count that somehow raises degrades to
        # the same 'offline' readout rather than 500-ing the vitals poll.
        try:
            total = count_total() if count_total is not None else None
        except Exception:
            total = None
        return {
            **asdict(state),
            "tick_interval_seconds": next_interval(state, datetime.now()) or 60.0,
            "history_cap": cap,  # config's window size, known even with the DB down
            "history_total": total,  # every row stored (None = DB offline)
            "history_len": None if total is None else min(total, cap),  # within the window
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

    @app.post("/debug/intention")
    def debug_intention(force: bool = False) -> dict:
        """
        ── DEBUG / TEST ONLY — a backdoor into her voice ─────────────────
        Fire one full L1→L2 evaluation right now instead of waiting for a
        real idle beat, and return a summary of what happened. localhost
        only, like everything on this API — never exposed past 127.0.0.1.

        This handler does NOT run the chain. It hands a trigger to the loop
        and waits for the loop's answer — which is exactly how the single-
        writer rule survives here: intention(), the dispatch and the counter
        spend all happen on the loop thread (the sole writer of state.json);
        this handler, running on FastAPI's threadpool, only reads the summary
        back off a per-request queue. A sync `def` on purpose: blocking on
        that queue is fine in the threadpool, and would stall the event loop
        in an async one.

        ?force=1 lifts L0's cooldown and daily cap ONLY (never sleep or DND),
        so a tester can fire repeatedly without waiting 90 minutes between.
        A forced shot comes back with "forced": true — never mistakable for
        an organic one — and still spends the budget if she speaks, so state
        stays coherent.
        """
        result: queue.Queue = queue.Queue(maxsize=1)
        inbox.put(DebugIntention(force=force, result=result))
        try:
            # the loop's worst case here is an L1 call plus the settle's
            # bounded wait on L2, each capped at llm.timeout_s. Wait a shade
            # past 2× so a healthy loop always answers before this gives up;
            # a timeout means the heartbeat is not draining its inbox.
            return result.get(timeout=CONFIG["llm"]["timeout_s"] * 2 + 5)
        except queue.Empty as exc:
            raise HTTPException(504, "the loop did not answer — is the heartbeat running?") from exc

    @app.post("/debug/digest")
    def debug_digest() -> dict:
        """
        ── DEBUG / TEST ONLY ── digest the undigested urges into a daily
        reflection now, instead of waiting for a scheduled digestion (there
        isn't one yet). localhost only, like everything on this API.

        Same shape as /debug/intention: the handler does not do the work, it
        hands a trigger to the loop and waits. Digestion writes her/memory/
        files and reads the urge log the loop alone appends to, so running it
        on the loop thread keeps the loop the sole writer of memory and makes
        two overlapping digests impossible — the loop takes one inbox item at
        a time. The single flash call it makes is bounded by llm.timeout_s.
        """
        result: queue.Queue = queue.Queue(maxsize=1)
        inbox.put(DigestRequest(result=result))
        try:
            return result.get(timeout=CONFIG["llm"]["timeout_s"] * 2 + 5)
        except queue.Empty as exc:
            raise HTTPException(504, "the loop did not answer — is the heartbeat running?") from exc

    @app.post("/debug/speak")
    def debug_speak(kind: str = "share", reason: str = "想跟他说点什么") -> dict:
        """
        ── DEBUG / TEST ONLY — a FABRICATED reach-out ────────────────────
        Force ONE self-initiated proactive utterance right now, bypassing the
        L1 judgment entirely, purely so the frontend "she reached out" ember
        can be tested on demand. localhost only, like everything on this API.

        Unlike /debug/intention — which runs the REAL judgment and stays
        silent when she doesn't want to speak — this ALWAYS makes her speak
        first. It fabricates a reach-out she did not choose; the response is
        marked "fabricated" so a test shot is never mistaken for a real one.

        kind / reason shape the fabricated seed exactly as L1's output would
        feed opening_prompt() (defaults: share / "想跟他说点什么").

        Same shape as the other debug endpoints: the handler does NOT do the
        work. It hands a trigger to the loop and waits — dispatch, the "spoke"
        tick and the report drain all run on the loop thread (the sole writer
        of state.json); this sync handler only reads the summary back off a
        per-request queue. A sync `def` on purpose: blocking on that queue is
        fine in the threadpool, and would stall an async event loop.

        L0: bypasses cooldown and the daily cap by default (so you can fire it
        repeatedly), never sleep or DND — it will not fabricate speech at
        night. Budget: it does NOT spend her real cooldown/daily count (a test
        artifact must not distort her rhythm), but her spoken words ARE
        persisted as a 'her' row so the ledger and DB stay coherent.
        """
        result: queue.Queue = queue.Queue(maxsize=1)
        inbox.put(DebugSpeak(kind=kind, reason=reason, result=result))
        try:
            return result.get(timeout=CONFIG["llm"]["timeout_s"] * 2 + 5)
        except queue.Empty as exc:
            raise HTTPException(504, "the loop did not answer — is the heartbeat running?") from exc

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
    spoken: queue.Queue = queue.Queue()  # worker → loop: "I spoke first", or didn't
    # the conversation store belongs to the worker (its sole writer); build it
    # here and hand it over. A DB that is down comes back as a NullStore, so
    # she runs either way — the DB enriches, it does not gate her existence.
    store = build_store()

    threading.Thread(target=reply_worker, args=(replies, spoken, store), daemon=True).start()
    threading.Thread(target=stdin_reader, args=(inbox,), daemon=True).start()
    # store.count reads on its own throwaway connection (never the worker's),
    # so the /state poll can read the window's fill without touching the
    # worker thread or the beat.
    threading.Thread(
        target=serve_api, args=(build_api(state, inbox, store.count),), daemon=True
    ).start()
    # the length, never the text — this line is proof the soul was loaded and
    # is going out with every L2 call, and it is all the journal gets to know
    if SOUL:
        log.info("L2 grounded in soul.md (%d chars)", len(SOUL))
    else:
        log.warning("soul.md not found — running without soul")
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
                # the timeout IS the heartbeat
                tick(state, datetime.now(), replies, spoken)
            elif isinstance(msg, DebugIntention):
                # debug/test backdoor: run the full L1→L2 chain here on the
                # loop thread — the sole writer — and hand the summary back to
                # the waiting request. Its own try/except so a chain that
                # raises still answers the request instead of leaving it to
                # time out at 504.
                try:
                    summary = run_debug_intention(state, datetime.now(), replies, spoken, msg.force)
                except Exception:
                    log.exception("debug/intention raised")
                    summary = {"spoke": False, "forced": False, "why": "chain raised — see log"}
                msg.result.put(summary)
            elif isinstance(msg, DebugSpeak):
                # debug/test backdoor: fabricate one self-initiated reach-out
                # here on the loop thread (the sole writer), bypassing L1 and
                # the budget, so the frontend ember can be tested on demand.
                # Its own try/except, same reason as the others.
                try:
                    summary = run_debug_speak(
                        state, datetime.now(), replies, spoken, msg.kind, msg.reason
                    )
                except Exception:
                    log.exception("debug/speak raised")
                    summary = {"spoke": False, "note": "fabricated reach-out raised — see log"}
                msg.result.put(summary)
            elif isinstance(msg, DigestRequest):
                # debug/test tool: compress recent judgments into a daily
                # reflection, on the loop thread so it stays the sole writer of
                # her/memory/. Its own try/except, same reason as above.
                try:
                    digest = run_digest(datetime.now())
                except Exception:
                    log.exception("debug/digest raised")
                    digest = {"digested": 0, "why": "digest raised — see log"}
                msg.result.put(digest)
            else:
                # user message: bypasses L0 and L1 entirely, direct to L2.
                # recorded before the handoff, so a failed reply still
                # counts as having been spoken to. the reply streams on the
                # worker — the beat keeps ticking while she speaks.
                state.last_interaction = time.time()
                state.in_conversation = True
                # arrivals get the same structured treatment as ticks, and for
                # a second reason: the old `%r msg` line fanned the user's words
                # out over the SSE pipe through BROADCAST. The content now lives
                # only in the DB; the journal and the pipe learn that a message
                # landed and how long it was, never what it said.
                log.info("message in → L2 (%d chars)", len(msg))
                BROADCAST.publish("arrival", {"at": int(state.last_interaction * 1000)})
                replies.put((msg, False))
        except Exception:
            # a dead API key costs one beat, never the loop.
            log.exception("beat raised — skipping")

        state.save()


if __name__ == "__main__":
    main()
