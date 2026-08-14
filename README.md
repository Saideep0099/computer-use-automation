# Computer-Use Automation System

An LLM discovers how to complete a task in a legacy UI **once**; the successful run is
compiled into a typed, versioned **capability artifact**; the artifact then **replays
deterministically** — no model in the loop — with a declared error taxonomy, safety
guardrails, and a real human-escalation path over the live session.

Built for the interface.ai take-home. The target application is a deliberately hostile,
self-contained stand-in for a legacy credit-union teller console (server-rendered,
table layouts, iframe work area, no ids or test attributes, ambiguous button labels),
served as **two tenants of the same vendor product** configured differently.

```
target_app/   the "LegacyCU Teller Console" stand-in (Flask, two tenants, fault injection)
agent/        LLM-driven discovery loop (observe -> decide -> act) + policy guardrails
artifacts/    trace -> capability compiler, outcome library, tenant overrides
replay/       deterministic replayer (no LLM) + handoff seam test
operator/     escalation & handoff: control-state machine + operator console
evidence/     discovery + replay runs: traces, results, screenshots, handoff records
REPORT.md     design write-up
```

## Setup

Requires Python 3.11+.

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

The **only** credential needed is an Anthropic API key, and only for the discovery run
(replay never calls a model):

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Everything runs locally; no external services. Replay, the target app, the compiler,
and the handoff demo all work **without any key**.

## Demo path

**0. Start the target app** (keep running in its own terminal):

```bash
cd target_app && python app.py        # http://127.0.0.1:5001
```

Seeded members: `12345` (active), `23456` (active), `99999` (does not exist),
`55555` (restricted). Fault injection: `?interstitial=1`, `?slow=1`, `?crash=1`.
Tenants: `?tenant=alpha` (default), `?tenant=beta`.

**1. Discovery — the LLM figures out the flow** (add `--headed` to watch):

```bash
cd agent
python discover.py --driver llm --headed \
  --goal "Look up member 12345 and read the savings balance" \
  --url "http://127.0.0.1:5001" --params member_id=12345 \
  --out ../evidence/discovery-run-1
```

(`--driver scripted` runs the same pipeline with a deterministic stand-in driver —
useful for testing the plumbing without a key.)

**2. Compile the trace into a capability artifact:**

```bash
cd ../artifacts
python compile.py --trace ../evidence/discovery-run-1/trace.json \
  --name member_savings_lookup --out member_savings_lookup.capability.json
```

**3. Deterministic replays — no LLM, new inputs, full outcome taxonomy:**

```bash
cd ../replay
# success with an input never seen in discovery
python replay.py --artifact ../artifacts/member_savings_lookup.capability.json \
  --params member_id=23456 --out ../evidence/replay-23456
# business outcome (a legitimate result, not a crash)
python replay.py --artifact ../artifacts/member_savings_lookup.capability.json \
  --params member_id=99999 --out ../evidence/replay-notfound
# recoverable condition: injected interstitial is auto-dismissed
python replay.py --artifact ../artifacts/member_savings_lookup.capability.json \
  --params member_id=12345 --entry-url "http://127.0.0.1:5001/?interstitial=1" \
  --out ../evidence/replay-interstitial
# hard failure: injected HTTP 500 -> APP_ERROR with debuggable detail
python replay.py --artifact ../artifacts/member_savings_lookup.capability.json \
  --params member_id=12345 --entry-url "http://127.0.0.1:5001/?crash=1" \
  --out ../evidence/replay-crash
```

**4. Cross-tenant reuse — alpha-recorded artifact replayed on tenant beta:**

```bash
python replay.py --artifact ../artifacts/member_savings_lookup.capability.json \
  --params member_id=12345 branch_code=07 \
  --override ../artifacts/overrides/tenant-beta.json \
  --out ../evidence/replay-tenant-beta
```

**5. Escalation & handoff** (run headed; you play the operator):

```bash
python replay.py --artifact ../artifacts/member_savings_lookup.capability.json \
  --params member_id=12345 --entry-url "http://127.0.0.1:5001/?tenant=beta" \
  --escalate --headed --out ../evidence/replay-escalation-human
```

Replay hits an unresolvable target (beta without its override), pauses, and serves an
operator console at `http://127.0.0.1:5099` with the context and screenshot. Complete
the search manually **in the automation's own browser window** (Client ID `12345`,
Branch Code `07`, OK), click *Hand control back*, and automation verifies the state,
extracts the output, and reports a `human_assisted` success — with your actions
captured in `handoff_record.json`. `replay/test_handoff.py` exercises the same seam
with a simulated operator for keyless testing.

## Result contract (what a calling agent receives)

```json
{ "status": "success | business_outcome | hard_failure",
  "outcome_id": "MEMBER_NOT_FOUND | ACCESS_DENIED | APP_ERROR | ...",
  "outputs": { "savings_balance": "$4,812.77" },
  "failed_step": "s2", "expected": "...", "observed": "...",
  "human_assisted": true, "evidence_dir": "evidence/..." }
```

See `REPORT.md` for the design reasoning and `evidence/` for recorded runs of every
scenario above, including the real LLM discovery run.
