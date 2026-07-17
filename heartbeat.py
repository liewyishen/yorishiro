"""
yorishiro — heartbeat

Phase 1, step 1: L0 only.
Gate answers exactly one question: "is it ALLOWED now?"
Never "should she?" — that's L1's job, and it isn't wired yet.

Success criterion for this file:
    a log line reading `tick blocked: cooldown`
    Prove she can be silent before she can speak.
"""

import json
import random
import time
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import yaml

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


# ─────────────────────────────────────────────── state

@dataclass
class State:
    presence: str = "active"              # active | sleeping
    last_proactive: float = 0.0           # epoch
    last_interaction: float = 0.0         # epoch
    daily_proactive_count: int = 0
    daily_count_date: str = ""            # YYYY-MM-DD, for rollover
    in_conversation: bool = False
    activity_running: bool = False
    activity_absorbed: bool = False       # focused — don't self-interrupt

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


# ─────────────────────────────────────────────── loop

def tick(state: State, now: datetime) -> None:
    state.roll_day(now)

    allowed, reason = gate(state, now)
    if not allowed:
        # the record of choosing not to disturb is evidence of existence
        log.info("tick blocked: %s", reason)
        return

    log.info("tick passed gate → L1 (not wired yet) → rest")
    # TODO Phase 1 step 2:
    #   urge, action, why = intention(state, now)      # small model, JSON only
    #   if action == "initiate" and urge > CONFIG["intention"]["urge_threshold"]:
    #       execute(state, seed=why)                   # L2, retains veto power


def main() -> None:
    state = State.load()
    log.info("begin heartbeat")

    while True:
        now = datetime.now()
        try:
            tick(state, now)
        except Exception:
            # the heartbeat may skip; it may never crash.
            log.exception("tick raised — skipping")

        state.save()

        interval = next_interval(state, now)
        if interval is None:
            log.info("sleeping — heartbeat suspended")
            interval = 60  # poll for wake
        time.sleep(interval)


if __name__ == "__main__":
    main()
