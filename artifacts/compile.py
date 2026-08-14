"""
Artifact compiler: raw discovery trace  ->  typed capability artifact.

Why a separate compiler (design rationale for REPORT.md):
  The trace is evidence of ONE concrete run (member 12345, tenant alpha, model
  chatter included). The capability is a CONTRACT: parameterized inputs, typed
  outputs, semantic targets with fallbacks, checkpoints, and a declared outcome
  taxonomy. Decoupling them (brief 3.2) means the model transcript can be thrown
  away or redacted without touching the production execution path, and the
  artifact can be reviewed/diffed/approved like code.

Parameterization: any typed literal that equals a --params value from the run is
replaced with {"from_input": <name>} — the same idea as canonicalizing
/item/12345 -> /item/:id, applied to form input.

Outcome taxonomy: seeded from outcome_library.yaml — a curated, growing library
of detection rules for this app family (business outcomes, recoverable
conditions). In production this library is per-vendor-product and shared across
tenants; here it ships with the repo. Hard failure is always the fallback class.

Usage:
  python compile.py --trace ../evidence/discovery-run-1/trace.json \
      --name member_savings_lookup --out member_savings_lookup.capability.json
"""
import argparse, json, pathlib, re, time
import yaml

SCHEMA_VERSION = "capability/v1"

def infer_type(value):
    if re.fullmatch(r"-?\$?[\d,]+(\.\d+)?", str(value)):
        return "money" if "$" in str(value) else "number"
    return "string"

def label_for_value(obs_text, value):
    """Find the label preceding an extracted value in the observed page text.
    Legacy tables render as 'Label <tab/newline> Value'."""
    lines = [l.strip() for l in obs_text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if value in line:
            before = line.split(value)[0].strip().strip(":\t ")
            if before:
                return before
            if i > 0:
                return lines[i - 1].strip().strip(":\t ")
    return None

def compile_trace(trace_path, name, out_path, description=None):
    trace = json.loads(pathlib.Path(trace_path).read_text())
    if trace.get("status") != "success":
        raise SystemExit(f"refusing to compile a non-successful trace (status={trace.get('status')})")
    params = trace.get("params", {})
    lib_path = pathlib.Path(__file__).parent / "outcome_library.yaml"
    outcomes = yaml.safe_load(lib_path.read_text()) if lib_path.exists() else {}

    steps, inputs_seen = [], {}
    for s in trace["steps"]:
        a = s["action"]; kind = a["action"]
        if kind in ("done", "stuck", "extract"):
            continue
        step = {"id": f"s{len(steps)+1}", "action": kind}
        if "target" in s:
            step["target"] = {
                "role": s["target"]["role"],
                "name": s["target"]["name"],
                "frame_hint": s["target"].get("frame_index", 0),
                # ranked resolution strategy lives in the REPLAYER; the artifact
                # records only the semantic identity + a hint.
            }
        if kind == "type":
            typed = s.get("typed", a.get("text", ""))
            matched = next((k for k, v in params.items() if str(v) == str(typed)), None)
            if matched:
                step["value"] = {"from_input": matched}
                inputs_seen[matched] = {"name": matched, "type": infer_type(typed),
                                        "required": True,
                                        "description": f"value entered into '{step['target']['name']}'"}
            else:
                step["value"] = {"literal": typed}
        if kind == "navigate":
            step["url"] = s["target"]["url"]
        step["wait_after"] = {"condition": "load_settled", "timeout_ms": 8000}
        steps.append(step)

    outputs = []
    for out_name, value in trace.get("outputs", {}).items():
        # locate the observation where the value first appeared, derive its label
        label = None
        for s in trace["steps"]:
            if value in s.get("observation_summary", ""):
                label = label_for_value(s["observation_summary"], value)
                break
        outputs.append({"name": out_name, "type": infer_type(value),
                        "extract": {"strategy": "labeled_value",
                                    "label": label or out_name,
                                    "example": value}})

    checkpoint = {"after_step": steps[-1]["id"] if steps else None,
                  "assert": {"any_visible_text": [o["extract"]["label"] for o in outputs] or ["Member Detail"]},
                  "meaning": "we reached the state that exposes the declared outputs"}

    artifact = {
        "schema_version": SCHEMA_VERSION,
        "capability": {
            "id": name,
            "version": 1,
            "name": name.replace("_", " "),
            "description": description or trace["goal"],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "created_from": {"trace": str(trace_path), "driver": trace.get("driver"),
                             "discovery_goal": trace["goal"]},
            "review": {"state": "draft", "reviewer": None},   # draft -> approved gate
        },
        "surface": {"kind": "web", "entry_url": trace["start_url"]},
        "inputs": list(inputs_seen.values()),
        "outputs": outputs,
        "steps": steps,
        "checkpoint": checkpoint,
        "outcomes": outcomes,           # business / recoverable; hard failure is implicit
        "policy_ref": "agent/policy.yaml",
    }
    pathlib.Path(out_path).write_text(json.dumps(artifact, indent=2))
    print(f"compiled {len(steps)} steps, {len(inputs_seen)} inputs, {len(outputs)} outputs -> {out_path}")
    return artifact

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--description")
    a = ap.parse_args()
    compile_trace(a.trace, a.name, a.out, a.description)
