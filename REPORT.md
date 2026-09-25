# Design Report

An LLM discovers a flow in a legacy UI once; it becomes a typed, versioned capability; a
person approves it; from then on it replays without an LLM, with typed results, guardrails on
every action, and a handoff to a person on the live session when needed. The target is
CoreOne Teller, a deliberately hostile mock core-banking app (framesets, table layouts,
unlabelled inputs, no ids) with two tenant variants and injectable faults. Everything cited is
in [evidence/](evidence/README.md).

| Real runs (`claude-opus-5`) | |
|---|---|
| Discovery | 7 runs; a lookup takes 5 turns, ~30 s, ~$0.10; 2 drafts caught before approval |
| Replay | no LLM; 5 × 10 unattended stability runs across capabilities and tenants, all passed |
| Second tenant | both capabilities pass on Riverbend with unchanged artifacts plus an overlay |
| Tests | 255 against real Chromium, 9 import-linter contracts, mypy `--strict` |

## 1. Architecture

```
 goal ─► discovery (LLM + recorder) ─► draft ─► verify ×2 ─► approve ─► stability ×10 ─► unattended
                      │                                       │
         GuardedSession: policy → lease → surface → evidence ◄── replay (no LLM)
                                    │
                      control ◄──► workbench (staff) · CLI (engineers) ─► cua.workflows
```

Discovery and replay are two drivers on one chokepoint, `GuardedSession`: every action,
from the model, a replay or a person, passes the policy, the control lease and the evidence
log. They share only the artifact schema; import-linter enforces the boundaries.

- **Perceive through the accessibility tree plus visible text**, not selectors or pixels:
  legacy markup has no ids but exposes roles, names and text, and the same model maps to
  desktop UIA/AX trees. A surface with no tree needs another adapter (§4).
- **The LLM never runs the work.** It discovers, proposes contracts and routes chat
  requests; replay never calls it. It never sees credentials or member data.
- **The artifact is built from what executed, not from what the model said**: each
  successful action with the locator the session verified and the risk the policy applied.
- **Nothing runs before a person approves it, or unattended before it's measured** (§6).
- **One process, synchronous**, every seam a protocol; the cost is one session per process.

Bank staff use a browser workbench, not a CLI. It opens on a chat, *what do you need
done?*: a saved automation comes back as a card with the inputs filled in, run only when the
person presses Run; anything new becomes a draft that a reviewer approves by reading its
steps in plain words, with a screenshot after each, never as JSON.

## 2. Artifact schema

A capability (pydantic, JSON in `catalog/`) has three parts: a **contract** for the caller
(typed inputs with sensitivity and format, typed outputs, business outcomes mapped to
screens, overall risk); **execution** for replay (entry, named targets, ordered steps each
with an intent, action, declared risk and checkpoint, and a success condition); and
**governance** (semver, draft/approved status, provenance, app and version range).

```json
"member_id_field": {"frame": [{"name": "content"}],
  "strategies": [{"by": "near", "text": "Member ID", "direction": "right", "role": "textbox"},
                 {"by": "css", "selector": "input[name=\"mbrno\"]"}],
  "fingerprint": {"role": "textbox", "tag": "input"}}
```

- **Targets are named and separate from steps**, so reviewers read `click search_button` and
  a tenant can override one by name.
- **Strategies are what an operator perceives** (role and name, label, nearby text, table
  cell), CSS last. The first unique match wins and must fit the fingerprint; several matches
  is an error, never a guess; winning by a fallback is reported as drift.
- **Screens live in a per-app state library**, written once per vendor product and shared by
  every tenant; a capability only says which screens are business outcomes for it.
- **Inconsistent artifacts are rejected**, including outcomes on the success path: real
  discovery attempt 1 mapped an error screen onto the search form, and verification caught it.
- **No coordinates, secrets or values**; the store refuses an artifact containing any value
  the run knew was sensitive. Steps are a straight line, so a capability reads in one pass.

## 3. Determinism & error handling

After every step, replay classifies the screen against the state library and polls until the
checkpoint appears or times out; there are no sleeps. Every result is exactly one kind:

| Kind | Meaning | Evidence |
|---|---|---|
| `success` | typed outputs | `recorded/01` |
| `business_outcome` | a result the caller handles, e.g. no such member | `02`, `08b` |
| `failure` | category, step, expected vs observed, retryable | `04` app error, `05` element not found, `07b` timeout |
| `aborted` | a person or the approval gate stopped it | `06` |

**Recoverable conditions are handled in the run and listed:** a notice is dismissed, a 503
retried, an expired session re-authenticated (`03a–c`), each bounded; slowness is a 10 s
budget with one restart (`07a`). **Replay knows what it may have committed:** before the first
state-changing step, failures are retryable and restarts safe; after it, the engine never
restarts and a stuck run goes to a person. **Locators are verified twice before approval**,
with two different members, and any strategy that didn't hold both times is dropped; real
attempt 1 had a fallback anchored on a member's name, which this removed. **Drift is
reported, never guessed:** the lookup on the Riverbend tenant fails explicitly at "Find"
(`05`) until an overlay fixes it (`05b`).

## 4. Heterogeneity & multi-tenant

Everything above `cua.surface` speaks targets and observations, so only the adapter changes.
**Legacy web** is built (Playwright: frames become a frame path, unlabelled inputs resolve by
nearby text or table cell). **Desktop** (Win32, Java via UIA/AX) exposes roles, names and
bounds, so the same strategies apply. **Pixels only** (Citrix, terminals) needs a vision
adapter that turns OCR and layout into the same tree; none of this is built beyond web.

Institutions run the same vendor product differently configured, so reuse is layered: the
**vendor app model** (state library, sign-on, policy), a **base capability** recorded once,
and a **tenant overlay** that replaces named targets and nothing else, so it changes where a
flow acts, never what it does. Riverbend needed four target overrides for the lookup and two
of eight for the form; both base artifacts are unchanged and pass there as `0.1.0+riverbend`
(`05b`, `05c`). Overlays are written by hand and verified like drafts; in production a
drift failure would start discovery scoped to that one target, producing an overlay.

## 5. Escalation & handoff

**Stuck** means: a screen the state library marks for a person (the member alert); after a
commit, an unknown screen, a missed checkpoint or a timeout; an irreversible step needing
approval; or the model asking for help during discovery. Before any commit, a clean retryable
failure is cheaper than a person.

**Control** is a lease on the live session with an epoch: whoever takes it invalidates every
earlier token, and the session checks its token just before acting, so the agent can't race
an operator. A request carries the run, step, reason and a screenshot, and appears under
*Needs you*. The person acts in the same browser session (their clicks are recorded), then
hands back; the engine takes a new lease, re-reads the screen and continues, and the result
lists the intervention. Unattended runs fail closed. Evidence: run `20260923T194339Z-f6c82a`
and the console screenshots. Mocked: no login, one local session.

## 6. Safety

**Hard limits for every actor:** origin, path and action-kind allowlists (key presses are
off: they can submit a form with no button to judge); only the runtime types passwords; at
most 200 actions and one irreversible action per run. **Risk is never taken on trust:** the
policy infers it from the element, the artifact declares it, the higher wins. **Irreversible
actions need a person's approval every time**; I chose approval over blocking because banks
need them done. **Unattended use is measured, not granted:** ten clean runs, no drift, within
30 days, for that exact version, or it only runs with a person.

**Data:** credentials resolve at the last moment and never reach the model or logs;
redaction is value-based at every sink, with placeholders like `«inputs.member_id»`; the chat
takes numbers and amounts out before the model sees a message. Found in real runs and tests
and fixed: an unredacted verification result in the catalog, a member's name in a locator,
and a name in step-screenshot text. **Limits:** redaction can't know data nobody declared, so
screenshots show whatever was on screen; names typed into the chat aren't detected; risk
inference reads button names, so an "OK" that posts money needs per-app rules and review.

## 7. Cuts

Deliberately left out: desktop and vision adapters; LLM-drafted overlays and a version check;
login, roles and a handoff streamed to the operator; queues and workers (one session per
process); proving irreversible flows by replay (verification must not commit, so opening an
account is a hand-written fixture); cost and rate limits; on-screen data detection.

Stretch goals: cross-tenant reuse with per-variant overrides, and confidence & approval (a
stability score gating unattended runs). The chat invoking saved automations by name is
close to the agent-facing interface, which I don't claim.

**In production** replay runs next to the legacy app behind a queue, staff use SSO with
four-eyes approval, and discovery happens only on a sandbox tenant. Next: scoped discovery
for drift, a typed tool catalog for agents, on-screen entity detection, sandbox verification
of irreversible flows, and stability from real runs.
