"""
Human-in-the-loop escalation & handoff (brief 3.6).

Control-transfer model (the design the brief asks us to reason about):
  Control is an explicit token held by exactly one party, moving through a
  state machine persisted next to the evidence:

      AUTOMATION -> PAUSED -> HUMAN -> RESUMING -> AUTOMATION
                                   \-> ABORTED

  - AUTOMATION: the replayer/agent acts; the human must not.
  - PAUSED:     automation has stopped mid-run; intervention request is being
                routed. Nobody acts.
  - HUMAN:      the operator owns the SAME live browser session (same page,
                same cookies, same app state — not a fresh one). Every click
                and input they make is captured into the handoff log.
  - RESUMING:   operator signalled done; automation verifies state before
                taking back over (outcome scan + checkpoint), then completes.

What is real here vs. mocked (per the brief's scope note):
  REAL:   pause/cede/resume on the same session; the intervention request with
          context (capability, step, reason, screenshot); capture of the
          human's actions via DOM event listeners; the control-state machine.
  MOCKED: the operator console is a minimal local web page (one screenshot,
          context fields, a Hand-back button) rather than a real-time
          co-browsing console — documented as an intentional cut.

The operator page runs on http://127.0.0.1:5099 while a handoff is active.
"""
import json, pathlib, threading, time
from flask import Flask, request

STATES = ("AUTOMATION", "PAUSED", "HUMAN", "RESUMING", "ABORTED")

class ControlState:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.set("AUTOMATION", "run started")

    def set(self, state, why):
        assert state in STATES
        self.state = state
        rec = {"state": state, "why": why, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        hist = []
        if self.path.exists():
            hist = json.loads(self.path.read_text()).get("history", [])
        hist.append(rec)
        self.path.write_text(json.dumps({"current": state, "history": hist}, indent=2))
        print(f"[control] -> {state} ({why})")

CAPTURE_JS = """
() => {
  if (window.__handoff_hooked) return; window.__handoff_hooked = true;
  const describe = (el) => ({
    tag: el.tagName, role: el.getAttribute('role') ||
      (el.tagName === 'A' ? 'link' : ['BUTTON'].includes(el.tagName) ||
       (el.tagName === 'INPUT' && ['submit','button'].includes(el.type)) ? 'button' :
       el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' ? 'textbox' : ''),
    text: (el.value || el.innerText || '').slice(0, 60)
  });
  document.addEventListener('click', e => window.__record_human_action(
      JSON.stringify({kind: 'click', target: describe(e.target)})), true);
  document.addEventListener('change', e => window.__record_human_action(
      JSON.stringify({kind: 'input', target: describe(e.target)})), true);
}
"""

OPERATOR_PAGE = """<html><head><title>Operator Console — Intervention</title><style>
 body {{ font-family: -apple-system, Helvetica, sans-serif; margin: 24px; background:#f6f7f9; }}
 .card {{ background:#fff; border:1px solid #ddd; border-radius:8px; padding:20px; max-width:760px; }}
 .why {{ background:#fff4e5; border-left:4px solid #e8a33d; padding:10px 14px; margin:12px 0; }}
 img {{ max-width:100%; border:1px solid #ccc; margin-top:12px; }}
 button {{ background:#1a7f37; color:#fff; border:0; border-radius:6px; padding:10px 22px;
          font-size:15px; cursor:pointer; }}
 code {{ background:#eee; padding:1px 5px; border-radius:4px; }}
</style></head><body><div class="card">
 <h2>Intervention required</h2>
 <p><b>Capability:</b> <code>{capability}</code> &nbsp; <b>Step:</b> <code>{step}</code></p>
 <div class="why"><b>Why automation stopped:</b> {reason}</div>
 <p>The live session is open in the automation browser window. <b>You have control.</b>
    Complete the task manually there, then click Hand back. Your actions are being recorded.</p>
 <form method="post" action="/handback"><button>Hand control back to automation</button></form>
 <p><b>State at escalation:</b></p><img src="/screenshot.png">
</div></body></html>"""

class Handoff:
    """Pause automation, expose the live session to a human, capture what they
    do, resume when they hand back. Returns the handoff record."""

    def __init__(self, page, outdir, context):
        self.page = page
        self.out = pathlib.Path(outdir)
        self.context = context
        self.human_actions = []
        self.done = threading.Event()

    def _record(self, source, payload):
        self.human_actions.append({**json.loads(payload),
                                   "at": time.strftime("%H:%M:%S")})

    def run(self, simulate_human=None):
        ctl = ControlState(self.out / "control_state.json")
        ctl.set("PAUSED", self.context["reason"])
        shot = self.out / "escalation.png"
        self.page.screenshot(path=str(shot))
        (self.out / "intervention_request.json").write_text(json.dumps(self.context, indent=2))

        # instrument the live page to capture human actions
        try:
            self.page.expose_binding("__record_human_action",
                                     lambda source, p: self._record(source, p))
        except Exception:
            pass   # already exposed on a previous handoff in this session
        for f in self.page.frames:
            try: f.evaluate(CAPTURE_JS)
            except Exception: pass

        # minimal operator surface
        app = Flask("operator")
        @app.route("/")
        def home():
            return OPERATOR_PAGE.format(**{k: self.context.get(k, "?")
                                           for k in ("capability", "step", "reason")})
        @app.route("/screenshot.png")
        def sshot():
            return shot.read_bytes(), 200, {"Content-Type": "image/png"}
        @app.route("/handback", methods=["POST"])
        def handback():
            self.done.set()
            return "<h3>Thanks — automation is resuming. You can close this tab.</h3>"
        server = threading.Thread(
            target=lambda: app.run(host="127.0.0.1", port=5099, use_reloader=False),
            daemon=True)
        server.start()

        ctl.set("HUMAN", "operator console at http://127.0.0.1:5099")
        print("\n*** HANDOFF: open http://127.0.0.1:5099 — complete the task in the "
              "automation browser window, then click Hand back. ***\n")

        if simulate_human:          # test harness only (documented)
            simulate_human(self.page)
            import urllib.request
            urllib.request.urlopen("http://127.0.0.1:5099/")  # prove console serves
            threading.Thread(target=lambda: (time.sleep(0.5),
                urllib.request.urlopen("http://127.0.0.1:5099/handback",
                                       data=b"")), daemon=True).start()

        # Wait by pumping the Playwright event loop (a plain threading wait would
        # block dispatch of the capture-binding callbacks in the sync API).
        deadline = time.time() + self.context.get("timeout_s", 600)
        while not self.done.is_set() and time.time() < deadline:
            try:
                self.page.wait_for_timeout(250)
            except Exception:
                break
        ctl.set("RESUMING", f"operator handed back; {len(self.human_actions)} human actions captured")
        record = {"human_actions": self.human_actions,
                  "handed_back": self.done.is_set(),
                  "operator_console": "http://127.0.0.1:5099"}
        (self.out / "handoff_record.json").write_text(json.dumps(record, indent=2))
        ctl.set("AUTOMATION", "verification and completion")
        return record
