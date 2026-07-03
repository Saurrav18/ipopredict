import json, os
from pathlib import Path
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Query, Request, Response, Cookie
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import auth, hmac
auth.init_db()

DATA_DIR = Path(os.environ.get("IPO_DATA_DIR", "."))
APP_URL  = os.environ.get("APP_URL", "")
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "") == "1"
# Only expose verification codes in API responses when explicitly in dev.
# Never tie this to SECURE_COOKIES: a misconfigured prod must NOT leak codes.
DEV_MODE = os.environ.get("DEV_MODE", "") == "1"
# SEBI-safe public build switch (mirrors publish.py). When on, the app exposes
# only factual IPO data + education, never forward-looking calls, and the alert
# system becomes neutral factual reminders rather than APPLY/tier notifications.
COMPLIANCE_MODE = os.environ.get("COMPLIANCE_MODE", "") == "1"
QUALIFIED_FILE   = DATA_DIR / "qualified_ipos.json"
CALIBRATION_FILE = DATA_DIR / "alert_calibration.json"
USERS_FILE       = DATA_DIR / "users.json"

app = FastAPI(
    title="IPO Alert API",
    description="Real-time IPO recommendations filtered by user confidence tier",
    version="1.0.0",
)

# CORS: cookies (auth) are same-origin, so we don't need wildcard with credentials.
# In prod set APP_URL; locally allow localhost. Never use "*" together with credentials.
_origins = [APP_URL] if APP_URL else ["http://127.0.0.1:8000", "http://localhost:8000"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Baseline hardening headers on every response: block framing (clickjacking),
    stop MIME sniffing, trim referrer leakage, and apply a conservative CSP.
    HSTS is only sent when we know we're on HTTPS (SECURE_COOKIES)."""
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
        "img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; form-action 'self'")
    if SECURE_COOKIES:
        resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return resp


# Lightweight in-memory rate limiter (per-IP). Enough for a single instance;
# swap for Redis if you scale horizontally.
import time as _time
from collections import defaultdict as _dd
_hits = _dd(list)
def _rate_ok(key: str, limit: int, window: int) -> bool:
    now = _time.time()
    q = _hits[key]
    while q and q[0] < now - window:
        q.pop(0)
    if len(q) >= limit:
        return False
    q.append(now)
    # opportunistic cleanup so the dict cannot grow unbounded by unique IPs
    if len(_hits) > 5000:
        for k in [k for k, v in _hits.items() if not v or v[-1] < now - window]:
            _hits.pop(k, None)
    return True

def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown"))

def load_json(path: Path, default=None):
    """Safely load JSON, return default if missing/corrupt."""
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return default if default is not None else {}

def save_json(path: Path, data):
    """Atomic write to prevent corruption during concurrent reads."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    tmp.replace(path)

@app.get("/")
def home():
    """Serve the frontend; plain health message if the file is missing."""
    page = DATA_DIR / "index.html"
    if page.exists():
        return FileResponse(page, media_type="text/html")
    return {"status": "ok", "note": "frontend file not found, API is up"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/config")
def config():
    """Frontend reads this to know which build it is talking to."""
    return {"compliance_mode": COMPLIANCE_MODE}

@app.get("/qualified_ipos.json")
def qualified_json():
    """Serve the live-IPO file the frontend reads to show open/upcoming IPOs."""
    if QUALIFIED_FILE.exists():
        return FileResponse(QUALIFIED_FILE, media_type="application/json")
    return JSONResponse([])

@app.get("/last_updated")
def last_updated():
    """When the scraper last fetched fresh IPO data. The scraper writes its
    Excel output (SCRAPER_XLSX) on every run, so that file's modification time
    is the true "data as of" moment. Falls back to qualified_ipos.json only if
    the scraper file is not present (e.g. before the first scrape)."""
    try:
        scraper_path = Path(os.environ.get("SCRAPER_XLSX", "active_ipos_v13.xlsx"))
        src = scraper_path if scraper_path.exists() else QUALIFIED_FILE
        if not src.exists():
            return {"generated_at": None}
        dt = datetime.fromtimestamp(src.stat().st_mtime)
        mins = int((datetime.now() - dt).total_seconds() // 60)
        if mins < 1:
            ago = "just now"
        elif mins < 60:
            ago = f"{mins} min ago"
        elif mins < 1440:
            ago = f"{mins // 60} hr {mins % 60} min ago"
        else:
            ago = f"{mins // 1440} day(s) ago"
        ipos = load_json(QUALIFIED_FILE, [])
        n_open = sum(1 for e in ipos if isinstance(e, dict) and e.get("state") == "open")
        return {"generated_at": dt.strftime("%d %b %Y, %I:%M %p"), "ago": ago, "open": n_open,
                "source": "scraper" if src is scraper_path else "publish"}
    except Exception:
        return {"generated_at": None}

@app.get("/ipos")
def get_ipos(tier: int = Query(90, ge=65, le=98)):
    """IPOs that qualify at the requested accuracy tier.
    In compliance mode there are no forward tiers, so return the factual list as-is."""
    qualified = load_json(QUALIFIED_FILE, [])
    if COMPLIANCE_MODE:
        return {"compliance_mode": True, "total": len(qualified), "ipos": qualified}
    visible = [i for i in qualified
               if any(t.get("tier", 0) >= tier for t in i.get("qualifying_tiers", []))]
    return {"user_tier": tier, "total": len(visible), "ipos": visible}

@app.get("/ipo/{ipo_name}")
def get_ipo_details(ipo_name: str):
    """Full record for one IPO, plus its card file when present."""
    qualified = load_json(QUALIFIED_FILE, [])
    match = next((i for i in qualified
                  if ipo_name.lower() in i.get("ipo_name", "").lower()), None)
    if not match:
        raise HTTPException(404, f"no IPO matching '{ipo_name}'")
    slug = match["ipo_name"].lower().replace(" ", "_")
    card = load_json(DATA_DIR / f"card_{slug}.json", None)
    return {"ipo": match, "card": card}

@app.get("/calibration")
def get_calibration():
    return load_json(CALIBRATION_FILE, {})

@app.get("/tiers")
def list_tiers():
    """Flat tier list for the slider."""
    calib = load_json(CALIBRATION_FILE, {}).get("slider_levels", {})
    tiers = []
    for k in sorted(calib, key=int):
        c = calib[k]
        if not c:
            continue
        tiers.append({"tier": int(k), "model": c["model"], "threshold": c["threshold"],
                      "avg_wr": c["avg_wr"], "min_wr": c["min_wr"],
                      "picks": c.get("avg_picks"),
                      "gmp_min": c.get("gmp_min", 0), "qib_min": c.get("qib_min", 0)})
    return {"tiers": tiers}

class SubscribeRequest(BaseModel):
    email: str
    whatsapp: Optional[str] = None
    telegram: Optional[str] = None
    tier: int

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

@app.post("/subscribe")
def subscribe(req: SubscribeRequest, request: Request):
    if not _rate_ok("sub:" + _client_ip(request), limit=10, window=3600):
        raise HTTPException(429, "Too many sign-ups. Please wait a bit.")
    """Save an alert subscription: tier threshold + email (+ optional WhatsApp)."""
    email = (req.email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1] or len(email) > 120:
        raise HTTPException(400, "valid email required")
    if not (65 <= req.tier <= 98):
        raise HTTPException(400, "tier must be between 65 and 98")
    wa = (req.whatsapp or "").strip().replace(" ", "")
    if wa and not (wa.lstrip("+").isdigit() and 10 <= len(wa.lstrip("+")) <= 15):
        raise HTTPException(400, "whatsapp must be a phone number with country code")

    users = load_json(USERS_FILE, default=[])
    if not isinstance(users, list):
        users = []
    users = [u for u in users if u.get("email") and u.get("email") != email]
    users.append({"email": email, "whatsapp": wa or None,
                  "telegram": (req.telegram or "").strip() or None,
                  "tier": req.tier,
                  "joined": __import__("datetime").datetime.now().isoformat()[:19]})
    save_json(USERS_FILE, users)
    return {"ok": True, "tier": req.tier,
            "note": "You will get one alert on closing day when an IPO qualifies at your tier."}

@app.get("/subscribers")
def subscribers(token: str = Query("")):
    """Owner-only: the alert pipeline on the laptop pulls this before sending."""
    if not ADMIN_TOKEN or not hmac.compare_digest(token or "", ADMIN_TOKEN):
        raise HTTPException(403, "forbidden")
    return {"subscribers": load_json(USERS_FILE, default=[])}

class SignupRequest(BaseModel):
    email: str
    password: str
    consent: bool = False   # user ticked "I agree to Terms, Privacy, and that this is not advice"

class LoginRequest(BaseModel):
    email: str
    password: str

class OtpRequest(BaseModel):
    email: str
    code: str

class EmailOnly(BaseModel):
    email: str

def _valid_email(email: str) -> bool:
    return "@" in email and "." in email.split("@")[-1] and len(email) <= 120

def _email_code(email: str, code: str) -> bool:
    """Email a verification code with a short welcome + how-it-works. Returns True if sent."""
    try:
        import send_alerts
        base = (APP_URL or "http://127.0.0.1:8000").rstrip("/")
        body = (
            f"Welcome to IPOPredict.\n\n"
            f"Your verification code is: {code}\n"
            f"It expires in 10 minutes. If you did not request this, ignore this email.\n\n"
            f"------------------------------------------------------------\n"
            f"What IPOPredict does\n"
            f"------------------------------------------------------------\n"
            f"For each upcoming Indian mainboard IPO, our models give a clear\n"
            f"call: APPLY or SKIP, with a confidence score and a likely\n"
            f"listing-day range. One important thing: we predict the\n"
            f"LISTING-DAY move only (how it opens versus the offer price),\n"
            f"not long-term returns.\n\n"
            f"How your accuracy tier (the slider) works\n"
            f"------------------------------------------------------------\n"
            f"You pick a tier from 65 up to 98. It is a historical hit rate:\n"
            f"a higher tier means fewer, safer alerts; a lower tier means\n"
            f"more alerts but more misses. You only get an email when an IPO\n"
            f"clears the tier you chose, so you set it once and we respect it.\n\n"
            f"------------------------------------------------------------\n"
            f"IPOPredict is educational information, NOT investment advice, and\n"
            f"is not a SEBI-registered Research Analyst or Investment Adviser.\n"
            f"Past accuracy never guarantees a future result. Investing carries\n"
            f"risk and the decision is always yours.\n\n"
            f"Terms:      {base}/terms\n"
            f"Privacy:    {base}/privacy\n"
            f"Disclaimer: {base}/disclaimer\n"
        )
        return send_alerts.send_email(
            email, "Welcome to IPOPredict - your verification code", body)
    except Exception:
        return False

@app.post("/auth/signup")
def auth_signup(body: SignupRequest, request: Request):
    """Create an account and email a 6-digit verification code."""
    if not _rate_ok("auth:" + _client_ip(request), limit=8, window=600):
        raise HTTPException(429, "Too many attempts. Please wait a few minutes.")
    email = (body.email or "").strip().lower()
    if not _valid_email(email):
        raise HTTPException(400, "Enter a valid email address.")
    if len(body.password or "") < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    if not body.consent:
        raise HTTPException(400, "Please accept the Terms, Privacy Policy, and the not-investment-advice notice to continue.")
    status = auth.create_account(email, body.password)
    if status == "exists":
        raise HTTPException(409, "An account with this email already exists. Please sign in.")
    auth.record_consent(email)   # timestamped proof the person accepted the terms
    # 'created' or 'exists_unverified' -> (re)send a code so they can finish verifying
    if not auth.otp_send_allowed(email):
        raise HTTPException(429, "A code was just sent. Please wait a minute before asking for another.")
    code = auth.set_otp(email)
    sent = _email_code(email, code)
    resp = {"ok": True, "needs_verification": True, "emailed": sent}
    if not sent and DEV_MODE:
        resp["dev_otp"] = code   # local dev only: show the code when email is not configured
    return resp

@app.post("/auth/verify-otp")
def auth_verify_otp(body: OtpRequest, response: Response):
    """Check the code; on success mark verified and start a session."""
    email = (body.email or "").strip().lower()
    status = auth.check_otp(email, body.code)
    if status == "ok":
        sid = auth.create_session(email)
        response.set_cookie("sid", sid, httponly=True, samesite="lax",
                            secure=SECURE_COOKIES, max_age=auth.SESSION_TTL)
        return {"ok": True, "authenticated": True}
    raise HTTPException(400, {"bad": "Incorrect code.",
                              "expired": "That code expired. Request a new one.",
                              "locked": "Too many wrong attempts. Request a new code."
                              }.get(status, "Verification failed."))

@app.post("/auth/login")
def auth_login(body: LoginRequest, request: Request, response: Response):
    """Sign in with email + password. Unverified accounts get a fresh code instead."""
    if not _rate_ok("auth:" + _client_ip(request), limit=10, window=600):
        raise HTTPException(429, "Too many attempts. Please wait a few minutes.")
    email = (body.email or "").strip().lower()
    locked = auth.login_locked(email)
    if locked:
        raise HTTPException(429, f"Account temporarily locked after too many failed sign-ins. "
                                 f"Try again in about {max(1, locked // 60)} minute(s).")
    status = auth.verify_login(email, body.password or "")
    if status == "ok":
        auth.clear_login_fails(email)
        sid = auth.create_session(email)
        response.set_cookie("sid", sid, httponly=True, samesite="lax",
                            secure=SECURE_COOKIES, max_age=auth.SESSION_TTL)
        return {"ok": True, "authenticated": True}
    if status == "unverified":
        code = auth.set_otp(email); sent = _email_code(email, code)
        resp = {"ok": False, "needs_verification": True, "emailed": sent}
        if not sent and DEV_MODE:
            resp["dev_otp"] = code
        return resp
    auth.record_login_fail(email)
    raise HTTPException(401, "Incorrect email or password.")

@app.post("/auth/resend-otp")
def auth_resend(body: EmailOnly, request: Request):
    """Send a fresh verification code."""
    if not _rate_ok("otp:" + _client_ip(request), limit=5, window=600):
        raise HTTPException(429, "Too many code requests. Please wait a few minutes.")
    email = (body.email or "").strip().lower()
    if not auth.user_exists(email) or not auth.otp_send_allowed(email):
        return {"ok": True}   # do not reveal which emails are registered / silently throttle
    code = auth.set_otp(email); sent = _email_code(email, code)
    resp = {"ok": True, "emailed": sent}
    if not sent and DEV_MODE:
        resp["dev_otp"] = code
    return resp

class ResetRequest(BaseModel):
    email: str
    code: str
    password: str

@app.post("/auth/forgot")
def auth_forgot(body: EmailOnly, request: Request):
    """Send a code so the person can reset a forgotten password."""
    if not _rate_ok("otp:" + _client_ip(request), limit=5, window=600):
        raise HTTPException(429, "Too many requests. Please wait a few minutes.")
    email = (body.email or "").strip().lower()
    if auth.user_exists(email) and auth.otp_send_allowed(email):
        code = auth.set_otp(email); sent = _email_code(email, code)
        resp = {"ok": True, "emailed": sent}
        if not sent and DEV_MODE:
            resp["dev_otp"] = code
        return resp
    return {"ok": True}   # do not reveal which emails are registered

@app.post("/auth/reset")
def auth_reset(body: ResetRequest, response: Response):
    """Verify the code, set the new password, and sign the person in."""
    email = (body.email or "").strip().lower()
    if len(body.password or "") < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    status = auth.check_otp(email, body.code)
    if status != "ok":
        raise HTTPException(400, {"bad": "Incorrect code.",
                                  "expired": "That code expired. Request a new one.",
                                  "locked": "Too many wrong attempts. Request a new code."
                                  }.get(status, "Reset failed."))
    auth.set_password(email, body.password)
    sid = auth.create_session(email)
    response.set_cookie("sid", sid, httponly=True, samesite="lax",
                        secure=SECURE_COOKIES, max_age=auth.SESSION_TTL)
    return {"ok": True, "authenticated": True}

@app.get("/auth/me")
def auth_me(sid: str = Cookie(None)):
    """Who am I, used by the frontend to show logged-in state."""
    email = auth.email_for_session(sid)
    if not email:
        return {"authenticated": False}
    return {"authenticated": True, "user": auth.get_user(email)}

@app.post("/auth/logout")
def auth_logout(response: Response, sid: str = Cookie(None)):
    if sid:
        auth.destroy_session(sid)
    response.delete_cookie("sid")
    return {"ok": True}

class AccountUpdate(BaseModel):
    tier: Optional[int] = None
    telegram: Optional[str] = None
    whatsapp: Optional[str] = None

@app.get("/account")
def account_get(sid: str = Cookie(None)):
    email = auth.email_for_session(sid)
    if not email:
        raise HTTPException(401, "not logged in")
    return auth.get_user(email)

@app.post("/account")
def account_update(body: AccountUpdate, sid: str = Cookie(None)):
    email = auth.email_for_session(sid)
    if not email:
        raise HTTPException(401, "not logged in")
    if body.tier is not None and not (65 <= body.tier <= 98):
        raise HTTPException(400, "tier must be 65-98")
    wa = (body.whatsapp or "").strip().replace(" ", "")
    if wa and not (wa.lstrip("+").isdigit() and 10 <= len(wa.lstrip("+")) <= 15):
        raise HTTPException(400, "whatsapp must be a phone number with country code")
    auth.update_user(email, tier=body.tier, telegram=body.telegram, whatsapp=wa or None)
    u = auth.get_user(email) or {}
    # Mirror the saved prefs into users.json so the alert sender (which reads that
    # file) picks them up. Keeps the account store and the alert list in sync, so
    # a signed-in person sets their tier and channels once and alerts actually fire.
    if u.get("tier"):
        users = load_json(USERS_FILE, default=[])
        if not isinstance(users, list):
            users = []
        users = [x for x in users if x.get("email") and x.get("email") != email]
        users.append({"email": email, "whatsapp": u.get("whatsapp"),
                      "telegram": u.get("telegram"), "tier": u.get("tier"),
                      "joined": __import__("datetime").datetime.now().isoformat()[:19]})
        save_json(USERS_FILE, users)
    return {"ok": True, "user": u}

def _remove_from_alert_list(email):
    users = load_json(USERS_FILE, default=[])
    if isinstance(users, list):
        save_json(USERS_FILE, [x for x in users if x.get("email") != email])

@app.post("/account/unsubscribe")
def account_unsubscribe(sid: str = Cookie(None)):
    """Logged-in person turns their email alerts off."""
    email = auth.email_for_session(sid)
    if not email:
        raise HTTPException(401, "not logged in")
    auth.unsubscribe(email)
    _remove_from_alert_list(email)
    return {"ok": True, "unsubscribed": True}

@app.get("/unsubscribe")
def unsubscribe_link(e: str = "", t: str = ""):
    """One-click unsubscribe from the link at the bottom of every alert email."""
    email = (e or "").strip().lower()
    def page(msg):
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<div style=\"font-family:system-ui,sans-serif;max-width:460px;margin:80px auto;"
            "padding:0 20px;text-align:center;color:#222\"><h2>IPOPredict</h2>"
            f"<p style='font-size:16px;line-height:1.6;color:#555'>{msg}</p></div>")
    if not email or not auth.check_unsub_token(email, t):
        return page("That unsubscribe link is not valid. If you still get emails, "
                    "open the site and turn alerts off from your account.")
    auth.unsubscribe(email)
    _remove_from_alert_list(email)
    return page("You are unsubscribed. You will not get any more IPO alert emails. "
                "You can turn them back on anytime from your account.")

def _legal_shell(title: str, updated: str, body_html: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{title} - IPOPredict</title>"
        "<div style=\"font-family:system-ui,-apple-system,sans-serif;max-width:760px;margin:0 auto;"
        "padding:32px 20px 80px;color:#1a1a1a;line-height:1.65\">"
        "<p style='margin:0 0 24px'><a href='/' style='color:#2563eb;text-decoration:none'>&larr; Back to IPOPredict</a></p>"
        f"<h1 style='font-size:26px;margin:0 0 4px'>{title}</h1>"
        f"<p style='color:#888;font-size:13px;margin:0 0 28px'>Last updated: {updated}</p>"
        f"{body_html}</div>")

_LEGAL_UPDATED = "1 July 2026"

@app.get("/disclaimer")
def disclaimer_page():
    return _legal_shell("Disclaimer &amp; Risk Notice", _LEGAL_UPDATED, """
<p><b>IPOPredict is an educational and informational tool. It is not investment advice, and we are not a SEBI-registered Research Analyst or Investment Adviser.</b></p>
<p>What we show are the outputs of statistical models trained on historical Indian mainboard IPO data. Specifically:</p>
<ul>
<li>Every prediction is about the <b>listing-day move only</b> (how a share is likely to open versus its offer price), not long-term returns.</li>
<li>Labels such as APPLY, SKIP, accuracy tiers, and win rates describe <b>historical model behaviour on past data</b>. They are not a recommendation to buy, sell, subscribe to, or hold any security.</li>
<li>Past accuracy <b>never guarantees</b> a future result. Markets are uncertain and you can lose money.</li>
<li>Our models use machine learning and AI. Model outputs can be wrong, biased by the data, or affected by events the data never saw.</li>
</ul>
<p>Any decision you make with securities is <b>your own</b>. Please do your own research and consider consulting a SEBI-registered adviser before investing. To the maximum extent permitted by law, IPOPredict and its operator accept no liability for any loss arising from use of this tool.</p>""")

@app.get("/privacy")
def privacy_page():
    return _legal_shell("Privacy Policy", _LEGAL_UPDATED, """
<p>This policy explains what IPOPredict collects and how it is handled, consistent with India's Digital Personal Data Protection Act, 2023.</p>
<h3>What we collect</h3>
<ul>
<li><b>Account data:</b> your email address and a securely hashed password (we never store your password in readable form).</li>
<li><b>Alert preferences:</b> your chosen accuracy tier and, optionally, a Telegram handle or phone number you provide for alerts.</li>
<li><b>Basic technical data:</b> temporary rate-limiting counters tied to your IP to prevent abuse. We do not run third-party ad or tracking scripts.</li>
</ul>
<h3>Why we use it</h3>
<p>Only to run the service: to sign you in, to send verification codes, and to email you an alert when an IPO clears the tier you chose. We do <b>not</b> sell your data or share it for advertising.</p>
<h3>How it is stored and sent</h3>
<p>Data sits in our application database. Verification codes and alerts are delivered by email (and, if you opt in, Telegram). Codes expire in 10 minutes.</p>
<h3>Your rights</h3>
<p>You can view or change your details from your account, turn off alerts anytime (one click in every email or from your account), and request full deletion of your account and data by contacting us. We keep account data only while your account is active.</p>
<h3>Contact / grievances</h3>
<p>For any privacy request or complaint, contact the operator at the email address published on the site. We will respond within a reasonable time.</p>""")

@app.get("/terms")
def terms_page():
    return _legal_shell("Terms &amp; Conditions", _LEGAL_UPDATED, """
<p>By creating an account or using IPOPredict ("the Service"), you agree to these terms.</p>
<h3>1. What the Service is</h3>
<p>IPOPredict provides educational, model-generated information about Indian mainboard IPOs, focused on likely listing-day movement. It is <b>not</b> investment advice and we are <b>not</b> a SEBI-registered Research Analyst or Investment Adviser. See our <a href="/disclaimer">Disclaimer</a>.</p>
<h3>2. Your responsibilities</h3>
<p>You must give accurate details, keep your login secure, and be responsible for decisions you make. You may not attempt to break, overload, scrape, or abuse the Service, or use it for anything unlawful.</p>
<h3>3. No guarantee</h3>
<p>The Service is provided "as is", without warranty of accuracy, availability, or fitness for a particular purpose. Model outputs may be wrong. Past accuracy does not guarantee future results.</p>
<h3>4. Limitation of liability</h3>
<p>To the maximum extent permitted by law, IPOPredict and its operator are not liable for any direct or indirect loss (including investment losses) arising from use of, or reliance on, the Service.</p>
<h3>5. Accounts and termination</h3>
<p>We may suspend or remove accounts that abuse the Service or breach these terms. You may delete your account at any time.</p>
<h3>6. Changes</h3>
<p>We may update these terms; material changes will be reflected by the "last updated" date above.</p>
<h3>7. Governing law</h3>
<p>These terms are governed by the laws of India, and the courts of India have jurisdiction.</p>""")

@app.get("/stats")
def stats():
    """Public stats, for the homepage."""
    """Public stats, for the homepage."""
    qualified = load_json(QUALIFIED_FILE, [])
    calib     = load_json(CALIBRATION_FILE, {})
    users     = load_json(USERS_FILE, [])
    return {
        "ipos_tracked":    len(qualified),
        "tiers_available": len(calib.get("slider_levels", {})),
        "users_subscribed":len(users),
        "tier_range":      "65-98%",
    }

class AskBody(BaseModel):
    question: str

_research_agent = None

@app.post("/ask")
def ask(body: AskBody, request: Request):
    """Run the research agent. Rules first, Gemini free tier if configured."""
    if not _rate_ok("ask:" + _client_ip(request), limit=20, window=600):
        raise HTTPException(429, "Too many questions too fast. Please wait a moment.")
    global _research_agent
    q = (body.question or "").strip()
    if not q or len(q) > 300:
        raise HTTPException(400, "question must be 1-300 characters")
    try:
        if _research_agent is None:
            import io, contextlib
            import ipo_research_agent as ra
            _research_agent = ra
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = _research_agent.ResearchAgent().run(q)
        # the agent returns a flat dict: question, parser, intent, answer, insights
        insights = r.get("insights") or []
        if not insights:
            insights = [o.get("insight", "") for o in r.get("observations", []) if o.get("insight")]
        intent = r.get("intent")
        intent_type = intent.get("type") if isinstance(intent, dict) else intent
        answer = r.get("answer") or " ".join(insights[:3])
        return {
            "question": q,
            "parser": r.get("parser", "rules"),
            "intent": intent_type,
            "insights": insights,
            "answer": answer,
        }
    except FileNotFoundError:
        raise HTTPException(503, "dataset not available on this server")
    except Exception as e:
        raise HTTPException(500, f"research agent error: {type(e).__name__}")
