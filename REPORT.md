# Design Write-Up

## Architecture

Four components with one deliberate seam between them:

**Target app** — a self-built stand-in, the *LegacyCU Teller Console*: server-rendered
tables, an iframe work area, no ids/test attributes, ambiguous labels ("Go"/"OK"),
served as two differently-configured tenants of the same "vendor product", with
deterministic fault injection (`?interstitial=1`, `?slow=1`, `?crash=1`) and seeded
business states (not-found, restricted). Trade-off: a public demo site would have been
zero effort, but it can't demo runtime errors on command, carries ToS risk, and isn't
domain-matched. ~180 lines bought a fully controllable laboratory that reviewers can
run offline.

**Discovery agent** (`agent/discover.py`) — an LLM observe → decide → act loop.
Observation walks every frame and presents the model with visible text plus interactive
elements as *(role, derived label)*; ids, classes and xpaths are deliberately never
shown, so the recorded trace is born in surface-agnostic vocabulary. The action space
is six strict-JSON actions (click / type / navigate / extract / done / stuck), one per
model turn; every action passes the policy allowlist *before* execution. Output is a
raw trace plus per-step screenshots.

**Compiler** (`artifacts/compile.py`) — turns a successful trace into the capability
artifact: parameterizes typed literals that match run params (`12345` →
`from_input: member_id`), types outputs, derives extraction labels from observed page
structure, generates a checkpoint, attaches the outcome taxonomy, stamps
`schema_version` + capability version + a draft/approved review state. The model
transcript can be discarded without touching the production path.

**Replayer** (`replay/replay.py`) — no LLM anywhere. Ranked target resolution,
explicit waits, outcome scanning before every step, checkpoint verification before
extraction, and a three-way result contract. Escalation (below) is a mode of the
replayer, not a separate system, because "stuck" is discovered mid-run on a live
session. Single process, synchronous: at one-capability-at-a-time granularity there is
nothing a queue would buy except operational surface; the seam to a queued service is
the replayer's function signature (artifact + params → result JSON).

## Artifact schema

A capability is a *contract*, not a step list (`artifacts/*.capability.json`):

- `capability`: id, integer `version`, description, provenance (which trace/driver),
  `review.state` (draft → approved) as the gate for unattended replay.
- `inputs` / `outputs`: typed (`member_id: number, required`;
  `savings_balance: money` with a `labeled_value` extraction spec). This is what makes
  the artifact *agent-invocable*: a calling agent can validate arguments and know the
  return shape without reading the steps.
- `steps`: ordered actions whose targets are **semantic descriptors** —
  `{role, name, frame_hint}` — never selectors. Rationale: on the surfaces this system
  targets, role + human-visible label is the only identity that is stable across
  markup churn and tenant re-branding; *how* a descriptor is resolved is the replay
  engine's job, which keeps the artifact portable across surfaces.
- `checkpoint`: an asserted end-state, so replay verifies it arrived rather than
  assuming clicks worked.
- `outcomes`: the declared error taxonomy (below), shipped with the artifact so a
  reviewer sees the full behavioral contract — including failure behavior — in one
  document.

## Determinism & error handling

Replay never asks a model anything; determinism comes from resolution + waiting +
verification. Target resolution is a ranked fallback chain: (1) engine accessible
role+name in the recorded frame, (2) same in any frame, (3) **derived-label** — the
replayer re-runs the recorder's own perception (labels live in adjacent table cells,
invisible to engine accessible-name computation) and addresses the control positionally
among same-role elements, (4) role-only if globally unique. We deliberately never fall
back to coordinates: in a banking app a wrong-element click is worse than a clean stop.
The chain was proven necessary in testing — tenant alpha resolved by luck (single
textbox), tenant beta exposed that engine a11y names were empty for *all* fields; the
derived-label tier fixed it. Lesson: recorder and replayer must share one perception
model.

Runtime conditions are classified by the artifact's taxonomy, checked before every
step and at the end:

- **Expected business outcomes** (`MEMBER_NOT_FOUND`, `ACCESS_DENIED`,
  `VALIDATION_ERROR`) return `status: business_outcome` with the message — a
  legitimate answer, never a crash.
- **Recoverable conditions** (`SESSION_NOTICE_INTERSTITIAL`, transient slow load)
  carry a scripted recovery (bounded attempts); replay recovers and continues.
- **Hard failures** (`APP_ERROR` on 5xx/ORA patterns, `TARGET_UNRESOLVED`,
  `CHECKPOINT_FAILED`, `EXTRACTION_FAILED`, `MISSING_INPUTS`, `POLICY_BLOCKED`) stop
  with failed step, expected vs. observed, and screenshots.

UI drift (secondary, per the brief) surfaces as `TARGET_UNRESOLVED`/`CHECKPOINT_FAILED`
with the attempted strategies listed — the debugging trail is part of the contract.

## Heterogeneity & multi-tenant

**Surface abstraction.** The seam is *perceive/act* vs. *the recorded flow*. Artifacts
speak only role/label/value; the replay engine owns resolution and execution. Extending
to a desktop app means implementing the same small interface (observe → elements+text;
resolve; click/type) over an OS accessibility API (which exposes the same role/name
vocabulary); artifacts are unchanged. Legacy web is not a future case — the target app
*is* one, and the derived-label tier exists precisely because of it.

**Multi-tenant reuse.** One *base artifact* per vendor product + small per-tenant
*overrides* (target renames, entry URL, inserted/removed steps with their added
inputs), merged at load time — demonstrated live: the alpha-recorded artifact replays
on beta (relabeled controls + an extra required Branch Code field) via
`artifacts/overrides/tenant-beta.json`, no re-recording. The outcome library is
per-product and shared, since its detections are text patterns, not selectors. Drift
management: every replay already emits which resolution tier each target needed;
aggregated per tenant, sustained fallback-tier degradation or checkpoint failures are
the drift signal, and the response is a per-tenant override (or a re-discovery
proposing one) rather than a fork of the base artifact.

## Escalation & handoff

"Stuck" is any hard failure with `--escalate` enabled (discovery's `stuck` action is
the same seam). Control is an explicit token in a persisted state machine:
`AUTOMATION → PAUSED → HUMAN → RESUMING → AUTOMATION`, with history in
`control_state.json` — at any instant it is unambiguous who may act. On escalation the
system writes an intervention request (capability, version, step, reason, goal) plus a
screenshot, and serves a minimal operator console (localhost:5099) showing that
context. The human operates **the same live browser session** the automation was
using — same cookies, same app state — while injected DOM listeners record each of
their clicks/inputs in semantic form into `handoff_record.json`. On hand-back,
automation does not trust blindly: it re-runs the outcome scan, checkpoint, and
extraction, then reports success with `human_assisted: true` and the action count.
Verified two ways: a simulated-operator harness (`replay/test_handoff.py`) and a real
human run (5 actions captured) — both in `evidence/`. One non-obvious bug worth
noting: a plain thread wait starved Playwright's sync event loop so capture callbacks
never fired; the wait must pump the loop.

## Safety

`agent/policy.yaml` is enforced in both discovery and replay: a URL-pattern allowlist
checked at entry and on every navigation, an action-type allowlist checked before
execution, and redaction patterns (SSN, card-like numbers, credential shapes) applied
to everything persisted — typed values, logs, artifacts. Risk model: reads and
navigation are safe; this capability is read-only by construction. For mutating
capabilities the artifact's `review.state` is the gate (draft artifacts don't replay
unattended) and a risky step would carry `requires_approval`, routing through the same
escalation seam as a human decision point — conservative default: block and ask.
Limits, stated honestly: redaction is pattern-based (a rendered balance is not
redacted — it *is* the requested output; callers own downstream handling); the
allowlist is coarse (URL + action type, not per-element); and prompt-injection via
hostile page text is mitigated only by the strict JSON action schema, the allowlist,
and escalation-on-risk, not solved.

## Cuts

Cut deliberately: an artifact **catalog/API** (the capability file + replay signature
*is* the contract; a FastAPI wrapper adds nothing to the design argument);
**co-browsing operator UI** (mocked as a minimal local page — the control-transfer
model is the real content); **desktop surface** (designed for via the perception seam,
not built); **multi-run stability scoring and LLM-assisted fallback** (both fit the
existing seams: the former is a loop around replay, the latter a bounded single-step
re-discovery gated by policy); **queueing/services** (nothing to queue at this scale).
Next with more time: the assisted single-step fallback recorded as evidence, per-tenant
drift dashboards from resolution-tier telemetry, and artifact signing for the approval
workflow.
