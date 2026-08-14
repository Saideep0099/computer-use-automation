"""Test the escalation/handoff flow end-to-end with a simulated operator.
Scenario: replay the alpha-recorded artifact on tenant beta WITHOUT the
override -> targets unresolvable -> hard failure -> handoff. The 'human'
completes the search manually IN THE SAME SESSION, hands back, automation
re-verifies state, extracts outputs, and reports a human-assisted success.

(For the recorded evidence in the submission, Sai runs this headed and plays
the human himself with real mouse clicks — this harness exists so the seam is
testable in CI without a person.)"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from replay import replay

def simulated_operator(page):
    """What a human teller would do in the live window."""
    work = page.frames[1]
    work.get_by_role("textbox").nth(0).fill("12345")   # Client ID
    work.get_by_role("textbox").nth(1).fill("07")      # Branch Code
    work.get_by_role("button", name="OK").click()
    page.wait_for_timeout(600)

r = replay(
    artifact_path="../artifacts/member_savings_lookup.capability.json",
    params={"member_id": "12345"},
    outdir="../evidence/replay-escalation",
    entry_url="http://127.0.0.1:5001/?tenant=beta",   # beta, NO override -> stuck
    escalate=True,
    _simulate_human=simulated_operator,
)
assert r["status"] == "success", r
assert r.get("human_assisted") is True
assert r["escalation"]["human_actions_captured"] >= 2
print("\nHANDOFF TEST PASSED:", r["outputs"], "| human actions:",
      r["escalation"]["human_actions_captured"])
