"""
Discovery agent: LLM-driven observe -> decide -> act loop.

Design decisions (defend-in-interview notes):
  - OBSERVATION is semantic, not DOM-structural. We walk every frame and extract
    interactive elements as (role, accessible name, context), plus visible page
    text. We deliberately DO NOT expose ids/classes/xpath to the model, because
    the target class of apps (legacy consoles) doesn't have stable ones. This is
    the "works when there is no clean DOM" bias the brief asks for, and it means
    the recorded trace is already in surface-agnostic vocabulary.
  - ONE ACTION PER MODEL TURN, returned as strict JSON. Small action space:
    click / type / navigate / extract / done / stuck. Easy to validate, easy to
    replay, easy to audit.
  - EVERY action is checked against the policy allowlist BEFORE execution
    (guardrails are in the loop from day one, not bolted on).
  - EVERYTHING is recorded: per-step observation summary, model reasoning,
    action, resolved element descriptor, screenshot. The trace is raw evidence;
    a separate compiler turns it into the reusable capability artifact
    (decoupling required by the brief, section 3.2).
  - --driver scripted lets us test the full plumbing without an API key; the
    submission's real run uses --driver llm (Anthropic).

Usage:
  python discover.py --goal "Look up member 12345 and read the savings balance" \
      --url http://127.0.0.1:5001 --params member_id=12345 --out ../evidence/run1
"""
import argparse, json, os, re, sys, time, pathlib
import yaml
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- observation

JS_COLLECT = """
() => {
  const out = [];
  const push = (el, role, name) => {
    name = (name || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;           // invisible
    out.push({ role, name, x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2) });
  };
  const labelFor = (el) => {
    if (el.labels && el.labels.length) return el.labels[0].innerText;
    // legacy apps: label lives in the previous table cell
    const cell = el.closest('td');
    if (cell && cell.previousElementSibling) return cell.previousElementSibling.innerText;
    return el.getAttribute('aria-label') || el.name || el.placeholder || '';
  };
  document.querySelectorAll('a[href]').forEach(el => push(el, 'link', el.innerText));
  document.querySelectorAll('button, input[type=submit], input[type=button]').forEach(
    el => push(el, 'button', el.value || el.innerText));
  document.querySelectorAll('input[type=text], input:not([type]), textarea').forEach(
    el => push(el, 'textbox', labelFor(el)));
  document.querySelectorAll('select').forEach(el => push(el, 'combobox', labelFor(el)));
  return out;
}
"""

def observe(page):
    """Return {'text': visible text, 'elements': [{id, frame, role, name, x, y}]} across all frames."""
    elements, texts = [], []
    for fi, frame in enumerate(page.frames):
        try:
            texts.append(frame.evaluate("() => document.body ? document.body.innerText : ''"))
            for e in frame.evaluate(JS_COLLECT):
                e["frame"] = fi
                e["id"] = len(elements)
                elements.append(e)
        except Exception:
            continue
    text = "\n".join(t for t in texts if t).strip()
    return {"text": text[:4000], "elements": elements}

def render_observation(obs):
    lines = ["VISIBLE TEXT:", obs["text"][:1500], "", "INTERACTIVE ELEMENTS:"]
    for e in obs["elements"]:
        lines.append(f'  [{e["id"]}] {e["role"]} "{e["name"]}"')
    return "\n".join(lines)

# ------------------------------------------------------------------ guardrails

class Policy:
    def __init__(self, path):
        cfg = yaml.safe_load(open(path)) if os.path.exists(path) else {}
        self.allowed_url_patterns = cfg.get("allowed_url_patterns", [])
        self.allowed_actions = set(cfg.get("allowed_actions",
                                   ["click", "type", "navigate", "extract", "done", "stuck"]))
        self.redact_patterns = [re.compile(p) for p in cfg.get("redact_patterns", [])]

    def check_url(self, url):
        return any(re.match(p, url) for p in self.allowed_url_patterns)

    def check_action(self, action):
        if action.get("action") not in self.allowed_actions:
            return f'action type "{action.get("action")}" not in allowlist'
        if action.get("action") == "navigate" and not self.check_url(action.get("url", "")):
            return f'navigation to "{action.get("url")}" outside allowlist'
        return None

    def redact(self, s):
        for p in self.redact_patterns:
            s = p.sub("[REDACTED]", s)
        return s

# ------------------------------------------------------------------- drivers

SYSTEM_PROMPT = """You are a computer-use agent operating a legacy web application for a credit union.
You are given a GOAL, the current page's visible text, and a numbered list of interactive elements.
Respond with EXACTLY ONE action as a single JSON object, nothing else. Actions:
  {"action":"click","element_id":N,"reason":"..."}
  {"action":"type","element_id":N,"text":"...","reason":"..."}
  {"action":"navigate","url":"...","reason":"..."}
  {"action":"extract","name":"<output_name>","value":"<value you read from the page>","reason":"..."}
  {"action":"done","reason":"goal achieved"}
  {"action":"stuck","reason":"why you cannot proceed safely"}
Rules:
- Interact only with listed element ids.
- If the goal asks to read/report a value, use "extract" with the exact value from the visible text before "done".
- If you see an unexpected dialog or notice, handle it sensibly (e.g. acknowledge it) and continue.
- If the application reports a business result like "no member found" or "access denied", extract it and then "done" — that IS a valid outcome.
- Never invent data. Never navigate to external sites."""

class LLMDriver:
    def __init__(self, model="claude-sonnet-4-6"):
        import anthropic
        self.client = anthropic.Anthropic()   # needs ANTHROPIC_API_KEY
        self.model = model
        self.history = []

    def decide(self, goal, obs_text):
        self.history.append({"role": "user", "content":
            f"GOAL: {goal}\n\n{obs_text}\n\nRespond with one JSON action."})
        resp = self.client.messages.create(
            model=self.model, max_tokens=400, system=SYSTEM_PROMPT,
            messages=self.history[-12:])   # sliding window
        raw = resp.content[0].text.strip()
        self.history.append({"role": "assistant", "content": raw})
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group(0))

class ScriptedDriver:
    """Deterministic stand-in for the model, used to test plumbing end-to-end
    without an API key. Solves the member-lookup goal on tenant alpha."""
    def __init__(self):
        self.stage = 0

    def decide(self, goal, obs_text):
        member = re.search(r"member (\d+)", goal)
        member = member.group(1) if member else "12345"
        if "Session Notice" in obs_text:
            bid = self._find(obs_text, "button", "Acknowledge")
            return {"action": "click", "element_id": bid, "reason": "dismiss interstitial"}
        if "Savings Balance" in obs_text:
            val = re.search(r"Savings Balance\s*\n?\s*(\$[\d,.]+)", obs_text)
            if self.stage < 3 and val:
                self.stage = 3
                return {"action": "extract", "name": "savings_balance",
                        "value": val.group(1), "reason": "read balance from detail table"}
            return {"action": "done", "reason": "balance extracted"}
        if self.stage == 0:
            tid = self._find(obs_text, "textbox", "Member Number")
            self.stage = 1
            return {"action": "type", "element_id": tid, "text": member, "reason": "enter member number"}
        if self.stage == 1:
            bid = self._find(obs_text, "button", "Go")
            self.stage = 2
            return {"action": "click", "element_id": bid, "reason": "submit search"}
        return {"action": "stuck", "reason": "scripted driver has no rule for this state"}

    @staticmethod
    def _find(obs_text, role, name):
        m = re.search(rf"\[(\d+)\] {role} \"{name}", obs_text)
        if not m:
            raise RuntimeError(f"scripted driver could not find {role} '{name}'")
        return int(m.group(1))

# ---------------------------------------------------------------- the loop

def run(goal, url, params, outdir, driver_kind, max_steps=15, headed=False):
    out = pathlib.Path(outdir); out.mkdir(parents=True, exist_ok=True)
    policy = Policy(os.path.join(os.path.dirname(__file__), "policy.yaml"))
    if not policy.check_url(url):
        sys.exit(f"POLICY: start url {url} not in allowlist")
    driver = LLMDriver() if driver_kind == "llm" else ScriptedDriver()
    trace = {"schema": "trace/v1", "goal": goal, "start_url": url, "params": params,
             "driver": driver_kind, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "steps": [], "status": None, "outputs": {}}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(url)
        for step_no in range(1, max_steps + 1):
            page.wait_for_load_state("networkidle")
            obs = observe(page)
            # If a frame navigated mid-observation, the JS context dies and we get
            # a partial/empty snapshot -> settle and retry (bounded).
            for _ in range(3):
                if obs["elements"]:
                    break
                page.wait_for_timeout(500)
                obs = observe(page)
            obs_text = policy.redact(render_observation(obs))
            shot = out / f"step{step_no:02d}.png"
            page.screenshot(path=str(shot))
            try:
                action = driver.decide(goal, obs_text)
            except Exception as e:
                trace["status"] = "driver_error"; trace["error"] = str(e)[:300]; break
            veto = policy.check_action(action)
            step = {"n": step_no, "observation_summary": obs_text[:800],
                    "action": action, "screenshot": shot.name, "policy_veto": veto}
            print(f"[{step_no}] {action.get('action')}: {action.get('reason','')}"
                  + (f"  VETOED: {veto}" if veto else ""))
            if veto:
                trace["steps"].append(step); trace["status"] = "policy_blocked"; break
            kind = action["action"]
            if kind in ("done", "stuck"):
                trace["steps"].append(step)
                trace["status"] = "success" if kind == "done" else "stuck"
                break
            if kind == "extract":
                trace["outputs"][action["name"]] = action["value"]
                trace["steps"].append(step); continue
            el = obs["elements"][action["element_id"]] if "element_id" in action else None
            if el:
                # Record the SEMANTIC descriptor — this is exactly what replay will use.
                step["target"] = {"role": el["role"], "name": el["name"], "frame_index": el["frame"]}
                frame = page.frames[el["frame"]]
                loc = frame.get_by_role(el["role"], name=el["name"], exact=False) \
                      if el["name"] else frame.get_by_role(el["role"])
                if loc.count() == 0:   # a11y name mismatch -> positional fallback
                    loc = frame.get_by_role(el["role"])
                if kind == "click":
                    loc.first.click()
                elif kind == "type":
                    loc.first.fill(action.get("text", ""))
                    step["typed"] = policy.redact(action.get("text", ""))
            elif kind == "navigate":
                page.goto(action["url"])
                step["target"] = {"url": action["url"]}
            trace["steps"].append(step)
            page.wait_for_timeout(300)   # let any triggered navigation begin
        else:
            trace["status"] = "max_steps"
        page.screenshot(path=str(out / "final.png"))
        browser.close()

    trace["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    (out / "trace.json").write_text(json.dumps(trace, indent=2))
    print(f"\nstatus={trace['status']} outputs={trace['outputs']}")
    print(f"trace -> {out/'trace.json'}")
    return trace

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--goal", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--params", nargs="*", default=[], help="k=v pairs, e.g. member_id=12345")
    ap.add_argument("--out", default="../evidence/run")
    ap.add_argument("--driver", choices=["llm", "scripted"], default="llm")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--max-steps", type=int, default=15)
    a = ap.parse_args()
    params = dict(kv.split("=", 1) for kv in a.params)
    run(a.goal, a.url, params, a.out, a.driver, a.max_steps, a.headed)
