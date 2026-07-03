import json, os, smtplib, ssl, urllib.parse, urllib.request
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from pathlib import Path

API_URL     = os.environ.get("API_URL", "").rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
GMAIL_USER  = os.environ.get("GMAIL_USER", "")
GMAIL_PASS  = os.environ.get("GMAIL_APP_PASS", "")

HERE = Path(__file__).parent
SUBS_LOCAL = HERE / "subscribers_local.json"
WA_KEYS    = HERE / "wa_keys.json"
SENT_LOG   = HERE / "alerts_sent.json"

def _norm_sub(u):
    return {"email": u.get("email"),
            "tier": u.get("tier"),
            "telegram": u.get("telegram"),
            "whatsapp": u.get("whatsapp")}

def sync_subscribers():
    """Gather subscribers from every place they might live and merge by email:
      1) the auth DB (magic-link signups),
      2) users.json (the website 'Turn on alerts' form),
      3) the legacy local copy / API.
    Whichever way someone signed up, they get found."""
    by_email = {}

    def _add(u):
        e = (u.get("email") or "").strip().lower()
        if not e:
            return
        cur = by_email.get(e, {})
        for k, v in _norm_sub(u).items():
            if v is not None:
                cur[k] = v
        cur.setdefault("email", u.get("email"))
        by_email[e] = cur

    # 1) auth DB
    try:
        import auth
        auth.init_db()
        for u in auth.all_subscribers():
            _add(u)
    except Exception:
        pass

    # 2) users.json written by the website form
    try:
        uf = HERE / "users.json"
        if uf.exists():
            data = json.loads(uf.read_text())
            if isinstance(data, list):
                for u in data:
                    _add(u)
    except Exception as e:
        print(f"  (could not read users.json: {e})")

    # 3) legacy local copy / remote API
    try:
        for u in _sync_subscribers_legacy():
            _add(u)
    except Exception:
        pass

    # keep only people who actually picked a tier
    return [u for u in by_email.values() if u.get("tier") is not None]

def _sync_subscribers_legacy():
    """Old path: pull from API into a local JSON copy."""
    local = json.loads(SUBS_LOCAL.read_text()) if SUBS_LOCAL.exists() else []
    if API_URL and ADMIN_TOKEN:
        try:
            url = f"{API_URL}/subscribers?token={urllib.parse.quote(ADMIN_TOKEN)}"
            with urllib.request.urlopen(url, timeout=15) as r:
                remote = json.load(r).get("subscribers", [])
            seen = {u["email"]: u for u in local}
            for u in remote:
                seen[u["email"]] = u
            local = list(seen.values())
            SUBS_LOCAL.write_text(json.dumps(local, indent=2))
            print(f"synced: {len(remote)} remote, {len(local)} total")
        except Exception as e:
            print(f"sync failed ({e}); using local copy of {len(local)}")
    return local

BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
BREVO_SENDER  = os.environ.get("BREVO_SENDER", GMAIL_USER or "no-reply@ipopredict.app")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_SENDER  = os.environ.get("RESEND_SENDER", BREVO_SENDER)

def _send_via_resend(to, subject, body):
    """Send over Resend's HTTPS API (port 443). Works on cloud hosts that block
    SMTP ports. Free tier, no card needed."""
    payload = json.dumps({
        "from": RESEND_SENDER, "to": [to], "subject": subject, "text": body,
    }).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=payload, method="POST",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status in (200, 201)

def _send_via_brevo(to, subject, body):
    """Send over Brevo's HTTPS API (port 443). Cloud hosts like Render block the
    SMTP ports Gmail uses, but allow HTTPS, so this is what works in production."""
    payload = json.dumps({
        "sender": {"email": BREVO_SENDER, "name": "IPOPredict"},
        "to": [{"email": to}],
        "subject": subject,
        "textContent": body,
    }).encode()
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email", data=payload, method="POST",
        headers={"api-key": BREVO_API_KEY, "content-type": "application/json",
                 "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status in (200, 201)

def send_email(to, subject, body):
    # Preferred: an HTTPS email API (works on Render, which blocks SMTP ports).
    if RESEND_API_KEY:
        try:
            if _send_via_resend(to, subject, body):
                return True
        except Exception as e:
            print(f"  resend send failed: {type(e).__name__}: {e}")
    if BREVO_API_KEY:
        try:
            if _send_via_brevo(to, subject, body):
                return True
        except Exception as e:
            print(f"  brevo send failed: {type(e).__name__}: {e}")
    # Fallback: Gmail SMTP (works locally; blocked on some cloud hosts).
    if not (GMAIL_USER and GMAIL_PASS):
        print("  email skipped (no email API key and no GMAIL creds)")
        return False
    msg = MIMEText(body)
    msg["Subject"], msg["From"], msg["To"] = subject, BREVO_SENDER, to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465,
                              context=ssl.create_default_context(), timeout=15) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.sendmail(GMAIL_USER, [to], msg.as_string())
        return True
    except Exception as e:
        print(f"  email send failed: {type(e).__name__}: {e}")
        return False

def send_whatsapp(phone, text):
    keys = json.loads(WA_KEYS.read_text()) if WA_KEYS.exists() else {}
    key = keys.get(phone)
    if not key:
        print(f"  whatsapp skipped for {phone} (no CallMeBot key yet)")
        return False
    url = ("https://api.callmebot.com/whatsapp.php?phone=" + urllib.parse.quote(phone)
           + "&text=" + urllib.parse.quote(text) + "&apikey=" + urllib.parse.quote(key))
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.status == 200

def send_telegram(chat_id, text):
    """Telegram Bot API: free, unlimited, no per-user activation hoops.
    Create a bot via @BotFather, set TELEGRAM_BOT_TOKEN. Each subscriber sends
    /start to the bot once; capture their chat_id. Better than CallMeBot for
    real users because there is no manual per-person key step."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not (token and chat_id):
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            return r.status == 200
    except Exception as e:
        print(f"  telegram failed for {chat_id}: {e}")
        return False

def send_sms(phone, text):
    """SMS via an HTTP provider (MSG91/Fast2SMS/Twilio). PAID in India, and
    transactional SMS needs DLT sender+template registration with TRAI.
    Left as a stub on purpose: wire it only for a paid tier. Set SMS_API_KEY,
    then fill the provider call below."""
    key = os.environ.get("SMS_API_KEY", "")
    if not (key and phone):
        return False
    print(f"  sms not configured (paid channel); skipped {phone}")
    return False

CHANNELS = [
    ("email",    lambda u, subj, body: send_email(u["email"], subj, body),          "GMAIL_USER"),
    ("telegram", lambda u, subj, body: send_telegram(u.get("telegram"), body),      "TELEGRAM_BOT_TOKEN"),
    ("whatsapp", lambda u, subj, body: send_whatsapp(u.get("whatsapp"), body) if u.get("whatsapp") else False, None),
    ("sms",      lambda u, subj, body: send_sms(u.get("sms"), body) if u.get("sms") else False, "SMS_API_KEY"),
]

def _plain_link(ipo):
    slug = ipo["ipo_name"].lower().replace(" ", "-")
    return f"{API_URL}/ipo/{slug}" if API_URL else None

def _subject(name, stage, verdict):
    """Verdict-led subject line so the call is clear before opening."""
    if verdict != "APPLY":
        return f"{name} closes today - our call: SKIP"
    if stage == "morning":
        return f"{name} closes today - APPLY (early read)"
    if stage == "midday":
        return f"{name} - APPLY (midday update)"
    if stage == "final":
        return f"{name} closes today - APPLY, act before 5 PM"
    return f"{name} - APPLY"

def compose(ipo, when="tomorrow", stage=None, email=None):
    """A clear, plain-language alert: the call, what it means in everyday terms,
    and exactly what to do. `stage` (morning / midday / final) tunes the timing."""
    p = ipo.get("prediction", {})
    inp = ipo.get("inputs", {})
    tiers = ipo.get("qualifying_tiers", [])
    top = max((t["tier"] for t in tiers), default=None)
    verdict = p.get("verdict", "APPLY")
    conf = p.get("confidence")
    name = ipo["ipo_name"]

    if stage == "morning":
        opener = (f"{name} closes today. Here's an early read - the subscription numbers are "
                  f"still coming in, so this can shift through the day.")
    elif stage == "midday":
        opener = f"{name} closes today. Midday update - demand has had time to build."
    elif stage == "final":
        opener = f"{name} closes today. This is the final read before applications close."
    else:
        opener = f"{name} closes {when}."

    L = [opener, ""]

    if verdict == "APPLY":
        L.append("OUR CALL: APPLY")
        say = []
        if conf is not None:
            sure = "very confident" if conf >= 85 else "fairly confident" if conf >= 70 else "leaning yes"
            say.append(f"We're {sure} in this call ({conf} out of 100).")
        if top is not None:
            say.append(f"IPOs our model rated this strongly have listed above their offer "
                       f"price about {top}% of the time.")
        if say:
            L.append(" ".join(say))
        if p.get("median") is not None:
            line = f"Likely outcome: a listing-day gain around {p['median']:+.0f}%."
            if p.get("range_low") is not None:
                line += (f" Cases like this have ranged from {p['range_low']:+.0f}% to "
                         f"{p['range_high']:+.0f}%, but {p['median']:+.0f}% is the middle of the pack.")
            L += ["", line]
        drivers = []
        if inp.get("gmp") is not None:
            drivers.append(f"Grey market premium: {inp['gmp']:+.0f}%. That's what buyers are "
                           f"unofficially paying over the offer price right now - a real demand signal.")
        if inp.get("sub") is not None:
            sub = inp["sub"]
            if sub >= 1:
                extra = []
                if inp.get("rii") is not None: extra.append(f"everyday retail {inp['rii']:.0f}x over")
                if inp.get("qib") is not None: extra.append(f"big institutions {inp['qib']:.1f}x")
                tail = (", with " + " and ".join(extra)) if extra else ""
                drivers.append(f"Demand: {sub:.0f}x oversubscribed overall{tail}.")
            else:
                drivers.append(f"Demand: only {sub:.1f}x subscribed so far.")
        if drivers:
            L += ["", "What's behind the call:"]
            L += ["- " + d for d in drivers]
        if stage == "morning":
            L += ["", "If you're leaning in you can apply now. We'll send a midday update and a "
                  "final read before the 5 PM cutoff."]
        elif stage == "midday":
            L += ["", "If you want in, apply through your broker and approve the UPI mandate before "
                  "5 PM. A final read comes around 3 PM."]
        else:
            L += ["", "To apply: place your application through your broker and approve the UPI "
                  "mandate before 5 PM today. Many banks stop accepting earlier (around 3:30-4 PM), "
                  "so don't leave it late."]
        if p.get("allotment_pct"):
            a = p["allotment_pct"]
            note = " - heavy demand makes allotment competitive" if a < 25 else ""
            L.append(f"Allotment odds: about {a:.0f}% per application{note}.")
    else:
        L.append("OUR CALL: SKIP")
        bar = f"the {top}% accuracy bar you set" if top else "the bar you set"
        L.append(f"This one cleared {bar}, so we're flagging it - but our overall call is SKIP, "
                 f"and here's the honest reason.")
        reasons = []
        gmp, sub = inp.get("gmp"), inp.get("sub")
        if gmp is not None and gmp < 7:
            reasons.append(f"the grey market premium is only {gmp:+.0f}% (we look for at least +7%)")
        elif gmp is not None:
            reasons.append(f"grey market premium {gmp:+.0f}%")
        if sub is not None and sub < 1:
            reasons.append(f"it's undersubscribed at {sub:.1f}x - fewer applications than shares on offer")
        why = "; ".join(reasons) if reasons else "the setup is weaker than our apply signal looks for"
        L += ["", f"Why we'd pass: {why}. That's a weak setup, so we wouldn't apply."]
        L += ["", "If you apply anyway, you're betting on the historical odds for that tier, not our "
              "recommendation."]

    link = _plain_link(ipo)
    L.append("")
    L.append(f"Full breakdown: {link}" if link else "Open the IPOPredict app for the full breakdown.")
    if verdict == "APPLY":
        L.append("Information only, not investment advice. Past accuracy doesn't guarantee future results.")
    else:
        L.append("Information only, not investment advice.")
    if email and API_URL:
        try:
            import auth
            tok = auth.unsub_token(email)
            L.append("")
            L.append(f"To stop these emails: {API_URL}/unsubscribe?e={urllib.parse.quote(email)}&t={tok}")
        except Exception:
            pass
    return "\n".join(L)

def _parse_close(s):
    if not s: return None
    s = str(s).strip()
    for fmt in ("%d %b, %Y", "%d %b %Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d/%m/%y", "%b %d, %Y"):
        try: return datetime.strptime(s, fmt).date()
        except ValueError: continue
    try:
        import pandas as pd
        return pd.to_datetime(s, dayfirst=True).date()
    except Exception:
        return None


def main():
    import sys, os
    # SEBI-safe build: never push APPLY / tier calls. The compliant site does not
    # offer forward-looking alerts, so the sender simply stands down.
    if os.environ.get("COMPLIANCE_MODE", "") == "1":
        print("COMPLIANCE_MODE is on: forward-looking IPO alerts are disabled. Nothing sent.")
        return
    args = sys.argv
    stage = None
    for s in ("morning", "midday", "final"):
        if f"--{s}" in args or f"--stage={s}" in args:
            stage = s
    if stage:
        when = "today"
    else:
        when = "today" if "--closing-today" in args else "tomorrow"
        if when == "today":
            stage = "final"   # a bare --closing-today behaves like the decision alert
    target = date.today() if when == "today" else date.today() + timedelta(days=1)

    subs = sync_subscribers()
    qualified = json.loads((HERE / "qualified_ipos.json").read_text())
    sent = json.loads(SENT_LOG.read_text()) if SENT_LOG.exists() else {}

    # An IPO is "due" if its close date is the target day. Also honour an explicit
    # _closing_today / _closing_tomorrow flag if a producer sets one.
    flag = "_closing_today" if when == "today" else "_closing_tomorrow"
    due = [i for i in qualified
           if _parse_close(i.get("close_date")) == target or i.get(flag)]
    label = stage if stage else when
    if not due:
        print(f"No IPO closing {when}; nothing to send ({label}).")
        return
    if not subs:
        print(f"{len(due)} IPO(s) closing {when}, but no subscribers yet.")
        return

    for ipo in due:
        name = ipo["ipo_name"]
        verdict = (ipo.get("prediction") or {}).get("verdict")
        if not verdict:                 # no call yet (upcoming) - nothing to alert
            continue
        top = max((t["tier"] for t in ipo.get("qualifying_tiers", [])), default=0)
        for u in subs:
            key = f"{name}|{label}|{u['email']}"     # dedup per stage, so all 3 can send once each
            if key in sent:
                continue
            if top >= u["tier"]:
                body = compose(ipo, when, stage, email=u['email'])
                subject = _subject(name, stage, verdict)
                results = {}
                for chan, sender, enabling_env in CHANNELS:
                    if enabling_env and not os.environ.get(enabling_env):
                        continue
                    try:
                        results[chan] = bool(sender(u, subject, body))
                    except Exception as e:
                        results[chan] = False
                        print(f"  {chan} error for {u['email']}: {e}")
                sent[key] = results
                summary = " ".join(f"{c}={v}" for c, v in results.items()) or "no channels active"
                print(f"[{label}] {name} -> {u['email']} (tier {u['tier']}): {summary}")
    SENT_LOG.write_text(json.dumps(sent, indent=2))


if __name__ == "__main__":
    main()
