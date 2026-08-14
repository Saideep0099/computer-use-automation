"""
Deterministic replayer: capability artifact + input params -> structured result.
NO LLM anywhere in this file — this is the path an AI agent triggers in production.

Determinism strategy (REPORT.md material):
  - Targets are resolved by a RANKED FALLBACK CHAIN, most-semantic first:
      1. role + accessible/derived name, in the recorded frame
      2. role + name, any frame          (frame layout drifted)
      3. role only, if exactly one match (label text drifted, control unique)
    If all fail -> hard failure with expected-vs-observed detail. We never fall
    back to coordinates in replay: a wrong-element click in a banking app is
    worse than a clean stop.
  - Explicit waits: settle after actions + bounded retry on empty observation
    (same lesson as discovery Bug #1), never unbounded sleeps.
  - After every step and at the end, the page is checked against the artifact's
    declared OUTCOME taxonomy: recoverable conditions get their scripted
    recovery and the run continues; business outcomes end the run as a valid
    result; anything undeclared that breaks the flow is a hard failure.
  - The checkpoint assertion must pass before outputs are extracted — we verify
    we reached the expected state instead of assuming clicks worked.

Result contract (what the calling agent receives):
  { "status": "success" | "business_outcome" | "hard_failure",
    "outcome_id": ..., "outputs": {...}, "message": ...,
    "failed_step": ..., "expected": ..., "observed": ...,
    "evidence_dir": ..., "steps_executed": N, "duration_ms": T }

Tenant overrides (--override): a small JSON that maps target names / adds steps
for a sibling tenant of the same vendor product — reuse, not re-record.

Usage:
  python replay.py --artifact ../artifacts/member_savings_lookup.capability.json \
      --params member_id=23456 --out ../evidence/replay-23456
"""
import argparse, json, pathlib, re, sys, time
import yaml
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "agent"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "operator"))
from discover import observe, Policy   # reuse the same perception + guardrails
from handoff import Handoff


def page_text(page):
    return observe(page)["text"]

def settle(page):
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(250)

# ------------------------------------------------------------- target resolve

def resolve(page, target):
    """Ranked fallback chain. Returns (locator, how) or (None, attempts)."""
    role, name, hint = target["role"], target.get("name") or "", target.get("frame_hint", 0)
    attempts = []
    frames = page.frames
    ordered = ([frames[hint]] if hint < len(frames) else []) + \
              [f for i, f in enumerate(frames) if i != hint]
    # 1 & 2: engine-computed accessible role + name (recorded frame first)
    if name:
        for f in ordered:
            loc = f.get_by_role(role, name=re.compile(re.escape(name.rstrip(':').strip()), re.I))
            if loc.count() == 1:
                return loc.first, f"a11y role+name in frame {frames.index(f)}"
            attempts.append(f"a11y role+name '{name}' in frame {frames.index(f)}: {loc.count()} matches")
    # 3: OUR derived labels — legacy apps put labels in adjacent table cells,
    #    which engine accessible-name computation cannot see. We re-run the same
    #    perception the recorder used and match role+derived-name, then address
    #    the control positionally among same-role elements in its frame.
    if name:
        want = name.rstrip(':').strip().lower()
        from discover import JS_COLLECT
        for f in ordered:
            try:
                els = f.evaluate(JS_COLLECT)
            except Exception:
                continue
            same_role = [e for e in els if e["role"] == role]
            hits = [i for i, e in enumerate(same_role)
                    if e["name"].rstrip(':').strip().lower() == want]
            if len(hits) == 1:
                return f.get_by_role(role).nth(hits[0]), \
                       f"derived-label role+name in frame {frames.index(f)} (pos {hits[0]})"
            attempts.append(f"derived-label '{name}' in frame {frames.index(f)}: {len(hits)} matches")
    # 4: role only, if unique across the whole page
    matches = [(f, f.get_by_role(role)) for f in frames]
    uniq = [(f, l) for f, l in matches if l.count() == 1]
    if len(uniq) == 1 and sum(l.count() for _, l in matches) == 1:
        return uniq[0][1].first, "unique role match"
    attempts.append(f"role-only '{role}': {sum(l.count() for _, l in matches)} matches across frames")
    return None, attempts

# --------------------------------------------------------------- outcome scan

def scan_outcomes(page, outcomes):
    text = page_text(page)
    # app-level error pages are first-class hard failures with a clear name
    if re.search(r"(HTTP 5\d\d|Internal Server Error|ORA-\d+)", text):
        return ("hard", "APP_ERROR", text[:200])
    for oc in outcomes.get("business", []):
        pat = oc["detect"].get("text_regex")
        if pat and re.search(pat, text):
            m = re.search(pat, text)
            msg = m.group(0)
            return ("business", oc["id"], msg)
    for oc in outcomes.get("recoverable", []):
        pat = oc["detect"].get("text_regex")
        if pat and re.search(pat, text):
            return ("recoverable", oc["id"], oc)
    return (None, None, None)

def apply_recovery(page, oc, log):
    for act in oc["recovery"]:
        if act["action"] == "click":
            loc, how = resolve(page, act["target"])
            if loc is None:
                return False
            loc.click(); settle(page)
            log.append({"recovery": oc["id"], "did": f"click {act['target']}", "via": how})
        elif act["action"] == "retry_wait":
            page.wait_for_timeout(act.get("timeout_ms", 5000))
            log.append({"recovery": oc["id"], "did": "waited"})
    return True

# ------------------------------------------------------------------ extraction

def extract_outputs(page, outputs):
    text = page_text(page)
    got, missing = {}, []
    for o in outputs:
        label = o["extract"]["label"]
        # 'Label' then value on same or next line (legacy table rendering)
        m = re.search(rf"{re.escape(label)}\s*[:\t\n]\s*([^\n\t]+)", text)
        if m:
            got[o["name"]] = m.group(1).strip()
        else:
            missing.append(o["name"])
    return got, missing

# ---------------------------------------------------------------------- merge

def apply_override(artifact, override):
    """Tenant override: rename targets, patch entry_url, insert extra steps."""
    art = json.loads(json.dumps(artifact))     # deep copy
    for old, new in override.get("target_renames", {}).items():
        for s in art["steps"]:
            if s.get("target", {}).get("name", "").strip().rstrip(":") == old.strip().rstrip(":"):
                s["target"]["name"] = new
    if "entry_url" in override:
        art["surface"]["entry_url"] = override["entry_url"]
    for ins in override.get("insert_steps", []):
        idx = next(i for i, s in enumerate(art["steps"]) if s["id"] == ins["before"])
        art["steps"].insert(idx, ins["step"])
        art["inputs"].extend(ins.get("adds_inputs", []))
    return art

# ----------------------------------------------------------------------- run

def replay(artifact_path, params, outdir, override_path=None, entry_url=None, headed=False,
           escalate=False, _simulate_human=None):
    t0 = time.time()
    out = pathlib.Path(outdir); out.mkdir(parents=True, exist_ok=True)
    artifact = json.loads(pathlib.Path(artifact_path).read_text())
    if override_path:
        artifact = apply_override(artifact, json.loads(pathlib.Path(override_path).read_text()))
    if entry_url:
        artifact["surface"]["entry_url"] = entry_url

    policy = Policy(str(pathlib.Path(__file__).resolve().parent.parent / "agent" / "policy.yaml"))
    missing_inputs = [i["name"] for i in artifact["inputs"] if i.get("required") and i["name"] not in params]
    if missing_inputs:
        return finish(out, t0, status="hard_failure", outcome_id="MISSING_INPUTS",
                      message=f"required inputs not provided: {missing_inputs}")

    url = artifact["surface"]["entry_url"]
    if not policy.check_url(url):
        return finish(out, t0, status="hard_failure", outcome_id="POLICY_BLOCKED",
                      message=f"entry url outside allowlist: {url}")

    log, result = [], None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        try:
            page.goto(url, timeout=10000)
        except Exception as e:
            browser.close()
            return finish(out, t0, status="hard_failure", outcome_id="LOAD_FAILED",
                          message=str(e)[:200], log=log)
        settle(page)

        for step in artifact["steps"]:
            # pre-step: handle declared recoverable states (e.g. interstitial)
            for _ in range(2):
                cls, oid, payload = scan_outcomes(page, artifact["outcomes"])
                if cls == "recoverable":
                    ok = apply_recovery(page, payload, log)
                    if not ok:
                        result = dict(status="hard_failure", outcome_id=oid,
                                      failed_step=step["id"],
                                      message="declared recovery could not be applied")
                    continue
                if cls == "business":
                    result = dict(status="business_outcome", outcome_id=oid, message=payload)
                if cls == "hard":
                    result = dict(status="hard_failure", outcome_id=oid,
                                  failed_step=step["id"], observed=payload,
                                  message="application reported an error state")
                break
            if result: break

            page.screenshot(path=str(out / f"{step['id']}.png"))
            if step["action"] == "navigate":
                if not policy.check_url(step["url"]):
                    result = dict(status="hard_failure", outcome_id="POLICY_BLOCKED",
                                  failed_step=step["id"], message=f"navigate outside allowlist")
                    break
                page.goto(step["url"]); settle(page)
                log.append({"step": step["id"], "did": f"navigate {step['url']}"})
                continue

            loc, how = resolve(page, step["target"])
            if loc is None:
                result = dict(status="hard_failure", outcome_id="TARGET_UNRESOLVED",
                              failed_step=step["id"],
                              expected=step["target"], observed=how,
                              message="no locator strategy resolved the target")
                break
            if step["action"] == "type":
                v = step["value"]
                text = str(params[v["from_input"]]) if "from_input" in v else v["literal"]
                loc.fill(text)
                log.append({"step": step["id"], "did": f"type into {step['target']['name']}",
                            "via": how, "value": policy.redact(text)})
            elif step["action"] == "click":
                loc.click()
                log.append({"step": step["id"], "did": f"click {step['target']['name']}", "via": how})
            settle(page)

        # ---- escalation: a hard failure mid-run hands the LIVE session to a human
        escalation_record = None
        if result and result["status"] == "hard_failure" and escalate:
            ho = Handoff(page, out, {
                "capability": artifact["capability"]["id"],
                "version": artifact["capability"]["version"],
                "step": result.get("failed_step"),
                "reason": f"{result['outcome_id']}: {result.get('message','')}",
                "goal": artifact["capability"]["description"],
            })
            escalation_record = ho.run(simulate_human=_simulate_human)
            settle(page)
            result = None   # automation resumes: re-verify state honestly below

        # post-steps: final outcome scan, checkpoint, extraction
        if result is None:
            cls, oid, payload = scan_outcomes(page, artifact["outcomes"])
            if cls == "business":
                result = dict(status="business_outcome", outcome_id=oid, message=payload)
            elif cls == "hard":
                result = dict(status="hard_failure", outcome_id=oid, observed=payload,
                              message="application reported an error state")
        if result is None:
            expected_texts = artifact["checkpoint"]["assert"]["any_visible_text"]
            text = page_text(page)
            if not any(t in text for t in expected_texts):
                result = dict(status="hard_failure", outcome_id="CHECKPOINT_FAILED",
                              failed_step=artifact["checkpoint"]["after_step"],
                              expected=f"any of {expected_texts} visible",
                              observed=text[:300],
                              message="did not reach the expected end state")
        if result is None:
            outputs, missing = extract_outputs(page, artifact["outputs"])
            if missing:
                result = dict(status="hard_failure", outcome_id="EXTRACTION_FAILED",
                              expected=f"outputs {missing}", observed=page_text(page)[:300],
                              message="checkpoint passed but declared outputs not found")
            else:
                result = dict(status="success", outcome_id=None, outputs=outputs, message="ok")

        page.screenshot(path=str(out / "final.png"))
        browser.close()

    if escalation_record is not None:
        result["escalation"] = {"escalated": True,
                                "human_actions_captured": len(escalation_record["human_actions"]),
                                "handed_back": escalation_record["handed_back"]}
        result["human_assisted"] = True

    return finish(out, t0, log=log, artifact_id=artifact["capability"]["id"],
                  artifact_version=artifact["capability"]["version"],
                  params={k: Policy(str(pathlib.Path(__file__).resolve().parent.parent / "agent" / "policy.yaml")).redact(str(v)) for k, v in params.items()},
                  **result)

def finish(out, t0, **kw):
    kw.setdefault("outputs", {})
    kw["steps_executed"] = len(kw.get("log", []))
    kw["duration_ms"] = int((time.time() - t0) * 1000)
    kw["evidence_dir"] = str(out)
    (out / "result.json").write_text(json.dumps(kw, indent=2, default=str))
    print(json.dumps({k: kw[k] for k in ("status", "outcome_id", "outputs", "message") if k in kw},
                     indent=2, default=str))
    print(f"result -> {out/'result.json'}")
    return kw

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--params", nargs="*", default=[])
    ap.add_argument("--out", required=True)
    ap.add_argument("--override", help="tenant override json")
    ap.add_argument("--entry-url", help="override entry url (e.g. fault-injection flags)")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--escalate", action="store_true",
                    help="on hard failure, hand the live session to a human operator")
    a = ap.parse_args()
    params = dict(kv.split("=", 1) for kv in a.params)
    r = replay(a.artifact, params, a.out, a.override, a.entry_url, a.headed, a.escalate)
    sys.exit(0 if r["status"] in ("success", "business_outcome") else 1)
