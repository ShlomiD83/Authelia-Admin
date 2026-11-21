"""Authelia Admin - small web UI to manage Authelia config, users and passwords."""
import base64
import difflib
import glob
import hashlib
import hmac
import json
import struct
import subprocess
import urllib.parse
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

import yaml
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask import (Flask, abort, flash, g, has_request_context, jsonify, make_response, redirect, render_template,
                   request, send_from_directory, session, url_for)

try:  # QR codes for TOTP setup; optional (the secret can always be typed in by hand)
    import segno
except ImportError:
    segno = None

try:  # passkeys (WebAuthn); the app still runs without the library, just without passkeys
    from webauthn import (base64url_to_bytes, generate_authentication_options,
                          generate_registration_options, verify_authentication_response,
                          verify_registration_response)
    from webauthn.helpers import bytes_to_base64url, options_to_json_dict
    from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                          PublicKeyCredentialDescriptor,
                                          ResidentKeyRequirement,
                                          UserVerificationRequirement)
    WEBAUTHN_OK = True
except ImportError:
    WEBAUTHN_OK = False

CONFIG_DIR = os.environ.get("AUTHELIA_CONFIG_DIR", "/config")
CONFIG_FILE = os.path.join(CONFIG_DIR, os.environ.get("AUTHELIA_CONFIG_FILE", "configuration.yml"))
USERS_FILE = os.path.join(CONFIG_DIR, os.environ.get("AUTHELIA_USERS_FILE", "users_database.yml"))
BACKUP_DIR = os.path.join(CONFIG_DIR, "backups")
MAX_BACKUPS = int(os.environ.get("MAX_BACKUPS", "30"))
AUTHELIA_CONTAINER = os.environ.get("AUTHELIA_CONTAINER", "")  # enables restart button (Docker socket mode)
# Socket-free restart: the button creates this file and a host-side systemd path unit restarts Authelia.
RESTART_FLAG_FILE = os.environ.get("RESTART_FLAG_FILE", "")
# Optional: Authelia's address as seen from this container (e.g. http://authelia:9091). After a restart the
# page polls Authelia's /api/health through this app and reports when Authelia is back up.
AUTHELIA_URL = os.environ.get("AUTHELIA_URL", "").strip().rstrip("/")
if AUTHELIA_URL and not re.match(r"^https?://", AUTHELIA_URL):
    print("WARNING: AUTHELIA_URL must start with http:// or https://, ignoring it", flush=True)
    AUTHELIA_URL = ""

# Time zone for backup timestamps, e.g. TZ=Asia/Jerusalem (uses the tzdata pip package, so it
# works even though the runtime image has no system zoneinfo). Falls back to UTC if invalid.
try:
    TZINFO = ZoneInfo(os.environ.get("TZ") or "UTC")
except Exception:
    print("WARNING: invalid TZ %r, using UTC" % os.environ.get("TZ"), flush=True)
    TZINFO = ZoneInfo("UTC")


def now():
    return datetime.now(TZINFO)

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")

# Passkeys: ADMIN_RP_ID is the site's domain (e.g. admin.example.com) and ADMIN_ORIGIN its full
# https origin (https://admin.example.com). A passkey is bound to that domain. Needs HTTPS.
RP_ID = os.environ.get("ADMIN_RP_ID", "").strip()
RP_ORIGIN = os.environ.get("ADMIN_ORIGIN", "").strip().rstrip("/")
RP_NAME = os.environ.get("ADMIN_RP_NAME", "Authelia Admin")
PASSKEY_FILE = os.environ.get("PASSKEY_FILE") or os.path.join(CONFIG_DIR, "admin_passkeys.json")
PASSKEY_ONLY = os.environ.get("ADMIN_PASSKEY_ONLY", "false").lower() == "true"
PASSKEYS_ENABLED = WEBAUTHN_OK and bool(RP_ID and RP_ORIGIN)
ADMIN_HANDLE = hashlib.sha256(("authelia-admin:" + ADMIN_USER).encode()).digest()  # stable, not guessable from the name


# TOTP (RFC 6238, SHA-1, 6 digits, 30 s): a second step for PASSWORD sign-in. The secret lives in the
# environment (no database). Passkey sign-in skips it, since a passkey already proves two factors.
def _norm_secret(s):
    return re.sub(r"[\s-]", "", s or "").upper()


def _b32(secret):
    return base64.b32decode(secret + "=" * (-len(secret) % 8))


def valid_totp_secret(secret):
    try:
        return len(secret) >= 16 and len(_b32(secret)) >= 10
    except Exception:
        return False


TOTP_SECRET = _norm_secret(os.environ.get("ADMIN_TOTP_SECRET", ""))
if TOTP_SECRET and not valid_totp_secret(TOTP_SECRET):
    raise SystemExit("ADMIN_TOTP_SECRET is not a valid base32 secret (at least 16 characters, A-Z and 2-7). "
                     "Generate one on the Security page, or remove the variable.")


def totp_at(secret, counter, digits=6):
    mac = hmac.new(_b32(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    return str((struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)).zfill(digits)


_totp_last = {}  # secret -> last accepted time step (replay guard, per worker process)


def totp_verify(secret, code, record=True, window=1):
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"\d{6}", code):
        return False
    step_now = int(time.time() // 30)
    hit = None
    for step in range(step_now - window, step_now + window + 1):  # tolerate 30 s of clock drift
        if hmac.compare_digest(totp_at(secret, step), code):
            hit = step
    if hit is None:
        return False
    if record:
        if hit <= _totp_last.get(secret, -1):
            return False  # this code was already used
        _totp_last[secret] = hit
    return True


def totp_uri(secret):
    issuer = urllib.parse.quote(RP_NAME)
    return (f"otpauth://totp/{issuer}:{urllib.parse.quote(ADMIN_USER)}?secret={secret}"
            f"&issuer={issuer}&algorithm=SHA1&digits=6&period=30")
ADMIN_PASSWORD =os.environ.get("ADMIN_PASSWORD", "")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
# Safest way to supply the hash: a file (no compose `$` escaping needed).
_hash_file = os.environ.get("ADMIN_PASSWORD_HASH_FILE", "")
if _hash_file:
    try:
        with open(_hash_file, encoding="utf-8") as _fh:
            ADMIN_PASSWORD_HASH = _fh.read().strip()
    except OSError as _e:
        raise SystemExit(f"Cannot read ADMIN_PASSWORD_HASH_FILE {_hash_file!r}: {_e}")
if ADMIN_PASSWORD_HASH and not ADMIN_PASSWORD_HASH.startswith("$argon2"):
    raise SystemExit("ADMIN_PASSWORD_HASH does not look like an argon2 hash "
                     "(did compose mangle the '$' signs? use ADMIN_PASSWORD_HASH_FILE).")

# Authelia's documented argon2id defaults.
ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)

if not (ADMIN_PASSWORD or ADMIN_PASSWORD_HASH):
    raise SystemExit("Set ADMIN_PASSWORD or ADMIN_PASSWORD_HASH (argon2id) to protect this UI.")

app = Flask(__name__)

# Behind a reverse proxy every request seems to come from the proxy, so the login lockout would count
# all clients together. TRUST_PROXY=1 makes the app use the client address from the proxy's
# X-Forwarded-For header (number = proxies in front of the app). Only enable it when the proxy is the
# ONLY way to reach this container (e.g. publish the port on 127.0.0.1 or use a private Docker network),
# otherwise anyone could fake that header to dodge the lockout.
_trust = int(os.environ.get("TRUST_PROXY", "0") or 0)
if _trust > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_trust)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=3600,
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_failed = {}  # ip -> (count, first_ts)


# ---------- helpers ----------
def check_admin_password(pw: str) -> bool:
    if ADMIN_PASSWORD_HASH:
        try:
            return ph.verify(ADMIN_PASSWORD_HASH, pw)
        except Exception:
            return False
    return hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode())


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["can_restart"] = bool(AUTHELIA_CONTAINER or RESTART_FLAG_FILE)
app.jinja_env.globals["tzname"] = str(TZINFO)
app.jinja_env.globals["audit_problem"] = lambda: audit_error
app.jinja_env.globals["uid_gid"] = f"{os.getuid()}:{os.getgid()}"
app.jinja_env.globals["audit_file"] = os.environ.get("AUDIT_LOG_FILE") or os.path.join(CONFIG_DIR, "admin_audit.log")


# Changes are made one at a time: two saves arriving together (e.g. from two tabs) would otherwise both read
# users_database.yml before either writes it, and the second would silently undo the first. The app runs
# a single gunicorn worker process, so a thread lock is enough.
_change_lock = threading.Lock()


@app.before_request
def one_change_at_a_time():
    if request.method == "POST" and not request.path.startswith(("/restart/status", "/passkeys/login")):
        _change_lock.acquire()
        g.holds_change_lock = True


@app.teardown_request
def release_change_lock(exc):
    if g.pop("holds_change_lock", False):
        _change_lock.release()


@app.before_request
def protect():
    if request.method == "POST":
        sent = request.form.get("csrf", "") or request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(sent, session.get("csrf", "x")):
            # Usually the session ended (logout, timeout, or the container restarted without SECRET_KEY),
            # not an attack. Explain it instead of showing a bare "Bad Request".
            if request.is_json or request.headers.get("X-CSRF-Token"):
                return jsonify(ok=False, error="Your session expired. Reload the page and log in again."), 400
            if not session.get("auth"):
                flash("Your session expired, so that action was not carried out. Log in and try again "
                      "(configuration edits are kept in this browser and can be restored).", "error")
                return redirect(url_for("login"))
            flash("The page was out of date, so that action was not carried out. Please try again.", "error")
            return redirect(request.path)


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("auth"):
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper


# ---------- admin action log ----------
# One JSON object per line. Capped at about 1 MB: the previous file is kept as <name>.1, so roughly the
# last 2 MB of history (thousands of entries) is available.
AUDIT_FILE = os.environ.get("AUDIT_LOG_FILE") or os.path.join(CONFIG_DIR, "admin_audit.log")
AUDIT_MAX_BYTES = 1_000_000
# How far back the Activity page goes (sign-ins and admin actions), and a safety cap on rows per list.
try:
    ACTIVITY_DAYS = max(1, int(os.environ.get("ACTIVITY_DAYS", "30")))
except ValueError:
    ACTIVITY_DAYS = 30
ACTIVITY_MAX_ROWS = 2000


def audit(action, target="", detail=""):
    """Record an admin action. Never lets a logging problem break the action itself."""
    # "admin" only when signed in: a failed sign-in can come from anyone. Outside a request (the automatic
    # database backup) there is no admin or IP: the entry is marked "automatic".
    in_request = has_request_context()
    entry = {"time": now().isoformat(timespec="seconds"),
             "admin": (ADMIN_USER if session.get("auth") else "") if in_request else "automatic",
             "via": session.get("via", "") if in_request else "",
             "ip": request.remote_addr if in_request else "", "action": action,
             "target": str(target)[:200], "detail": str(detail)[:500]}
    global audit_error
    try:
        if os.path.exists(AUDIT_FILE) and os.path.getsize(AUDIT_FILE) > AUDIT_MAX_BYTES:
            os.replace(AUDIT_FILE, AUDIT_FILE + ".1")
        with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        audit_error = None
    except OSError as e:
        # shown on the dashboard and the Activity page, so a broken log doesn't go unnoticed
        audit_error = f"{e.strerror or e} ({e.filename or AUDIT_FILE})"
        app.logger.exception("Couldn't write the admin action log %s", AUDIT_FILE)


audit_error = None   # last problem writing the admin action log, None while it works
if os.path.exists(AUDIT_FILE) and not os.access(AUDIT_FILE, os.W_OK):
    audit_error = f"Permission denied ({AUDIT_FILE})"
elif not os.path.exists(AUDIT_FILE) and not os.access(os.path.dirname(AUDIT_FILE) or ".", os.W_OK):
    audit_error = f"Permission denied ({os.path.dirname(AUDIT_FILE)})"


def read_audit(limit=200, days=None):
    """Newest first; with `days`, only entries from the last `days` days."""
    lines = []
    for path in (AUDIT_FILE, AUDIT_FILE + ".1"):
        try:
            with open(path, encoding="utf-8") as fh:
                lines += fh.readlines()[::-1]   # current file first, then the older .1 file
        except OSError:
            continue
        if len(lines) >= limit:
            break
    cutoff = now() - timedelta(days=days) if days else None
    out = []
    for ln in lines:
        if len(out) >= limit:
            break
        try:
            e = json.loads(ln)
            e["when"] = datetime.fromisoformat(e["time"]) if e.get("time") else None
        except (ValueError, KeyError, TypeError):
            continue
        if cutoff and e["when"] and e["when"] < cutoff:
            continue
        out.append(e)
    return out


def backup_name(base):
    """Path for a new backup of `base` named by the current time; a second backup within the same second
    gets -2, -3, ... instead of overwriting the first (these names still sort in time order)."""
    stamp = now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"{base}.{stamp}")
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(BACKUP_DIR, f"{base}.{stamp}-{n}")
        n += 1
    return dest


def backup(path):
    """Copy path into the backups folder; returns the backup's path (None if there was nothing to back up)."""
    if not os.path.exists(path):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = backup_name(os.path.basename(path))
    shutil.copy2(path, dest)
    olds = sorted(glob.glob(os.path.join(BACKUP_DIR, os.path.basename(path) + ".*")))
    for old in olds[:-MAX_BACKUPS]:
        os.remove(old)
    return dest


def atomic_write(path, text):
    """Back up, then replace path with text. Returns the backup's path."""
    bk = backup(path)
    # Write in place of a temp file in the same dir, then replace (Authelia's watcher sees one event).
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        try:
            shutil.copymode(path, tmp)
        except FileNotFoundError:
            pass
        try:
            os.replace(tmp, path)
        except OSError:
            # single-file bind mounts can't be replaced; write in place
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.remove(tmp)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return bk


def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("users") or {}


def save_users(users):
    atomic_write(USERS_FILE, yaml.safe_dump({"users": users}, sort_keys=True, allow_unicode=True,
                                            default_flow_style=False))


def parse_groups(raw):
    return sorted({g.strip() for g in re.split(r"[,\n]", raw or "") if g.strip()})


def validate_password(pw):
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if len(pw) > 128:
        return "Password must be at most 128 characters."
    return None


# ---------- auth ----------
def load_passkeys():
    try:
        with open(PASSKEY_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_passkeys(items):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(PASSKEY_FILE), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(items, fh, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, PASSKEY_FILE)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# ---- passkey signing ----
# Each entry carries an HMAC made with a key derived from SECRET_KEY, which lives in the compose file, not
# in the Authelia folder. Someone who can only write to that folder can't add or alter a passkey: entries
# without a valid signature are ignored for sign-in and shown as "not trusted" on the Security page, where
# a signed-in admin can trust them. Needs a fixed SECRET_KEY; without one (random per start) signing is off.
PASSKEY_SIGNING = bool(os.environ.get("SECRET_KEY"))
_PK_KEY = hmac.new(app.config["SECRET_KEY"].encode(), b"authelia-admin passkey signing v1",
                   hashlib.sha256).digest()
_PK_SIGNED_FIELDS = ("id", "public_key", "name", "created")


def pk_signature(p):
    msg = json.dumps({k: p.get(k) for k in _PK_SIGNED_FIELDS}, sort_keys=True, separators=(",", ":"))
    return hmac.new(_PK_KEY, msg.encode(), hashlib.sha256).hexdigest()


def pk_sign(p):
    if PASSKEY_SIGNING:
        p["sig"] = pk_signature(p)
    return p


def pk_trusted(p):
    if not PASSKEY_SIGNING:
        return True
    return isinstance(p.get("sig"), str) and hmac.compare_digest(p["sig"], pk_signature(p))


def trusted_passkeys():
    """The passkeys that may be used to sign in."""
    return [p for p in load_passkeys() if pk_trusted(p)]


def password_login_allowed():
    """Password sign-in is only switched off when passkey-only mode is on AND a trusted passkey exists, so
    untrusted or deleted passkeys never lock you out."""
    return not (PASSKEY_ONLY and PASSKEYS_ENABLED and trusted_passkeys())


def login_ctx():
    return dict(show_passkey=PASSKEYS_ENABLED and bool(trusted_passkeys()), password_ok=password_login_allowed())


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if not password_login_allowed():
            flash("Password sign-in is disabled. Use a passkey.", "error")
            return render_template("login.html", **login_ctx()), 403
        ip = request.remote_addr
        count, first = _failed.get(ip, (0, time.time()))
        if time.time() - first > 900:
            count, first = 0, time.time()
        if count >= 5:
            if count == 5:
                audit("Sign-in blocked", "", "too many failed attempts from this IP")
                _failed[ip] = (count + 1, first)
            flash("Too many failed attempts. Try again in 15 minutes.", "error")
            return render_template("login.html", **login_ctx()), 429
        u, p = request.form.get("username", ""), request.form.get("password", "")
        if hmac.compare_digest(u, ADMIN_USER) and check_admin_password(p):
            session.clear()
            if TOTP_SECRET:  # password is right; the one-time code is still needed
                session["pw_ok"] = time.time()
                return redirect(url_for("login_totp"))
            _failed.pop(ip, None)
            session["auth"] = True
            session["via"] = "password"
            session.permanent = True
            audit("Signed in")
            return redirect(url_for("dashboard"))
        _failed[ip] = (count + 1, first)
        audit("Sign-in failed", u[:64], "wrong user name or password")
        flash("Invalid credentials.", "error")
    return render_template("login.html", **login_ctx())


@app.route("/login/totp", methods=["GET", "POST"])
def login_totp():
    if not TOTP_SECRET:
        return redirect(url_for("login"))
    t = session.get("pw_ok")
    if not t or time.time() - t > 300:  # the password step must have happened in the last 5 minutes
        session.clear()
        flash("Sign-in expired. Start again.", "error")
        return redirect(url_for("login"))
    if request.method == "POST":
        ip = request.remote_addr
        count, first = _failed.get(ip, (0, time.time()))
        if time.time() - first > 900:
            count, first = 0, time.time()
        if count >= 5:
            flash("Too many failed attempts. Try again in 15 minutes.", "error")
            return render_template("login_totp.html"), 429
        if totp_verify(TOTP_SECRET, request.form.get("code", "")):
            _failed.pop(ip, None)
            session.clear()
            session["auth"] = True
            session["via"] = "password + TOTP"
            session.permanent = True
            audit("Signed in")
            return redirect(url_for("dashboard"))
        _failed[ip] = (count + 1, first)
        audit("Sign-in failed", ADMIN_USER, "wrong TOTP code")
        flash("That code didn't work. Check the code and your phone's clock.", "error")
    return render_template("login_totp.html")


@app.route("/logout", methods=["POST"])
def logout():
    if session.get("auth"):
        audit("Signed out")
    session.clear()
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    """Unauthenticated liveness probe for Docker's HEALTHCHECK: the config folder must be usable."""
    if os.path.isdir(CONFIG_DIR) and os.access(CONFIG_DIR, os.R_OK | os.W_OK):
        return "ok", 200
    return "config folder not accessible", 503


# ---------- passkeys (WebAuthn) ----------
def _pk_error(msg, code=400):
    return jsonify(ok=False, error=msg), code


def security_page(new_secret=None, verified=None):
    qr = None
    if new_secret and segno:
        qr = segno.make(totp_uri(new_secret), error="m").svg_inline(
            scale=5, border=3, dark="#000000", light="#ffffff", omitsize=True,  # viewBox only, so CSS can scale it
            title="Authenticator setup QR code")
    resp = make_response(render_template(
        "security.html", page="security",
        passkeys=[dict(p, trusted=pk_trusted(p)) for p in load_passkeys()], signing=PASSKEY_SIGNING,
        enabled=PASSKEYS_ENABLED, lib_ok=WEBAUTHN_OK, rp_id=RP_ID, origin=RP_ORIGIN,
        passkey_only=PASSKEY_ONLY, totp_enabled=bool(TOTP_SECRET),
        new_secret=new_secret, verified=verified, qr=qr))
    resp.headers["Cache-Control"] = "no-store"  # may contain a fresh TOTP secret
    return resp


@app.route("/security")
@login_required
def security():
    return security_page()


@app.route("/security/totp/new", methods=["POST"])
@login_required
def totp_new():
    """Show a fresh random secret (not stored anywhere) with a QR code, to be confirmed and then put in the env."""
    return security_page(new_secret=base64.b32encode(secrets.token_bytes(20)).decode())


@app.route("/security/totp/check", methods=["POST"])
@login_required
def totp_check():
    secret = _norm_secret(request.form.get("secret", ""))
    if not valid_totp_secret(secret):
        abort(400)
    ok = totp_verify(secret, request.form.get("code", ""), record=False)
    flash("Code is correct." if ok else "That code didn't work. Check the code and your phone's clock.",
          "ok" if ok else "error")
    return security_page(new_secret=secret, verified=ok)


@app.route("/passkeys/register/options", methods=["POST"])
@login_required
def pk_register_options():
    if not PASSKEYS_ENABLED:
        return _pk_error("Passkeys are not configured.", 404)
    existing = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(p["id"])) for p in load_passkeys()]
    opts = generate_registration_options(
        rp_id=RP_ID, rp_name=RP_NAME, user_name=ADMIN_USER, user_id=ADMIN_HANDLE,
        exclude_credentials=existing,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED))
    session["pk_reg"] = bytes_to_base64url(opts.challenge)
    return jsonify(options_to_json_dict(opts))


@app.route("/passkeys/register/verify", methods=["POST"])
@login_required
def pk_register_verify():
    if not PASSKEYS_ENABLED:
        return _pk_error("Passkeys are not configured.", 404)
    data = request.get_json(silent=True) or {}
    chal = session.pop("pk_reg", None)
    cred = data.get("credential")
    if not chal or not isinstance(cred, dict):
        return _pk_error("Registration expired. Try again.")
    try:
        v = verify_registration_response(
            credential=cred, expected_challenge=base64url_to_bytes(chal),
            expected_rp_id=RP_ID, expected_origin=RP_ORIGIN, require_user_verification=True)
    except Exception as e:
        return _pk_error(f"Registration failed: {e}")
    items = load_passkeys()
    cid = bytes_to_base64url(v.credential_id)
    if any(p.get("id") == cid for p in items):
        return _pk_error("That passkey is already registered.")
    transports = (cred.get("response") or {}).get("transports")
    items.append({
        "id": cid,
        "public_key": bytes_to_base64url(v.credential_public_key),
        "sign_count": v.sign_count,
        "name": (str(data.get("name") or "").strip()[:60] or "Passkey"),
        "created": now().isoformat(timespec="seconds"),
        "last_used": None,
        "synced": bool(v.credential_backed_up),
        "transports": [t for t in transports if isinstance(t, str)] if isinstance(transports, list) else [],
    })
    pk_sign(items[-1])
    save_passkeys(items)
    audit("Passkey added", items[-1]["name"])
    flash("Passkey added.", "ok")
    return jsonify(ok=True)


@app.route("/passkeys/<pid>/delete", methods=["POST"])
@login_required
def pk_delete(pid):
    items = load_passkeys()
    left = [p for p in items if p.get("id") != pid]
    if len(left) == len(items):
        abort(404)
    save_passkeys(left)
    audit("Passkey removed", next((p.get("name", "") for p in items if p.get("id") == pid), ""))
    flash("Passkey removed.", "ok")
    return redirect(url_for("security"))


@app.route("/passkeys/<pid>/trust", methods=["POST"])
@login_required
def pk_trust(pid):
    """Sign a passkey that has no valid signature (added before signing existed, or outside this app)."""
    if not PASSKEY_SIGNING:
        abort(404)
    items = load_passkeys()
    p = next((x for x in items if x.get("id") == pid), None)
    if not p:
        abort(404)
    pk_sign(p)
    save_passkeys(items)
    audit("Passkey trusted", p.get("name", ""), "created " + str(p.get("created") or "unknown"))
    flash(f"Passkey '{p.get('name', 'Passkey')}' is trusted and can be used to sign in.", "ok")
    return redirect(url_for("security"))


@app.route("/passkeys/login/options", methods=["POST"])
def pk_login_options():
    if not (PASSKEYS_ENABLED and trusted_passkeys()):
        return _pk_error("Passkey sign-in is not available.", 404)
    opts = generate_authentication_options(rp_id=RP_ID,
                                           user_verification=UserVerificationRequirement.REQUIRED)
    session["pk_auth"] = bytes_to_base64url(opts.challenge)
    return jsonify(options_to_json_dict(opts))


@app.route("/passkeys/login/verify", methods=["POST"])
def pk_login_verify():
    if not (PASSKEYS_ENABLED and trusted_passkeys()):
        return _pk_error("Passkey sign-in is not available.", 404)
    ip = request.remote_addr
    count, first = _failed.get(ip, (0, time.time()))
    if time.time() - first > 900:
        count, first = 0, time.time()
    if count >= 5:
        return _pk_error("Too many failed attempts. Try again in 15 minutes.", 429)
    chal = session.pop("pk_auth", None)
    cred = (request.get_json(silent=True) or {}).get("credential")
    items = load_passkeys()
    # only a trusted (correctly signed) passkey can sign in
    pk = next((p for p in items if isinstance(cred, dict) and p.get("id") == cred.get("id") and pk_trusted(p)), None)
    verified = None
    if chal and pk:
        try:
            verified = verify_authentication_response(
                credential=cred, expected_challenge=base64url_to_bytes(chal),
                expected_rp_id=RP_ID, expected_origin=RP_ORIGIN,
                credential_public_key=base64url_to_bytes(pk["public_key"]),
                credential_current_sign_count=int(pk.get("sign_count") or 0),
                require_user_verification=True)
        except Exception:
            verified = None
    if not verified:
        _failed[ip] = (count + 1, first)
        audit("Sign-in failed", ADMIN_USER, "passkey not accepted")
        return _pk_error("Passkey sign-in failed.", 401)
    pk["sign_count"] = verified.new_sign_count
    pk["last_used"] = now().isoformat(timespec="seconds")
    save_passkeys(items)
    _failed.pop(ip, None)
    session.clear()
    session["auth"] = True
    session["via"] = "passkey: " + pk.get("name", "Passkey")
    session.permanent = True
    audit("Signed in")
    return jsonify(ok=True, next=url_for("dashboard"))


# ---------- users ----------
# ---------- dashboard (home page) ----------
_BK_STAMP = re.compile(r"\.(\d{8}-\d{6})(?:-\d+)?$")


def backup_time(name):
    """Backups are named <file>.<YYYYMMDD-HHMMSS> in TZ (copies keep the source's mtime, so use the name)."""
    m = _BK_STAMP.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=TZINFO)
    except ValueError:
        return None


@app.route("/")
@login_required
def dashboard():
    all_users = load_users()
    active = {n: u for n, u in all_users.items() if not u.get("disabled")}
    users_info = {"total": len(all_users), "disabled": len(all_users) - len(active),
                  "admins": sum(1 for u in all_users.values() if "admins" in (u.get("groups") or []))}

    # security and recent sign-ins, from Authelia's database (None if it isn't available)
    sec = None
    con = authelia_db()
    if con:
        try:
            since = now() - timedelta(hours=24)
            rows = con.execute("SELECT time, successful, banned, username FROM authentication_logs "
                               "ORDER BY id DESC LIMIT 1000").fetchall()
            recent, fail24 = [], 0
            for r in rows:
                t = db_time(r["time"])
                if len(recent) < 5:
                    recent.append({"time": t, "user": r["username"], "ok": bool(r["successful"]),
                                   "banned": bool(r["banned"])})
                if t and t >= since and not r["successful"]:
                    fail24 += 1
            sec = {"bans": len(active_bans(con)), "fail24": fail24, "recent": recent}
        finally:
            con.close()
        info = user_security_info() or {}
        sec["no2fa"] = sum(1 for n in active if not (info.get(n, {}).get("totp") or info.get(n, {}).get("passkeys")))
        sec["active"] = len(active)

    # backups: count and the latest per file
    latest, count = {}, 0
    for f in glob.glob(os.path.join(BACKUP_DIR, "*")):
        if not os.path.isfile(f):
            continue
        count += 1
        n = os.path.basename(f)
        base, t = _BK_STAMP.sub("", n), backup_time(n)
        if t and (base not in latest or t > latest[base]):
            latest[base] = t
    backups_info = {"count": count, "latest": sorted(latest.items(), key=lambda kv: kv[1], reverse=True)[:4]}

    # configuration: when it last changed, and whether that was through this app
    cfg_changed = None
    try:
        cfg_changed = datetime.fromtimestamp(os.path.getmtime(CONFIG_FILE), TZINFO)
    except OSError:
        pass
    audit_recent = read_audit(200)
    cfg_by = None
    for a in audit_recent:
        if a.get("action") == "Configuration saved" or (a.get("action") == "Backup restored"
                                                        and a.get("target") == os.path.basename(CONFIG_FILE)):
            # the file's time and the logged save are the same moment (allow a few seconds)
            if cfg_changed and a.get("when") and abs((a["when"] - cfg_changed).total_seconds()) <= 5:
                cfg_by = a.get("admin") or "admin"
            break

    return render_template("dashboard.html", page="dashboard", users_info=users_info, sec=sec,
                           backups_info=backups_info, cfg_changed=cfg_changed, cfg_by=cfg_by,
                           actions=audit_recent[:5], known=set(all_users), authelia_url=bool(AUTHELIA_URL))


_validation_cache = {}   # fingerprint of configuration.yml -> (ok, problems)
_authelia_version = None


def authelia_version():
    global _authelia_version
    if _authelia_version is None and AUTHELIA_BIN:
        try:
            out = subprocess.run([AUTHELIA_BIN, "--version"], capture_output=True, text=True, timeout=10,
                                 env=CLI_ENV).stdout
            m = re.search(r"v?(\d+\.\d+\.\d+)", out)
            _authelia_version = "v" + m.group(1) if m else ""
        except Exception:
            _authelia_version = ""
    return _authelia_version or ""


# Newest Authelia release on GitHub, to tell when the image's built-in Authelia CLI (used for validation and
# the editor's schema) is behind. Cached for 6 hours (1 hour after a failure). UPDATE_CHECK=false turns it off.
UPDATE_CHECK = os.environ.get("UPDATE_CHECK", "true").lower() != "false"
_release_cache = {"at": 0.0, "version": None}


def latest_authelia_release():
    if not UPDATE_CHECK:
        return None
    age = time.time() - _release_cache["at"]
    if age < (6 * 3600 if _release_cache["version"] else 3600) and _release_cache["at"]:
        return _release_cache["version"]
    import urllib.request
    version = None
    try:
        req = urllib.request.Request("https://api.github.com/repos/authelia/authelia/releases/latest",
                                     headers={"Accept": "application/vnd.github+json",
                                              "User-Agent": "authelia-admin-update-check"})
        with urllib.request.urlopen(req, timeout=4) as r:
            tag = json.load(r).get("tag_name", "")
        m = re.match(r"v?(\d+\.\d+\.\d+)$", tag)
        version = "v" + m.group(1) if m else None
    except Exception:
        version = None   # offline or rate-limited: no notice, try again later
    _release_cache.update(at=time.time(), version=version)
    return version


def version_tuple(v):
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else None


@app.route("/dashboard/status")
@login_required
def dashboard_status():
    """The two slower checks, loaded by the dashboard after it opens."""
    text = read_text(CONFIG_FILE)
    fp = fingerprint(text)
    if fp not in _validation_cache:
        ok, out = authelia_validate(text)
        problems = 0
        if ok is False:
            errors = out.split("with warnings")[0]
            problems = len(re.findall(r"^\s*-\s", errors, flags=re.M)) or 1
        _validation_cache.clear()
        _validation_cache[fp] = (ok, problems)
    ok, problems = _validation_cache[fp]
    built, latest = authelia_version(), latest_authelia_release()
    update = bool(version_tuple(built) and version_tuple(latest) and version_tuple(latest) > version_tuple(built))
    resp = jsonify(authelia=authelia_health(), config_ok=ok, config_problems=problems,
                   version=built, latest=latest, update=update, checked=now().strftime("%H:%M"))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/users")
@login_required
def users():
    info = user_security_info()   # 2FA methods and last sign-in per user, from Authelia's database
    return render_template("users.html", users=load_users(), page="users", sec=info)


# ---------- Authelia's database (read-only): sign-ins, bans, 2FA registrations ----------
def authelia_db():
    """Read-only connection to Authelia's SQLite database, or None if it isn't there or has no tables yet."""
    import sqlite3
    path = db_path()
    if not os.path.isfile(path):
        return None
    try:
        con = sqlite3.connect(f"file:{urllib.parse.quote(path)}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        con.execute("SELECT 1 FROM authentication_logs LIMIT 1")
        return con
    except sqlite3.Error:
        return None


# e.g. "2026-11-20 14:34:00.123456789+02:00", "2026-11-20T12:34:00Z", "2026-11-20 12:34:00"
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?")


def db_time(value):
    """Authelia's timestamps as an aware datetime in TZ, or None. Authelia writes them with the offset of
    its own time zone (its container's TZ); a value without an offset (SQLite's CURRENT_TIMESTAMP) is UTC."""
    if not value:
        return None
    m = _TS_RE.match(str(value))
    if not m:
        return None
    dt = datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")
    off = m.group(3)
    if off and off != "Z":
        sign = 1 if off[0] == "+" else -1
        digits = off[1:].replace(":", "")
        tz = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4])))
    else:
        tz = timezone.utc
    return dt.replace(tzinfo=tz).astimezone(TZINFO)


def fmt_time(dt):
    if not dt:
        return ""
    today = now().date()
    if dt.date() == today:
        return dt.strftime("Today %H:%M:%S")
    return dt.strftime("%Y-%m-%d %H:%M")


app.jinja_env.filters["when"] = fmt_time


def fmt_short(dt):
    """14:44 for today, 18 Nov 14:44 otherwise."""
    if not dt:
        return ""
    return dt.strftime("%H:%M") if dt.date() == now().date() else dt.strftime("%d %b %H:%M")


app.jinja_env.filters["short"] = fmt_short


def active_bans(con):
    """Bans that are in force now: not revoked, not expired, and either permanent or not yet past `expires`."""
    out = []
    cur = now()
    for kind, table, col in (("user", "banned_user", "username"), ("ip", "banned_ip", "ip")):
        try:
            rows = con.execute(f"SELECT id, time, expires, expired, revoked, {col} AS target, source, reason "
                               f"FROM {table} WHERE revoked = 0 AND expired IS NULL ORDER BY time DESC").fetchall()
        except Exception:
            continue   # older Authelia without ban tables
        for r in rows:
            until = db_time(r["expires"])
            if until and until <= cur:
                continue
            out.append({"kind": kind, "id": r["id"], "target": r["target"], "since": db_time(r["time"]),
                        "until": until, "source": r["source"], "reason": r["reason"] or ""})
    return out


def user_security_info():
    """{username: {"totp": bool, "passkeys": n, "last": datetime|None, "banned": bool}}; None if no database."""
    con = authelia_db()
    if not con:
        return None
    info = {}
    def get(u):
        return info.setdefault(u, {"totp": False, "passkeys": 0, "last": None, "banned": False})
    try:
        for r in con.execute("SELECT username FROM totp_configurations"):
            get(r["username"])["totp"] = True
        for r in con.execute("SELECT username, COUNT(*) AS n FROM webauthn_credentials GROUP BY username"):
            get(r["username"])["passkeys"] = r["n"]
        for r in con.execute("SELECT username, MAX(time) AS t FROM authentication_logs WHERE successful = 1 "
                             "GROUP BY username"):
            get(r["username"])["last"] = db_time(r["t"])
        for b in active_bans(con):
            if b["kind"] == "user":
                get(b["target"])["banned"] = True
    except Exception:
        app.logger.exception("Reading Authelia's database failed")
    finally:
        con.close()
    return info


@app.route("/activity")
@login_required
def activity():
    audit = read_audit(ACTIVITY_MAX_ROWS + 1, days=ACTIVITY_DAYS)
    common = {"page": "activity", "known": set(load_users()), "days": ACTIVITY_DAYS, "cap": ACTIVITY_MAX_ROWS,
              "audit": audit[:ACTIVITY_MAX_ROWS], "audit_capped": len(audit) > ACTIVITY_MAX_ROWS}
    con = authelia_db()
    if not con:
        return render_template("activity.html", nodb=True, db=db_path(), **common)
    try:
        bans = active_bans(con)
        cutoff = now() - timedelta(days=ACTIVITY_DAYS)
        since = now() - timedelta(hours=24)
        logs, ok24, fail24, capped = [], 0, 0, False
        # Newest first. Old entries are skipped rather than ending the scan, so the list doesn't rely on rows
        # being stored in time order; the scan itself is bounded.
        cur = con.execute("SELECT time, successful, banned, username, auth_type, remote_ip FROM authentication_logs "
                          "ORDER BY id DESC LIMIT 50000")
        for r in cur:
            t = db_time(r["time"])
            if t and t < cutoff:
                continue
            if len(logs) >= ACTIVITY_MAX_ROWS:
                capped = True
                break
            logs.append({"time": t, "ok": bool(r["successful"]), "banned": bool(r["banned"]),
                         "user": r["username"], "method": r["auth_type"], "ip": r["remote_ip"] or ""})
            if t and t >= since:
                if r["successful"]:
                    ok24 += 1
                else:
                    fail24 += 1
    finally:
        con.close()
    return render_template("activity.html", bans=bans, logs=logs, ok24=ok24, fail24=fail24, logs_capped=capped,
                           **common)


@app.route("/activity/unban", methods=["POST"])
@login_required
def unban():
    kind = request.form.get("kind")
    ban_id = request.form.get("id", "")
    target = request.form.get("target", "")
    if kind not in ("user", "ip") or not ban_id.isdigit():
        abort(400)
    if not AUTHELIA_BIN:
        flash("authelia binary not available in this image.", "error")
        return redirect(url_for("activity"))
    r = subprocess.run([AUTHELIA_BIN, "storage", "bans", kind, "revoke", "--id", ban_id, "--config", CONFIG_FILE],
                       capture_output=True, text=True, timeout=30, env=CLI_ENV)
    if r.returncode == 0:
        audit("Unbanned " + ("user" if kind == "user" else "IP"), target)
        flash(f"Unbanned {'user' if kind == 'user' else 'IP'} {target}. They can sign in again.", "ok")
    else:
        flash(f"Unban failed: {cli_message(r.stdout + r.stderr)}", "error")
    back = request.form.get("back", "")
    if not back.startswith("/") or back.startswith("//"):
        back = url_for("activity")
    return redirect(back)


@app.route("/users/new", methods=["GET", "POST"])
@login_required
def user_new():
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        pw = request.form.get("password", "")
        all_users = load_users()
        err = None
        if not USERNAME_RE.match(name):
            err = "Username may contain letters, digits, . _ - (max 64)."
        elif name in all_users:
            err = "User already exists."
        else:
            err = validate_password(pw)
        if err:
            flash(err, "error")
        else:
            all_users[name] = {
                "disabled": False,
                "displayname": request.form.get("displayname", "").strip() or name,
                "password": ph.hash(pw),
                "email": request.form.get("email", "").strip(),
                "groups": parse_groups(request.form.get("groups")),
            }
            save_users(all_users)
            audit("User created", name, "groups: " + (", ".join(all_users[name]["groups"]) or "none"))
            flash(f"User '{name}' created.", "ok")
            return redirect(url_for("users"))
    return render_template("user_form.html", user=None, name="", page="users")


@app.route("/users/<name>", methods=["GET", "POST"])
@login_required
def user_edit(name):
    all_users = load_users()
    if name not in all_users:
        abort(404)
    u = all_users[name]
    if request.method == "POST":
        action = request.form.get("action")
        if action == "delete":
            del all_users[name]
            save_users(all_users)
            audit("User deleted", name)
            flash(f"User '{name}' deleted.", "ok")
            return redirect(url_for("users"))
        if action == "password":
            pw = request.form.get("password", "")
            err = validate_password(pw)
            if err:   # stay on the user's page to try again
                flash(err, "error")
                return redirect(url_for("user_edit", name=name))
            u["password"] = ph.hash(pw)
            save_users(all_users)
            audit("Password set", name)
            flash(f"Password for '{name}' updated.", "ok")
            return redirect(url_for("users"))
        before = {k: u.get(k) for k in ("displayname", "email", "groups", "disabled")}
        u["displayname"] = request.form.get("displayname", "").strip() or name
        u["email"] = request.form.get("email", "").strip()
        u["groups"] = parse_groups(request.form.get("groups"))
        u["disabled"] = request.form.get("disabled") == "on"
        save_users(all_users)
        changes = []
        for k, label in (("displayname", "name"), ("email", "email"), ("groups", "groups"), ("disabled", "disabled")):
            if before[k] != u[k]:
                old, new = before[k], u[k]
                if k == "groups":
                    old, new = ", ".join(old or []) or "none", ", ".join(new or []) or "none"
                changes.append(f"{label}: {old} → {new}")
        if changes:
            audit("User updated", name, "; ".join(changes))
        flash(f"User '{name}' updated." if changes else f"No changes to '{name}'.", "ok")
        return redirect(url_for("users"))
    return render_template("user_form.html", user=u, name=name, page="users")


@app.route("/users/<name>/reset2fa", methods=["POST"])
@login_required
def user_reset_2fa(name):
    """Remove a user's TOTP and WebAuthn registrations from Authelia's SQLite storage via the CLI."""
    if name not in load_users():
        abort(404)
    if not AUTHELIA_BIN:
        flash("authelia binary not available in this image.", "error")
        return redirect(url_for("user_edit", name=name))
    cmds = [("TOTP", ["storage", "user", "totp", "delete", name]),
            ("WebAuthn", ["storage", "user", "webauthn", "delete", name, "--all"])]
    msgs, failed = [], False
    for label, c in cmds:
        r = subprocess.run([AUTHELIA_BIN, *c, "--config", CONFIG_FILE],
                           capture_output=True, text=True, timeout=30, env=CLI_ENV)
        out = cli_message(r.stdout + r.stderr)
        low = out.lower()
        if "schema outdated" in low and "version 0" in low:
            # Same cause for both commands: report it once, in plain words.
            flash("2FA reset for '%s' failed: Authelia's database has no tables yet. Authelia creates them the "
                  "first time it starts with this configuration; start Authelia once, then try again." % name, "error")
            return redirect(url_for("user_edit", name=name))
        # "nothing registered" is not a failure
        nothing = r.returncode != 0 and bool(re.search(r"not found|no rows|no (totp|webauthn)", low))
        bad = r.returncode != 0 and not nothing
        failed = failed or bad
        msgs.append(f"{label}: " + (out if bad else "nothing registered" if nothing else "removed"))
    audit("2FA reset" + (" (partly failed)" if failed else ""), name, " | ".join(msgs))
    flash("2FA reset for '%s'. %s" % (name, " | ".join(msgs)), "error" if failed else "ok")
    return redirect(url_for("user_edit", name=name))


def cli_message(text):
    """Authelia CLI output without colour codes and without the usage/help text it prints after an error."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text or "")
    text = re.split(r"\n\s*Usage:", text, maxsplit=1)[0].strip()
    return re.sub(r"^Error:\s*", "", text)


# ---------- configuration ----------
AUTHELIA_BIN = shutil.which("authelia")
# Authelia treats every AUTHELIA_* env var as a config key and warns about unknown ones,
# so hide this app's own settings from the CLI (real keys like AUTHELIA_STORAGE_* pass through).
_OWN_ENV = {"AUTHELIA_CONFIG_DIR", "AUTHELIA_CONFIG_FILE", "AUTHELIA_USERS_FILE",
            "AUTHELIA_CONTAINER", "AUTHELIA_DB_FILE", "AUTHELIA_URL"}
CLI_ENV = {k: v for k, v in os.environ.items() if k not in _OWN_ENV}


def authelia_validate(text):
    """Run `authelia validate-config` on text. Returns (ok, output) or (None, msg) if unavailable."""
    if not AUTHELIA_BIN:
        return None, "authelia binary not found in this image"
    fd, tmp = tempfile.mkstemp(suffix=".yml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        r = subprocess.run([AUTHELIA_BIN, "validate-config", "--config", tmp],
                           capture_output=True, text=True, timeout=30, env=CLI_ENV)
        out = re.sub(r"\x1b\[[0-9;]*m", "", (r.stdout + r.stderr))
        # drop the usage/help block the CLI prints after "Error: configuration validation failed"
        out = re.sub(r"\n\s*Usage:.*?(?=\n\s*Configuration |\Z)", "\n", out, flags=re.S).strip()
        return r.returncode == 0, out
    except Exception as e:
        return None, str(e)
    finally:
        os.path.exists(tmp) and os.remove(tmp)


def read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def make_diff(old, new, old_name, new_name):
    """Unified diff as [(css_class, line)] for colouring in the templates; [] when identical."""
    out = []
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), old_name, new_name,
                                     lineterm="", n=3):
        if line.startswith(("+++", "---")):
            cls = "hdr"
        elif line.startswith("@@"):
            cls = "hunk"
        elif line.startswith("+"):
            cls = "add"
        elif line.startswith("-"):
            cls = "del"
        else:
            cls = ""
        out.append((cls, line))
    return out


@app.route("/config", methods=["GET", "POST"])
@login_required
def config():
    disk = read_text(CONFIG_FILE)
    disk_fp = fingerprint(disk)
    if request.method == "POST":
        text = request.form.get("content", "").replace("\r\n", "\n")
        action = request.form.get("action", "save")
        # Fingerprint of the file as it was when the page was first opened; kept across Validate/Show changes.
        base = request.form.get("base") or disk_fp
        overwrote = action == "overwrite"
        if overwrote:   # the user chose to replace a file that changed elsewhere
            base, action = disk_fp, "save"
        from_backup = os.path.basename(request.form.get("from_backup", ""))

        def page(**kw):
            return render_template("config.html", content=text, page="config", base=base, disk=disk,
                                   from_backup=from_backup, **kw)

        if action == "diff":
            d = make_diff(disk, text, "current", "edited")
            if not d:
                flash("No changes compared to the current file.", "ok")
            return page(diff=d)
        if action == "save" and base != disk_fp:
            flash("Not saved: configuration.yml was changed outside this page since you opened it. "
                  "Review the difference below, then load the current file or overwrite it.", "error")
            return page(conflict=True, diff=make_diff(disk, text, "on disk now", "your edits"))
        try:
            parsed = yaml.safe_load(text)
            if not isinstance(parsed, dict):
                raise ValueError("Top level of configuration must be a mapping.")
        except Exception as e:
            flash(f"Invalid YAML, not saved: {e}", "error")
            return page()
        ok, out = authelia_validate(text)
        if action == "validate":
            if ok is None:
                flash(f"Validation unavailable: {out}", "error")
            else:
                flash("Authelia validation passed." if ok else "Authelia validation failed.", "ok" if ok else "error")
            return page(vout=out)
        if ok is False and request.form.get("force") != "on":
            flash("Authelia validation failed, not saved. Fix the errors or tick 'Save anyway'.", "error")
            return page(vout=out, failed=True)
        bk = atomic_write(CONFIG_FILE, text)
        delta = [l for l in difflib.unified_diff(disk.splitlines(), text.splitlines(), lineterm="", n=0)
                 if not l.startswith(("---", "+++", "@@"))]
        added = sum(1 for l in delta if l.startswith("+"))
        removed = sum(1 for l in delta if l.startswith("-"))
        notes = [f"+{added} −{removed} lines"]
        if from_backup:
            notes.append("from backup " + from_backup)
        if overwrote:
            notes.append("overwrote a change made outside the editor")
        if ok is False:
            notes.append("saved although Authelia's validation failed")
        if bk:
            notes.append("previous version backed up as " + os.path.basename(bk))
        audit("Configuration saved", os.path.basename(CONFIG_FILE), "; ".join(notes))
        flash("Configuration saved (backup created). Restart Authelia to apply.", "ok")
        return redirect(url_for("config", saved=1))

    # "Open in editor" from a configuration backup: the backup's text as unsaved edits of the current file
    from_backup = request.args.get("from_backup", "")
    if from_backup:
        path = os.path.realpath(os.path.join(BACKUP_DIR, os.path.basename(from_backup)))
        if (os.path.dirname(path) != os.path.realpath(BACKUP_DIR) or not os.path.isfile(path)
                or not os.path.basename(path).startswith(os.path.basename(CONFIG_FILE) + ".")):
            abort(404)
        flash(f"Loaded {os.path.basename(path)} into the editor. It isn't saved yet: review the changes, "
              "then Validate & save to apply it.", "ok")
        return render_template("config.html", content=read_text(path), page="config", base=disk_fp, disk=disk,
                               from_backup=os.path.basename(path))
    return render_template("config.html", content=disk, page="config", saved=bool(request.args.get("saved")),
                           ondisk=True, base=disk_fp)


def fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@app.route("/config/fingerprint")
@login_required
def config_fingerprint():
    """Polled by the editor to notice changes made elsewhere (e.g. over SSH) while the page is open."""
    resp = jsonify(fp=fingerprint(read_text(CONFIG_FILE)))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/restart", methods=["POST"])
@login_required
def restart():
    if not (AUTHELIA_CONTAINER or RESTART_FLAG_FILE):
        abort(404)
    # Back to the page the button was on, without ?saved=1 (so the restart prompt doesn't reappear).
    # ?restarting=<mode> makes the page show a live status card (see /restart/status and app.js).
    ref = urllib.parse.urlparse(request.referrer or "").path or url_for("config")
    if not ref.startswith("/") or ref.startswith("//"):
        ref = url_for("config")
    if RESTART_FLAG_FILE:
        try:
            with open(RESTART_FLAG_FILE, "w") as fh:
                fh.write(now().isoformat() + "\n")
        except Exception as e:
            flash(f"Couldn't create the restart request file: {e}", "error")
            return redirect(ref)
        audit("Restart requested", "Authelia", "flag file")
        return redirect(ref + "?restarting=flag")
    try:
        import docker
        docker.from_env().containers.get(AUTHELIA_CONTAINER).restart(timeout=10)
    except Exception as e:
        if "No such file" in str(e) or "Permission denied" in str(e):
            flash("Restart failed: can't use the Docker socket. Mount /var/run/docker.sock into this "
                  "container and give its user access to it (group_add with the docker group id).", "error")
        else:
            flash(f"Restart failed: {e}", "error")
        return redirect(ref)
    audit("Restarted", AUTHELIA_CONTAINER, "Docker socket")
    return redirect(ref + "?restarting=socket")


def authelia_health():
    """'up' / 'down' from Authelia's /api/health, or None when AUTHELIA_URL is not set."""
    if not AUTHELIA_URL:
        return None
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    def probe():
        with urllib.request.urlopen(AUTHELIA_URL + "/api/health", timeout=2) as r:
            return "up" if r.status == 200 else "down"
    # A hard limit: while Authelia's container is stopped, resolving its name can hang longer than
    # urlopen's timeout (DNS lookups aren't covered by it).
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(probe).result(timeout=2.5)
    except (FutureTimeout, Exception):
        return "down"
    finally:
        pool.shutdown(wait=False)


@app.route("/restart/status")
@login_required
def restart_status():
    """Polled by the page after a restart. flag: pending (host service hasn't picked it up) / done."""
    flag = None
    if RESTART_FLAG_FILE:
        flag = "pending" if os.path.exists(RESTART_FLAG_FILE) else "done"
    health = None if flag == "pending" else authelia_health()   # not needed until the host picked it up
    resp = jsonify(flag=flag, authelia=health, container=AUTHELIA_CONTAINER or "authelia")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/restart/withdraw", methods=["POST"])
@login_required
def restart_withdraw():
    """Remove a restart request the host service didn't pick up, so it can't restart Authelia later by
    surprise (for example when the service is fixed hours afterwards)."""
    if not RESTART_FLAG_FILE:
        return jsonify(ok=False, error="Flag-file restart isn't configured."), 404
    try:
        os.remove(RESTART_FLAG_FILE)
        removed = True
    except FileNotFoundError:
        removed = False   # picked up in the meantime
    if removed:
        audit("Restart request withdrawn", "Authelia", "not picked up by the host service")
    return jsonify(ok=True, withdrawn=removed)


# ---------- backups ----------
def db_path():
    """SQLite file: storage.local.path from configuration.yml, else AUTHELIA_DB_FILE, else /config/db.sqlite3."""
    p = None
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            p = ((yaml.safe_load(fh) or {}).get("storage") or {}).get("local", {}).get("path")
    except Exception:
        pass
    return p or os.environ.get("AUTHELIA_DB_FILE") or os.path.join(CONFIG_DIR, "db.sqlite3")


def is_db_backup(name):
    return name.startswith(os.path.basename(db_path()) + ".")


BACKUP_GROUPS = [("config", "Configuration", "configuration.yml backups"),
                 ("users", "Users", "users_database.yml backups"),
                 ("db", "Database", "SQLite database backups (download only)"),
                 ("other", "Other", "Unrecognised files in the backups folder")]


def backup_category(name):
    if is_db_backup(name):
        return "db"
    base = name.rsplit(".", 1)[0]
    if base == os.path.basename(CONFIG_FILE):
        return "config"
    if base == os.path.basename(USERS_FILE):
        return "users"
    return "other"


@app.route("/backups")
@login_required
def backups():
    files = sorted((f for f in glob.glob(os.path.join(BACKUP_DIR, "*")) if os.path.isfile(f)), reverse=True)
    by_cat = {key: [] for key, _, _ in BACKUP_GROUPS}
    for f in files:
        n = os.path.basename(f)
        by_cat[backup_category(n)].append({"name": n, "size": os.path.getsize(f), "db": is_db_backup(n)})
    groups = [{"key": k, "title": t, "hint": h, "items": by_cat[k]}
              for k, t, h in BACKUP_GROUPS if by_cat[k] or k != "other"]
    return render_template("backups.html", groups=groups, page="backups", db_file=db_path(),
                           db_exists=os.path.isfile(db_path()), db_auto=DB_BACKUP_AT, db_keep=DB_BACKUP_KEEP,
                           db_next=next_db_backup(), db_last=latest_db_backup_time(), db_state=db_backup_state)


@app.route("/backups/new-db", methods=["POST"])
@login_required
def backup_db():
    src = db_path()
    if not os.path.isfile(src):
        flash(f"Database not found at {src}. Check storage.local.path and the /config mount.", "error")
        return redirect(url_for("backups"))
    try:
        dest, method = make_db_backup()
    except Exception as e:
        flash(f"Database backup failed: {e}", "error")
        return redirect(url_for("backups"))
    audit("Database backed up", os.path.basename(dest), method)
    flash(f"Database backed up as {os.path.basename(dest)} ({method}).", "ok")
    return redirect(url_for("backups"))


def make_db_backup():
    """Back up Authelia's SQLite database into the backups folder; keeps the newest DB_BACKUP_KEEP.
    Returns (path, method). Raises on failure (nothing half-written is left behind)."""
    src = db_path()
    if not os.path.isfile(src):
        raise FileNotFoundError(f"database not found at {src}")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    base = os.path.basename(src)
    dest = backup_name(base)
    try:
        try:
            import sqlite3
            s = sqlite3.connect(src, timeout=30)
            d = sqlite3.connect(dest)
            try:
                s.backup(d)  # online backup: consistent even while Authelia is running
            finally:
                d.close()
                s.close()
            method = "SQLite online backup"
        except ImportError:
            shutil.copy2(src, dest)
            method = "file copy (sqlite3 module unavailable; stop Authelia first for a guaranteed-consistent copy)"
        os.chmod(dest, 0o600)  # contains 2FA secrets
    except Exception:
        if os.path.exists(dest):
            os.remove(dest)
        raise
    for old in sorted(glob.glob(os.path.join(BACKUP_DIR, base + ".*")))[:-DB_BACKUP_KEEP]:
        os.remove(old)
    return dest, method


# ---- automatic daily database backup ----
# DB_BACKUP_TIME: HH:MM in TZ (default 03:00), or "off". DB_BACKUP_KEEP: database backups kept (default 7).
_dbt = os.environ.get("DB_BACKUP_TIME", "03:00").strip().lower()
_m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", _dbt)
DB_BACKUP_AT = (int(_m.group(1)), int(_m.group(2))) if _m else None
if _dbt not in ("off", "false", "") and not _m:
    print(f"WARNING: invalid DB_BACKUP_TIME {_dbt!r} (use HH:MM or off); automatic database backups are off",
          flush=True)
try:
    DB_BACKUP_KEEP = max(1, int(os.environ.get("DB_BACKUP_KEEP", "7")))
except ValueError:
    DB_BACKUP_KEEP = 7
db_backup_state = {"last": None, "error": None}   # last automatic run (datetime) / last error message


def latest_db_backup_time():
    base = os.path.basename(db_path())
    times = [backup_time(os.path.basename(f)) for f in glob.glob(os.path.join(BACKUP_DIR, base + ".*"))]
    times = [t for t in times if t]
    return max(times) if times else None


def next_db_backup():
    if not DB_BACKUP_AT:
        return None
    n = now()
    t = n.replace(hour=DB_BACKUP_AT[0], minute=DB_BACKUP_AT[1], second=0, microsecond=0)
    return t if t > n else t + timedelta(days=1)


def _run_db_backup():
    if not os.path.isfile(db_path()):
        return   # Authelia hasn't created its database (yet)
    try:
        dest, method = make_db_backup()
        db_backup_state.update(last=now(), error=None)
        audit("Database backed up (automatic)", os.path.basename(dest), method)
    except Exception as e:
        db_backup_state.update(last=now(), error=str(e))
        app.logger.exception("Automatic database backup failed")
        audit("Database backup failed (automatic)", os.path.basename(db_path()), str(e))


def _db_backup_loop():
    time.sleep(120)   # let the app start first
    # catch up after downtime: the last database backup is more than a day old
    last = latest_db_backup_time()
    if last is None or now() - last > timedelta(hours=25):
        _run_db_backup()
    while True:
        target = next_db_backup()
        while now() < target:   # short steps, so clock changes or a suspended host don't throw it off
            time.sleep(min(60.0, max(1.0, (target - now()).total_seconds())))
        _run_db_backup()
        time.sleep(61)   # never twice for the same minute


if DB_BACKUP_AT:
    threading.Thread(target=_db_backup_loop, name="db-backup", daemon=True).start()


@app.route("/backups/<name>/download")
@login_required
def backup_download(name):
    path = os.path.realpath(os.path.join(BACKUP_DIR, name))
    if os.path.dirname(path) != os.path.realpath(BACKUP_DIR) or not os.path.isfile(path):
        abort(404)
    return send_from_directory(BACKUP_DIR, name, as_attachment=True)


@app.route("/backups/<name>", methods=["GET", "POST"])
@login_required
def backup_view(name):
    path = os.path.realpath(os.path.join(BACKUP_DIR, name))
    if os.path.dirname(path) != os.path.realpath(BACKUP_DIR) or not os.path.isfile(path):
        abort(404)
    if is_db_backup(name):  # binary file: download only, restore is manual
        return redirect(url_for("backups"))
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    base = name.rsplit(".", 1)[0]
    target = CONFIG_FILE if base == os.path.basename(CONFIG_FILE) else USERS_FILE if base == os.path.basename(USERS_FILE) else None
    if request.method == "POST":
        if not target:
            abort(400)
        atomic_write(target, content)
        audit("Backup restored", base, "from " + name)
        flash(f"Restored {base} from {name}.", "ok")
        return redirect(url_for("backups"))
    # What a restore would change: current file -> this backup.
    diff = make_diff(read_text(target), content, "current", name) if target else []
    return render_template("backup_view.html", name=name, content=content, page="backups", diff=diff,
                           is_config=target == CONFIG_FILE)


@app.route("/backups/<name>/delete", methods=["POST"])
@login_required
def backup_delete(name):
    path = os.path.realpath(os.path.join(BACKUP_DIR, name))
    if os.path.dirname(path) != os.path.realpath(BACKUP_DIR) or not os.path.isfile(path):
        abort(404)
    os.remove(path)
    audit("Backup deleted", name)
    flash(f"Backup {name} deleted.", "ok")
    return redirect(url_for("backups"))


@app.errorhandler(OSError)
def file_error(e):
    """A file couldn't be read or written (usually folder permissions): explain it instead of a bare 500.
    Nothing is half-written: backups are taken first and files are replaced atomically."""
    app.logger.exception("File error on %s %s", request.method, request.path)
    where = getattr(e, "filename", None) or "a file"
    reason = e.strerror or str(e)
    hint = ""
    if isinstance(e, PermissionError):
        hint = (" Check that the container's user (%d:%d) can write the Authelia folder and its backups "
                "folder, for example with: sudo chown -R %d:%d <your Authelia folder>"
                % (os.getuid(), os.getgid(), os.getuid(), os.getgid()))
    msg = f"Nothing was changed: couldn't access {where} ({reason}).{hint}"
    if request.is_json or request.headers.get("X-CSRF-Token"):
        return jsonify(ok=False, error=msg), 500
    if request.method == "GET":   # a page couldn't load: show the reason (a redirect could loop)
        return render_template("error.html", message=msg, page=None), 500
    flash(msg, "error")
    # back to the page the action came from (a config edit is kept in the browser and offered for restore)
    back = urllib.parse.urlparse(request.referrer or "").path
    if not back.startswith("/") or back.startswith("//"):
        back = url_for("users")
    return redirect(back)


@app.after_request
def headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'"
    return resp
