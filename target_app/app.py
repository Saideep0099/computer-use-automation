"""
LegacyCU Teller Console — an intentionally hostile, legacy-style target app.

Design goals (why each "bad" thing is here ON PURPOSE):
  - Server-rendered, table-based layout, NO ids, NO test attributes, generic
    class names ("c1", "c2") -> CSS selectors are useless. Forces semantic /
    accessibility-based targeting.
  - An <iframe> wraps the work area -> automation must handle frame boundaries.
  - Ambiguous button labels ("Go", "OK") -> targeting needs context, not just name.
  - Deterministic error injection via query flags so every runtime-error class in
    the brief (3.3) can be demoed on command:
        ?interstitial=1   -> a session-notice dialog appears before the page
        ?slow=1           -> 4s artificial delay (transient slowness)
        ?crash=1          -> HTTP 500 (outright app error)
  - Seeded members exercise business outcomes:
        12345 -> valid member (happy path)
        99999 -> "No member found" (business outcome, not a crash)
        55555 -> permission denial (business outcome)
  - Two tenants of the SAME vendor product, configured differently:
        /?tenant=alpha  -> "Member Lookup", blue, member-number field only
        /?tenant=beta   -> "Client Search", green, different labels + extra
                           required "Branch Code" field
    This is the stand-in for "hundreds of institutions run the same product."

Run:  python app.py   (serves on http://127.0.0.1:5001)
"""
import time
from flask import Flask, request, make_response

app = Flask(__name__)

MEMBERS = {
    "12345": {"name": "Maria Alvarez", "savings": "$4,812.77", "checking": "$1,203.10", "status": "Active"},
    "23456": {"name": "Dwight Chen",   "savings": "$310.05",   "checking": "$88.20",    "status": "Active"},
    "55555": {"name": "RESTRICTED",    "savings": None,         "checking": None,        "status": "Restricted"},
}

TENANTS = {
    "alpha": {
        "org": "First Meridian CU", "color": "#1a3a6b", "search_label": "Member Number",
        "search_btn": "Go", "title": "Member Lookup", "needs_branch": False,
    },
    "beta": {
        "org": "Lakeview Federal CU", "color": "#1f6b3a", "search_label": "Client ID",
        "search_btn": "OK", "title": "Client Search", "needs_branch": True,
    },
}

def tenant():
    return TENANTS.get(request.args.get("tenant", "alpha"), TENANTS["alpha"])

def qs(extra=""):
    """Carry tenant + injection flags through links/forms (legacy apps love query strings)."""
    keep = []
    for k in ("tenant", "interstitial", "slow", "crash"):
        v = request.args.get(k)
        if v:
            keep.append(f"{k}={v}")
    if extra:
        keep.append(extra)
    return ("?" + "&".join(keep)) if keep else ""

def inject_faults():
    if request.args.get("crash") == "1":
        return make_response("<html><body><h1>HTTP 500 - Internal Server Error</h1>"
                             "<p>ORA-00600: internal error code</p></body></html>", 500)
    if request.args.get("slow") == "1":
        time.sleep(4)
    return None

def chrome(inner, t):
    """Outer page: banner + iframe wrapping the actual work area. No ids anywhere."""
    return f"""<html><head><title>{t['org']} - Teller Console v3.2.1</title>
<style>
 body {{ font-family: 'Times New Roman', serif; margin:0; background:#d4d0c8; }}
 .c1 {{ background:{t['color']}; color:white; padding:6px 10px; font-size:14px; }}
 table {{ border-collapse:collapse; }}
 td {{ font-size:13px; padding:3px 6px; }}
 input {{ font-size:13px; border:1px solid #7f9db9; }}
 .c3 {{ background:#ece9d8; border:1px outset #fff; padding:2px 12px; font-size:12px; }}
</style></head><body>
<table width="100%" class="c1"><tr>
  <td><b>{t['org']}</b> &nbsp;|&nbsp; TELLER CONSOLE</td>
  <td align="right">Operator: T-4471 &nbsp; Session: 08:{time.strftime('%M')}</td>
</tr></table>
<iframe src="/work{qs()}" width="100%" height="520" frameborder="0" name="workarea"></iframe>
<table width="100%"><tr><td align="center" style="color:#666;font-size:11px">
 (c) 2004-2026 CoreBanc Systems Inc. Unauthorized access prohibited.</td></tr></table>
</body></html>"""

INTERSTITIAL = """<html><body style="background:#d4d0c8;font-family:serif">
<table width="100%" height="400"><tr><td align="center">
<table style="background:#ece9d8;border:2px outset #fff" width="380"><tr><td align="center">
 <p><b>Session Notice</b></p>
 <p style="font-size:13px">Your session will expire in 5 minutes due to<br>scheduled maintenance. Save your work.</p>
 <form method="get" action="/work">
   {hidden}
   <input type="submit" class="c3" value="Acknowledge">
 </form>
</td></tr></table></td></tr></table></body></html>"""

def hidden_carry(drop_interstitial=True):
    fields = []
    for k in ("tenant", "slow", "crash"):
        v = request.args.get(k)
        if v:
            fields.append(f'<input type="hidden" name="{k}" value="{v}">')
    # interstitial acknowledged -> don't re-trigger
    return "\n".join(fields)

@app.route("/")
def home():
    fault = inject_faults()
    if fault: return fault
    return chrome("", tenant())

@app.route("/work")
def work():
    fault = inject_faults()
    if fault: return fault
    t = tenant()
    if request.args.get("interstitial") == "1" and not request.args.get("ack"):
        return INTERSTITIAL.format(hidden=hidden_carry() + '<input type="hidden" name="ack" value="1">'
                                   + f'<input type="hidden" name="interstitial" value="1">')
    branch = ""
    if t["needs_branch"]:
        branch = f"""<tr><td align="right">Branch Code:</td>
        <td><input type="text" name="branch" size="6"></td></tr>"""
    return f"""<html><head><style>
 body {{ font-family:'Times New Roman',serif; background:#fff; margin:8px; }}
 td {{ font-size:13px; padding:3px 6px; }}
 .c3 {{ background:#ece9d8; border:1px outset #fff; padding:2px 12px; font-size:12px; }}
</style></head><body>
<table><tr><td colspan="2"><b><u>{t['title']}</u></b></td></tr>
<form method="get" action="/result">
{hidden_carry()}<input type="hidden" name="ack" value="1">
<tr><td align="right">{t['search_label']}:</td>
    <td><input type="text" name="q" size="12"></td></tr>
{branch}
<tr><td></td><td><input type="submit" class="c3" value="{t['search_btn']}"></td></tr>
</form>
<tr><td colspan="2" style="font-size:11px;color:#666">Enter exact {t['search_label'].lower()} and press {t['search_btn']}.</td></tr>
</table></body></html>"""

@app.route("/result")
def result():
    fault = inject_faults()
    if fault: return fault
    t = tenant()
    q = (request.args.get("q") or "").strip()
    if t["needs_branch"] and not (request.args.get("branch") or "").strip():
        return page_msg(t, "Validation Error", "Branch Code is required.", back=True)
    if q not in MEMBERS:
        return page_msg(t, "Search Result", f"No member found for {t['search_label'].lower()} '{q}'.", back=True)
    m = MEMBERS[q]
    if m["status"] == "Restricted":
        return page_msg(t, "Access Denied", "Operator T-4471 lacks permission to view this record. Contact a supervisor.", back=True)
    return f"""<html><head><style>
 body {{ font-family:'Times New Roman',serif; background:#fff; margin:8px; }}
 td {{ font-size:13px; padding:3px 8px; border:1px solid #999; }}
 .h td {{ background:#ece9d8; font-weight:bold; border:none; }}
</style></head><body>
<table><tr class="h"><td colspan="2">Member Detail - {q}</td></tr>
<tr><td>Name</td><td>{m['name']}</td></tr>
<tr><td>Status</td><td>{m['status']}</td></tr>
<tr><td>Savings Balance</td><td>{m['savings']}</td></tr>
<tr><td>Checking Balance</td><td>{m['checking']}</td></tr>
</table>
<p><a href="/work{qs()}">&laquo; New search</a></p>
</body></html>"""

def page_msg(t, title, msg, back=False):
    link = f'<p><a href="/work{qs()}">&laquo; Back to search</a></p>' if back else ""
    return f"""<html><body style="font-family:serif;margin:8px">
<table><tr><td style="font-size:13px"><b><u>{title}</u></b></td></tr>
<tr><td style="font-size:13px">{msg}</td></tr></table>{link}</body></html>"""

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001)
