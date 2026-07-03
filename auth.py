import os, sqlite3, secrets, time, hashlib, hmac
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

DB_URL      = os.environ.get("DATABASE_URL", "")
SQLITE      = Path(os.environ.get("AUTH_DB", "ipo_ledger.db"))
SESSION_TTL = 30 * 24 * 3600     # stay signed in 30 days
OTP_TTL     = 10 * 60            # verification codes valid 10 minutes
OTP_MAX_TRY = 5                  # wrong guesses before a code is locked
_PBKDF_ITERS = 200_000

_USE_PG = DB_URL.startswith("postgres")

@contextmanager
def _conn():
    """Yield a DB connection for either backend with a uniform placeholder."""
    if _USE_PG:
        import psycopg2
        c = psycopg2.connect(DB_URL)
        try:
            yield c, "%s"; c.commit()
        finally:
            c.close()
    else:
        c = sqlite3.connect(SQLITE)
        try:
            yield c, "?"; c.commit()
        finally:
            c.close()

def init_db():
    """Create tables if absent, and add newer columns to older DBs."""
    with _conn() as (c, _):
        cur = c.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                email         TEXT PRIMARY KEY,
                password_hash TEXT,
                verified      INTEGER DEFAULT 0,
                tier          INTEGER,
                telegram      TEXT,
                whatsapp      TEXT,
                created_at    INTEGER
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS otp_codes (
                email      TEXT PRIMARY KEY,
                code       TEXT,
                expires_at INTEGER,
                tries      INTEGER DEFAULT 0
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                sid        TEXT PRIMARY KEY,
                email      TEXT,
                expires_at INTEGER
            )""")
        # per-account login lockout (defends brute force even across rotating IPs)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS login_guard (
                email        TEXT PRIMARY KEY,
                fails        INTEGER DEFAULT 0,
                locked_until INTEGER DEFAULT 0
            )""")
        # per-email throttle on verification-code emails (anti email-bomb / quota abuse)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS otp_throttle (
                email     TEXT PRIMARY KEY,
                last_at   INTEGER DEFAULT 0,
                day_count INTEGER DEFAULT 0,
                day_start INTEGER DEFAULT 0
            )""")
        # commit the tables NOW, before any risky migration below. On Postgres a
        # failed statement aborts the whole transaction, so the ALTERs must not
        # share a transaction with the CREATE TABLEs above.
        c.commit()

    # migrate older DBs: add columns that may not exist yet. Each ALTER runs in
    # its OWN connection/transaction so a failure (column already exists) can't
    # poison the others - critical on Postgres.
    for ddl in ("ALTER TABLE users ADD COLUMN password_hash TEXT",
                "ALTER TABLE users ADD COLUMN verified INTEGER DEFAULT 0",
                "ALTER TABLE users ADD COLUMN consent_at INTEGER"):
        try:
            with _conn() as (c2, _):
                c2.cursor().execute(ddl); c2.commit()
        except Exception:
            pass   # column already exists - fine


def _now() -> int:
    return int(time.time())

# passwords (stdlib pbkdf2, salted)
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF_ITERS)
    return salt.hex() + "$" + dk.hex()

def verify_password(password: str, stored: str) -> bool:
    if not stored or "$" not in stored:
        return False
    salt_hex, hash_hex = stored.split("$", 1)
    try:
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), _PBKDF_ITERS)
    except ValueError:
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)

# accounts
def user_exists(email: str) -> bool:
    with _conn() as (c, ph):
        cur = c.cursor(); cur.execute(f"SELECT 1 FROM users WHERE email={ph}", (email.lower().strip(),))
        return cur.fetchone() is not None

def create_account(email: str, password: str) -> str:
    """Create an UNVERIFIED account. Returns 'created', 'exists', or 'exists_unverified'."""
    email = email.lower().strip()
    with _conn() as (c, ph):
        cur = c.cursor()
        cur.execute(f"SELECT verified FROM users WHERE email={ph}", (email,))
        row = cur.fetchone()
        if row:
            return "exists" if row[0] else "exists_unverified"
        cur.execute(
            f"INSERT INTO users (email, password_hash, verified, tier, created_at) "
            f"VALUES ({ph},{ph},0,{ph},{ph})",
            (email, hash_password(password), 90, _now()))
        return "created"

def set_password(email: str, password: str):
    with _conn() as (c, ph):
        c.cursor().execute(f"UPDATE users SET password_hash={ph} WHERE email={ph}",
                           (hash_password(password), email.lower().strip()))

def verify_login(email: str, password: str) -> str:
    """Returns 'ok', 'bad' (wrong email/password), or 'unverified'."""
    email = email.lower().strip()
    with _conn() as (c, ph):
        cur = c.cursor()
        cur.execute(f"SELECT password_hash, verified FROM users WHERE email={ph}", (email,))
        row = cur.fetchone()
    if not row or not verify_password(password, row[0]):
        return "bad"
    return "ok" if row[1] else "unverified"

# one-time codes
def set_otp(email: str) -> str:
    """Generate + store a fresh 6-digit code, return it (to be emailed)."""
    email = email.lower().strip()
    code = f"{secrets.randbelow(10**6):06d}"
    with _conn() as (c, ph):
        cur = c.cursor()
        cur.execute(f"DELETE FROM otp_codes WHERE email={ph}", (email,))
        cur.execute(f"INSERT INTO otp_codes (email, code, expires_at, tries) VALUES ({ph},{ph},{ph},0)",
                    (email, code, _now() + OTP_TTL))
    return code

def check_otp(email: str, code: str) -> str:
    """'ok' (and marks account verified), 'bad', 'expired', or 'locked'."""
    email = email.lower().strip()
    code = (code or "").strip()
    with _conn() as (c, ph):
        cur = c.cursor()
        cur.execute(f"SELECT code, expires_at, tries FROM otp_codes WHERE email={ph}", (email,))
        row = cur.fetchone()
        if not row:
            return "bad"
        real, exp, tries = row
        if _now() > exp:
            cur.execute(f"DELETE FROM otp_codes WHERE email={ph}", (email,)); return "expired"
        if tries >= OTP_MAX_TRY:
            return "locked"
        if not hmac.compare_digest(str(real), code):
            cur.execute(f"UPDATE otp_codes SET tries=tries+1 WHERE email={ph}", (email,)); return "bad"
        cur.execute(f"DELETE FROM otp_codes WHERE email={ph}", (email,))
        cur.execute(f"UPDATE users SET verified=1 WHERE email={ph}", (email,))
        return "ok"

# sessions
def create_session(email: str) -> str:
    sid = secrets.token_urlsafe(32)
    with _conn() as (c, ph):
        c.cursor().execute(f"INSERT INTO sessions (sid, email, expires_at) VALUES ({ph},{ph},{ph})",
                           (sid, email.lower().strip(), _now() + SESSION_TTL))
    return sid

def email_for_session(sid: str) -> Optional[str]:
    if not sid:
        return None
    with _conn() as (c, ph):
        cur = c.cursor()
        cur.execute(f"SELECT email, expires_at FROM sessions WHERE sid={ph}", (sid,))
        row = cur.fetchone()
        if not row or _now() > row[1]:
            return None
        return row[0]

def destroy_session(sid: str):
    with _conn() as (c, ph):
        c.cursor().execute(f"DELETE FROM sessions WHERE sid={ph}", (sid,))

def get_user(email: str) -> Optional[dict]:
    with _conn() as (c, ph):
        cur = c.cursor()
        cur.execute(f"SELECT email, tier, telegram, whatsapp FROM users WHERE email={ph}", (email.lower().strip(),))
        row = cur.fetchone()
        if not row:
            return None
        return {"email": row[0], "tier": row[1], "telegram": row[2], "whatsapp": row[3]}

def update_user(email: str, tier: int = None, telegram: str = None, whatsapp: str = None):
    fields, vals = [], []
    if tier is not None:     fields.append("tier");     vals.append(tier)
    if telegram is not None: fields.append("telegram"); vals.append(telegram or None)
    if whatsapp is not None: fields.append("whatsapp"); vals.append(whatsapp or None)
    if not fields:
        return
    with _conn() as (c, ph):
        set_clause = ", ".join(f"{f}={ph}" for f in fields)
        c.cursor().execute(f"UPDATE users SET {set_clause} WHERE email={ph}", (*vals, email.lower().strip()))

def all_subscribers() -> list:
    with _conn() as (c, ph):
        cur = c.cursor()
        cur.execute("SELECT email, tier, telegram, whatsapp FROM users WHERE tier IS NOT NULL AND verified=1")
        return [{"email": r[0], "tier": r[1], "telegram": r[2], "whatsapp": r[3]} for r in cur.fetchall()]

def unsubscribe(email: str):
    """Stop all email alerts for this person by clearing their tier."""
    with _conn() as (c, ph):
        c.cursor().execute(f"UPDATE users SET tier=NULL WHERE email={ph}", (email.lower().strip(),))

_UNSUB_SECRET = os.environ.get("APP_SECRET", "ipo-ledger-unsub-v1")

def unsub_token(email: str) -> str:
    """Short signed token so the email link works without a login."""
    return hmac.new(_UNSUB_SECRET.encode(), email.lower().strip().encode(), hashlib.sha256).hexdigest()[:16]

def check_unsub_token(email: str, token: str) -> bool:
    return hmac.compare_digest(unsub_token(email), (token or "").strip())

# brute-force lockout (per account)
LOGIN_MAX_FAILS = 8          # wrong passwords before a temporary lock
LOGIN_LOCK_SECS = 15 * 60    # how long the account stays locked

def login_locked(email: str) -> int:
    """Return seconds remaining if the account is locked, else 0."""
    email = email.lower().strip()
    with _conn() as (c, ph):
        cur = c.cursor()
        cur.execute(f"SELECT locked_until FROM login_guard WHERE email={ph}", (email,))
        row = cur.fetchone()
    if row and row[0] and row[0] > _now():
        return row[0] - _now()
    return 0

def record_login_fail(email: str):
    email = email.lower().strip()
    with _conn() as (c, ph):
        cur = c.cursor()
        cur.execute(f"SELECT fails FROM login_guard WHERE email={ph}", (email,))
        row = cur.fetchone()
        fails = (row[0] if row else 0) + 1
        locked_until = _now() + LOGIN_LOCK_SECS if fails >= LOGIN_MAX_FAILS else 0
        if fails >= LOGIN_MAX_FAILS:
            fails = 0     # reset the counter once locked
        if row:
            cur.execute(f"UPDATE login_guard SET fails={ph}, locked_until={ph} WHERE email={ph}",
                        (fails, locked_until, email))
        else:
            cur.execute(f"INSERT INTO login_guard (email, fails, locked_until) VALUES ({ph},{ph},{ph})",
                        (email, fails, locked_until))

def clear_login_fails(email: str):
    with _conn() as (c, ph):
        c.cursor().execute(f"DELETE FROM login_guard WHERE email={ph}", (email.lower().strip(),))

def record_consent(email: str):
    """Timestamp that this account accepted Terms + Privacy + not-advice notice."""
    with _conn() as (c, ph):
        c.cursor().execute(f"UPDATE users SET consent_at={ph} WHERE email={ph}",
                           (_now(), email.lower().strip()))

# verification-code send throttle (per email)
OTP_MIN_GAP  = 60            # at least this many seconds between codes
OTP_MAX_DAY  = 5            # and no more than this many per day

def otp_send_allowed(email: str) -> bool:
    """Rate-limit code *generation* per email address, not just per IP, so a
    single victim's inbox cannot be flooded and the daily mail quota abused."""
    email = email.lower().strip()
    now = _now()
    with _conn() as (c, ph):
        cur = c.cursor()
        cur.execute(f"SELECT last_at, day_count, day_start FROM otp_throttle WHERE email={ph}", (email,))
        row = cur.fetchone()
        last_at, day_count, day_start = row if row else (0, 0, 0)
        if now - day_start >= 86400:
            day_count, day_start = 0, now
        if now - last_at < OTP_MIN_GAP or day_count >= OTP_MAX_DAY:
            return False
        day_count += 1
        if row:
            cur.execute(f"UPDATE otp_throttle SET last_at={ph}, day_count={ph}, day_start={ph} WHERE email={ph}",
                        (now, day_count, day_start, email))
        else:
            cur.execute(f"INSERT INTO otp_throttle (email, last_at, day_count, day_start) VALUES ({ph},{ph},{ph},{ph})",
                        (email, now, day_count, day_start))
        return True
