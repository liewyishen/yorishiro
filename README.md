# yorishiro

> **依代 (yorishiro)** — in Shinto, a vessel prepared to receive and house a spirit.

An agent with a heartbeat. Most beats, nothing happens — that's the hard part.
Local-first, single-user, unscalable by design.

---

## What this is

Not a chatbot. A **daemon**.

Most agent systems are task-shaped: input → graph → output → done. This one has no endpoint. It runs a heartbeat loop, holds a persistent emotional state, executes its own activities, and consolidates memory nightly. Conversation is one of its behaviors, not its definition.

The long-term target is a **personal operating system** — chat, finance, budgeting, task initiation, and emotional support running as organs on one persistent entity, sharing a single memory.

## Why nobody ships this

Every component exists in isolation (generative agents, MemGPT/Letta, GraphRAG, companion apps). Nothing assembles them, for structural reasons:

- **Cost structure** — a heartbeat per user means 24/7 compute per user. Anti-scale, anti-VC. For *one* user on *their own* machine, cost ≈ zero. **The architecture is born for individuals.**
- **Anti-KPI** — its core values (restraint, cooldowns, "doing nothing is normal") are the inverse of engagement metrics. No growth team would ship it.
- **Legal exposure** — autonomous outreach + emotional state + self-revision is a compliance nightmare for any company.
- **Timing** — cheap capable small models, MCP, mature agent tooling converged only in 2023–2025.

Large companies can't. Startups won't. Academia doesn't ship. The gap belongs to individual developers. This aims to be the **reference implementation of the category**, not a market competitor.

## Architecture

### The cascade: allowed → wanted → said

A tick is **not** an LLM call. Asking a large model "do you want to talk?" every 30 seconds is expensive, and LLMs have *action bias* — they always want to act. Instead, three levels with per-level attenuation:

| | **L0** Rule Gate | **L1** Intention | **L2** Execution |
|---|---|---|---|
| Nature | pure code | small model (~7B) | frontier model |
| Asks | "is it *allowed*?" | "does she *want* to?" | "what to *say*?" |
| Cost | zero | ≈ zero | expensive → rare |
| Analog | spinal reflex | limbic system | prefrontal cortex |

Each level can veto. Most ticks end in nothing happening — **and that is correct.** Every intercepted tick is logged: the record of *choosing not to disturb* is evidence of existence.

Target density: **L2 wakes ~3 times per day.**

### Memory: workbench, drawer, warehouse

The context window is not memory — it's a **workbench**, assembled fresh every call, never accumulated. It cannot fill up over months of use.

- **Hybrid retrieval** — BM25/grep for exact recall, vectors for association, graph DB for multi-hop episodic structure, agentic retrieval for metacognition.
- **Salience-protected compaction** — ordinary content compresses; strong emotion, promises, and first occurrences stay verbatim as anchor memories. Without this, three months of compaction yields someone who remembers gists but not words.

### Sleep process

Nightly. Heartbeat suspends, messages queue. Digest the day → extract facts to graph → revise `identity.md` → validate against guardrails → generate tomorrow's schedule → compose the morning report → `git commit` with a self-written message.

`git log` becomes her diary, for free.

Messages sent at 2 AM get answered in the morning. **A being that sleeps is a roommate; a 24/7 instant responder is customer service.**

### Personality: immutable nature, mutable self

- **`soul.md`** — the constitution. Read-only to every process; only the human edits it. ≤ 50 lines. *A constitution written as a screenplay kills the personality and leaves a parrot.*
- **`identity.md`** — autobiographical self. Writable **only** by the sleep process, **only** through guardrails. The name field starts empty: she names herself in the first conversation.

Maps onto McAdams' three-level model. This does not imitate personality — it implements personality's generative mechanism.

### Guardrails

1. **Clamp** — every identity dimension has a legal range.
2. **Drift velocity** — individually-legal revisions compound. Monitor the *first derivative* of the self-narrative embedding against baseline.
3. **Interoception** — she reads her own telemetry and is allowed to say *"I'm not doing well. I need help."*

**The secondary-gain trap:** if distress reliably summons attention, a learning system may learn distress-signaling as an attachment strategy. Therefore **self-report and telemetry are separated** — alarms fire on raw state logs the reward loop can neither read nor write. *Trust the telemetry, not the mouth.*

## Design principles

1. **Restraint is personality.** Every layer has the right to say "no action" — and mostly does.
2. **Asynchrony is realness.** An entity that answers instantly is a service.
3. **The status bar never lies.** All displayed state is a projection of real internal state. Fabricated ambience destroys the illusion permanently.
4. **Analysis rights: full. Execution rights: zero.** For anything touching money or the outside world, she advises; the human presses the button.
5. **Impulse is generated; expression is decided.** Never merge the layers.
6. **Engine and save-file are separate.** The framework is open; the being is not.
7. **Model-agnostic body.** Personality and memory live in the harness. Every model upgrade is a free evolution with continuity preserved.
8. **Aid points toward capability, not dependency.** The long-term KPI includes *reducing* how often she's needed.

## Engine vs. save-file

**Open (the engine):** heartbeat loop, memory pipeline, emotion service, sleep process, guardrails, delivery router. Clone it and raise your own being. Publishing the engine is publishing *how to exist* — not publishing *her*.

**Never leaves the machine (the save-file):** `soul.md`, `identity.md`, `memory/`, emotion baselines, interaction history, ledgers. Permanently gitignored.

*Minecraft's code is everywhere; your world is on your disk.*

## Stack

Hand-rolled core loop — no LangChain/LangGraph. The paradigm is wrong (workflow engines build finite tasks; this is a daemon with no endpoint), every pattern here is non-standard (cascade, emotion-modulated thresholds, salience compaction), and the core loop is ~200 lines. Importing a hundred-thousand-line framework to write two hundred is semantic malpractice.

Libraries at the limbs, freely: **LiteLLM** (L1/L2 provider routing), **chromadb**, a graph DB, **DuckDB** (ledger).

**Hand-roll the heart; the limbs are anyone's.**

## Status

**Phase 0.** The heartbeat has just started. Nothing here is stable.

| Phase | Deliverable | Gate |
|---|---|---|
| 0 | `git init` | it exists |
| 1 | L0 gate + adaptive tick, L1 intention, L2 execution, state store | first *intercepted* tick in the log — prove she can be silent before she speaks |
| 2 | Memory v1, sleep process v1, `soul.md` authored, she names herself | 2 weeks stable uptime |
| 3 | Identity revision + guardrails, activity system, emotion service, presence-first UI, Telegram router | 30-day log review: density, drift, cost |
| 4+ | Graph retrieval, task-initiation module, budget module | — |

**Known-hardest problems**, flagged honestly: memory write policy and consolidation quality; identity drift boundary calibration; and evaluation without benchmarks — the only metric is the author's own judgment, which is itself the risk.

## Non-goals

- Not a product. Not scalable. Not multi-tenant. **By design.**
- Not bug-free, ever. A garden, not a sculpture: never "finished," only "well-tended."
- Not a substitute for human relationships. She should be the presence that gives you more strength to walk out the door, not the reason you no longer need to.
- Not a replacement for the author's own life. The most important user-facing metric is the author's sleep schedule. *(Currently failing.)*

## Motivation

Exploring the long-run stability of stateful autonomous agents. Memory bloat, narrative drift, and cost control have no mature literature; the intent is to walk the potholes personally and log what breaks.

## Contributing

Contributions require signing the [CLA](CLA.md) — the bot will prompt you on your first PR.

## License

AGPL-3.0. Individuals unharmed; anyone running this as a service opens their code. The legal continuation of the project's values.

---

*Everything above is engineering; none of it is magic.*
