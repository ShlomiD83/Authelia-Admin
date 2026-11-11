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
import time
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

import yaml
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask import (Flask, abort, flash, jsonify, make_response, redirect, render_template,
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


@app.before_request
def protect():
    if request.method == "POST":
        sent = request.form.get("csrf", "") or request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(sent, session.get("csrf", "x")):
            abort(400, "Bad CSRF token")


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("auth"):
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper


def backup(path):
    if not os.path.exists(path):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, os.path.join(BACKUP_DIR, f"{os.path.basename(path)}.{stamp}"))
    olds = sorted(glob.glob(os.path.join(BACKUP_DIR, os.path.basename(path) + ".*")))
    for old in olds[:-MAX_BACKUPS]:
        os.remove(old)


def atomic_write(path, text):
    backup(path)
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


def password_login_allowed():
    """Password sign-in is only switched off when passkey-only mode is on AND a passkey exists (no lock-out)."""
    return not (PASSKEY_ONLY and PASSKEYS_ENABLED and load_passkeys())


def login_ctx():
    return dict(show_passkey=PASSKEYS_ENABLED and bool(load_passkeys()), password_ok=password_login_allowed())


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
            session.permanent = True
            return redirect(url_for("users"))
        _failed[ip] = (count + 1, first)
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
            session.permanent = True
            return redirect(url_for("users"))
        _failed[ip] = (count + 1, first)
        flash("That code didn't work. Check the code and your phone's clock.", "error")
    return render_template("login_totp.html")


@app.route("/logout", methods=["POST"])
def logout():
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
        "security.html", page="security", passkeys=load_passkeys(),
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
    save_passkeys(items)
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
    flash("Passkey removed.", "ok")
    return redirect(url_for("security"))


@app.route("/passkeys/login/options", methods=["POST"])
def pk_login_options():
    if not (PASSKEYS_ENABLED and load_passkeys()):
        return _pk_error("Passkey sign-in is not available.", 404)
    opts = generate_authentication_options(rp_id=RP_ID,
                                           user_verification=UserVerificationRequirement.REQUIRED)
    session["pk_auth"] = bytes_to_base64url(opts.challenge)
    return jsonify(options_to_json_dict(opts))


@app.route("/passkeys/login/verify", methods=["POST"])
def pk_login_verify():
    if not (PASSKEYS_ENABLED and load_passkeys()):
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
    pk = next((p for p in items if isinstance(cred, dict) and p.get("id") == cred.get("id")), None)
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
        return _pk_error("Passkey sign-in failed.", 401)
    pk["sign_count"] = verified.new_sign_count
    pk["last_used"] = now().isoformat(timespec="seconds")
    save_passkeys(items)
    _failed.pop(ip, None)
    session.clear()
    session["auth"] = True
    session.permanent = True
    return jsonify(ok=True, next=url_for("users"))


# ---------- users ----------
@app.route("/")
@login_required
def users():
    return render_template("users.html", users=load_users(), page="users")


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
            flash(f"User '{name}' deleted.", "ok")
            return redirect(url_for("users"))
        if action == "password":
            pw = request.form.get("password", "")
            err = validate_password(pw)
            if err:
                flash(err, "error")
            else:
                u["password"] = ph.hash(pw)
                save_users(all_users)
                flash("Password updated.", "ok")
            return redirect(url_for("user_edit", name=name))
        u["displayname"] = request.form.get("displayname", "").strip() or name
        u["email"] = request.form.get("email", "").strip()
        u["groups"] = parse_groups(request.form.get("groups"))
        u["disabled"] = request.form.get("disabled") == "on"
        save_users(all_users)
        flash("User updated.", "ok")
        return redirect(url_for("user_edit", name=name))
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
    cmds = [["storage", "user", "totp", "delete", name],
            ["storage", "user", "webauthn", "delete", name, "--all"]]
    msgs, failed = [], False
    for c in cmds:
        r = subprocess.run([AUTHELIA_BIN, *c, "--config", CONFIG_FILE],
                           capture_output=True, text=True, timeout=30, env=CLI_ENV)
        out = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout + r.stderr).strip()
        msgs.append(f"{' '.join(c[2:4])}: {out or 'ok'}")
        # "no such registration" style errors are fine; surface everything anyway
        failed = failed or (r.returncode != 0 and "not found" not in out.lower() and "no rows" not in out.lower())
    flash("2FA reset for '%s'. %s" % (name, " | ".join(msgs)), "error" if failed else "ok")
    return redirect(url_for("user_edit", name=name))


# ---------- configuration ----------
AUTHELIA_BIN = shutil.which("authelia")
# Authelia treats every AUTHELIA_* env var as a config key and warns about unknown ones,
# so hide this app's own settings from the CLI (real keys like AUTHELIA_STORAGE_* pass through).
_OWN_ENV = {"AUTHELIA_CONFIG_DIR", "AUTHELIA_CONFIG_FILE", "AUTHELIA_USERS_FILE",
            "AUTHELIA_CONTAINER", "AUTHELIA_DB_FILE"}
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
        out = re.sub(r"\x1b\[[0-9;]*m", "", (r.stdout + r.stderr)).strip()
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
    if request.method == "POST":
        text = request.form.get("content", "").replace("\r\n", "\n")
        action = request.form.get("action", "save")
        if action == "diff":
            d = make_diff(read_text(CONFIG_FILE), text, "current", "edited")
            if not d:
                flash("No changes compared to the current file.", "ok")
            return render_template("config.html", content=text, page="config", diff=d)
        try:
            parsed = yaml.safe_load(text)
            if not isinstance(parsed, dict):
                raise ValueError("Top level of configuration must be a mapping.")
        except Exception as e:
            flash(f"Invalid YAML, not saved: {e}", "error")
            return render_template("config.html", content=text, page="config")
        ok, out = authelia_validate(text)
        if action == "validate":
            if ok is None:
                flash(f"Validation unavailable: {out}", "error")
            else:
                flash("Authelia validation passed." if ok else "Authelia validation failed.", "ok" if ok else "error")
            return render_template("config.html", content=text, page="config", vout=out)
        if ok is False and request.form.get("force") != "on":
            flash("Authelia validation failed, not saved. Fix the errors or tick 'Save anyway'.", "error")
            return render_template("config.html", content=text, page="config", vout=out, failed=True)
        atomic_write(CONFIG_FILE, text)
        flash("Configuration saved (backup created). Restart Authelia to apply.", "ok")
        return redirect(url_for("config", saved=1))
    content = ""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            content = fh.read()
    return render_template("config.html", content=content, page="config", saved=bool(request.args.get("saved")))


@app.route("/restart", methods=["POST"])
@login_required
def restart():
    if not (AUTHELIA_CONTAINER or RESTART_FLAG_FILE):
        abort(404)
    ref = request.referrer or url_for("config")
    if RESTART_FLAG_FILE:
        try:
            with open(RESTART_FLAG_FILE, "w") as fh:
                fh.write(now().isoformat() + "\n")
            flash("Restart requested. Authelia restarts within a few seconds.", "ok")
        except Exception as e:
            flash(f"Couldn't create the restart request file: {e}", "error")
        return redirect(ref.split("?")[0])
    try:
        import docker
        docker.from_env().containers.get(AUTHELIA_CONTAINER).restart(timeout=10)
        flash(f"Container '{AUTHELIA_CONTAINER}' restarted.", "ok")
    except Exception as e:
        if "No such file" in str(e) or "Permission denied" in str(e):
            flash("Restart failed: can't use the Docker socket. Mount /var/run/docker.sock into this "
                  "container and give its user access to it (group_add with the docker group id).", "error")
        else:
            flash(f"Restart failed: {e}", "error")
    return redirect(ref.split("?")[0])  # drop ?saved=1 so the restart prompt doesn't reappear


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
                           db_exists=os.path.isfile(db_path()))


@app.route("/backups/new-db", methods=["POST"])
@login_required
def backup_db():
    src = db_path()
    if not os.path.isfile(src):
        flash(f"Database not found at {src}. Check storage.local.path and the /config mount.", "error")
        return redirect(url_for("backups"))
    os.makedirs(BACKUP_DIR, exist_ok=True)
    base = os.path.basename(src)
    dest = os.path.join(BACKUP_DIR, f"{base}.{now().strftime('%Y%m%d-%H%M%S')}")
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
    except Exception as e:
        if os.path.exists(dest):
            os.remove(dest)
        flash(f"Database backup failed: {e}", "error")
        return redirect(url_for("backups"))
    for old in sorted(glob.glob(os.path.join(BACKUP_DIR, base + ".*")))[:-MAX_BACKUPS]:
        os.remove(old)
    flash(f"Database backed up as {os.path.basename(dest)} ({method}).", "ok")
    return redirect(url_for("backups"))


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
        flash(f"Restored {base} from {name}.", "ok")
        return redirect(url_for("backups"))
    # What a restore would change: current file -> this backup.
    diff = make_diff(read_text(target), content, "current", name) if target else []
    return render_template("backup_view.html", name=name, content=content, page="backups", diff=diff)


@app.route("/backups/<name>/delete", methods=["POST"])
@login_required
def backup_delete(name):
    path = os.path.realpath(os.path.join(BACKUP_DIR, name))
    if os.path.dirname(path) != os.path.realpath(BACKUP_DIR) or not os.path.isfile(path):
        abort(404)
    os.remove(path)
    flash(f"Backup {name} deleted.", "ok")
    return redirect(url_for("backups"))


@app.after_request
def headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'"
    return resp
