"""External uptime monitor for LedgerSpeak / ASG (H-13 step 3). Runs in GitHub Actions every 5 min.
Stdlib only. State in state/state.json (committed back by the workflow only when it changes).
Alert rule: a check must fail 2 runs in a row -> DOWN alert (Telegram + email), repeat every 6 h while
down, one RECOVERED message when it passes again. No secrets or response bodies are logged."""
import json, os, sys, time, urllib.request, urllib.error
from datetime import datetime

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "state.json")
FAILS_BEFORE_ALERT = 2
REALERT_SECS = 6 * 3600
PROBE_STALE_SECS = 20 * 60   # /api/cron/health stamps lastProbeAt every 5 min
UA = "amagi-uptime/1.0 (+github actions)"


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(200_000), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read(20_000), time.time() - t0
    except Exception as e:
        return 0, type(e).__name__.encode(), time.time() - t0


def site(url):
    def check():
        s, _, dt = fetch(url)
        return s == 200, f"HTTP {s} in {dt:.1f}s"
    return check


_health = {}


def health_json():
    if "v" not in _health:
        s, body, dt = fetch("https://app.amagisolutionsgroup.com/api/health")
        try:
            j = json.loads(body)
        except Exception:
            j = None
        _health["v"] = (s, j, dt)
    return _health["v"]


def app_health():
    s, j, dt = health_json()
    if s != 200 or not isinstance(j, dict):
        return False, f"HTTP {s} in {dt:.1f}s"
    c = j.get("components", {})
    ok = c.get("app") == "ok" and c.get("database") == "ok"
    return ok, (f"HTTP 200 in {dt:.1f}s, status={j.get('status')}, app={c.get('app')}, "
                f"database={c.get('database')}, billingWebhook={c.get('billingWebhook')}")


def cron_heartbeat():
    """intuit-meter-check heartbeat. /api/health 'scheduledJobs' turns 'degraded' when any cron heartbeat
    is late (intuit-meter-check is stale after 2h15m; sms-queue and retention are covered too), and
    lastProbeAt is stamped by the 5-min /api/cron/health job."""
    s, j, _ = health_json()
    if s != 200 or not isinstance(j, dict):
        return False, f"health HTTP {s}"
    sj = j.get("components", {}).get("scheduledJobs")
    lp = j.get("lastProbeAt")
    age = None
    if lp:
        try:
            age = time.time() - datetime.fromisoformat(lp.replace("Z", "+00:00")).timestamp()
        except Exception:
            age = None
    ok = sj == "ok" and age is not None and age < PROBE_STALE_SECS
    return ok, f"scheduledJobs={sj}, lastProbeAt age={'?' if age is None else int(age)}s"


CHECKS = {
    "app_health": ("LedgerSpeak app /api/health", app_health),
    "cron_heartbeat": ("LedgerSpeak cron heartbeat (intuit-meter-check + scheduled jobs)", cron_heartbeat),
    "getledgerspeak": ("getledgerspeak.com", site("https://getledgerspeak.com/")),
    "amagi_site": ("amagisolutionsgroup.com", site("https://amagisolutionsgroup.com/")),
}


def telegram(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return "telegram:not-configured"
    data = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return f"telegram:{r.status}"
    except Exception as e:
        return f"telegram:FAIL {getattr(e, 'code', type(e).__name__)}"


def email(subject, text):
    key, to = os.environ.get("RESEND_API_KEY"), os.environ.get("ALERT_EMAIL_TO")
    frm = os.environ.get("ALERT_EMAIL_FROM") or "LedgerSpeak Uptime <alerts@mail.getledgerspeak.com>"
    if not key or not to:
        return "email:not-configured"
    data = json.dumps({"from": frm, "to": [a.strip() for a in to.split(",") if a.strip()],
                       "subject": subject, "text": text}).encode()
    req = urllib.request.Request("https://api.resend.com/emails", data=data,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return f"email:{r.status}"
    except Exception as e:
        return f"email:FAIL {getattr(e, 'code', type(e).__name__)}"


def notify(subject, text):
    run = (f"{os.environ.get('GITHUB_SERVER_URL', '')}/{os.environ.get('GITHUB_REPOSITORY', '')}"
           f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")
    body = f"{text}\n\nExternal monitor (GitHub Actions): {run}"
    res = [telegram(f"{subject}\n{body}"), email(subject, body)]
    print("notify:", subject, "->", ", ".join(res))
    return any(r.split(":", 1)[1].startswith("2") for r in res)   # delivered on at least one channel


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    now = time.time()
    if os.environ.get("TEST_ALERT") == "true":
        ok = notify("[TEST] LedgerSpeak external uptime monitor",
                    "Test alert from the new off-PC monitor. Checks: "
                    + ", ".join(v[0] for v in CHECKS.values()) + ". No action needed.")
        sys.exit(0 if ok else 1)
    st = load()
    changed = False
    for key, (label, fn) in CHECKS.items():
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"check error {type(e).__name__}"
        print(f"{'OK  ' if ok else 'FAIL'} {label}: {detail}")
        s = st.get(key, {"fails": 0, "down_alerted_at": None})
        before = json.dumps(s, sort_keys=True)
        if ok:
            if s.get("down_alerted_at"):
                notify(f"RECOVERED: {label}", f"{label} is passing again: {detail}")
            s = {"fails": 0, "down_alerted_at": None}
        else:
            s["fails"] = s.get("fails", 0) + 1
            last = s.get("down_alerted_at")
            if s["fails"] >= FAILS_BEFORE_ALERT and (not last or now - last >= REALERT_SECS):
                if notify(f"DOWN: {label}", f"{label} failed {s['fails']} checks in a row: {detail}"):
                    s["down_alerted_at"] = now
        st[key] = s
        changed |= json.dumps(s, sort_keys=True) != before
    # Weekly save even with no change keeps the public repo active (GitHub disables crons after 60 idle days).
    if changed or now - st.get("_saved_at", 0) > 7 * 86400:
        st["_saved_at"] = now
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            json.dump(st, f, indent=1, sort_keys=True)
        print("state saved")


if __name__ == "__main__":
    main()
