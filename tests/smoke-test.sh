#!/usr/bin/env bash
# Smoke test for an Authelia Admin image: starts it with the sample data in tests/fixtures, signs in, opens
# every page and tries the main actions. Exits non-zero if anything fails, so CI publishes only images
# that pass (see .github/workflows/docker.yml).
#
#   tests/smoke-test.sh <image>          e.g. tests/smoke-test.sh authelia-admin:test
#
# Needs docker, curl and openssl. Works on Linux and in Git Bash on Windows (Docker Desktop).
set -uo pipefail

IMAGE="${1:?usage: tests/smoke-test.sh <image>}"
PORT="${PORT:-18099}"
NAME="aa-smoke-$$"
HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="$(mktemp -d)"          # mounted as /config
WORK="$(mktemp -d)"          # cookie jar and responses (not mounted)
B="http://127.0.0.1:${PORT}"
JAR="$WORK/cookies"
FAILED=0
PASSED=0

cp "$HERE"/fixtures/* "$DATA"/
chmod -R a+rwX "$DATA"
ADMIN_PW="smoke-$(openssl rand -hex 8)"

cleanup() {
  if [ "$FAILED" -gt 0 ]; then
    echo "---- container log (last 40 lines) ----"
    docker logs "$NAME" 2>&1 | tail -40
  fi
  docker rm -f "$NAME" >/dev/null 2>&1
  rm -rf "$DATA" "$WORK"
}
trap cleanup EXIT

ok()   { PASSED=$((PASSED + 1)); echo "  ok    $1"; }
fail() { FAILED=$((FAILED + 1)); echo "  FAIL  $1"; }
check() { local name="$1"; shift; if "$@"; then ok "$name"; else fail "$name"; fi; }

# On Git Bash, Docker Desktop needs a Windows path for the bind mount
MOUNT="$DATA"
if command -v cygpath >/dev/null 2>&1; then MOUNT="$(cygpath -w "$DATA")"; fi

echo "Starting $IMAGE"
MSYS_NO_PATHCONV=1 docker run -d --name "$NAME" -p "127.0.0.1:${PORT}:8080" \
  -e ADMIN_USER=admin -e ADMIN_PASSWORD="$ADMIN_PW" -e SECRET_KEY="$(openssl rand -hex 32)" \
  -e TZ=Asia/Jerusalem -v "$MOUNT:/config" "$IMAGE" >/dev/null || { echo "docker run failed"; exit 1; }

up=0
for _ in $(seq 1 40); do
  if curl -fs "$B/healthz" >/dev/null 2>&1; then up=1; break; fi
  sleep 0.5
done
check "container starts and /healthz answers" [ "$up" = 1 ]
[ "$up" = 1 ] || exit 1

# helpers: GET/POST with the session cookie; the last response body is in $WORK/body, final URL in $WORK/url
get()  { curl -s -L -b "$JAR" -c "$JAR" -o "$WORK/body" -w '%{url_effective} %{http_code}' "$B$1" > "$WORK/url"; }
post() { local path="$1"; shift; curl -s -L -b "$JAR" -c "$JAR" -o "$WORK/body" -w '%{url_effective} %{http_code}' "$@" "$B$path" > "$WORK/url"; }
csrf() { grep -o 'name="csrf" value="[^"]*"' "$WORK/body" | head -1 | sed 's/.*value="//; s/"$//'; }
body_has() { grep -q -- "$1" "$WORK/body"; }
landed_on() { [ "$(awk '{print $1}' "$WORK/url" | sed -E 's#^https?://[^/]+##')" = "$1" ]; }   # exact path (and query)
status_is() { awk '{print $2}' "$WORK/url" | grep -qx -- "$1"; }

echo "Sign-in"
get /login
T="$(csrf)"
post /login --data-urlencode "csrf=$T" --data-urlencode "username=admin" --data-urlencode "password=wrong-password"
check "wrong password is refused" body_has "Invalid credentials"
get /login
T="$(csrf)"
post /login --data-urlencode "csrf=$T" --data-urlencode "username=admin" --data-urlencode "password=$ADMIN_PW"
check "sign-in lands on the dashboard" body_has "<h1>Dashboard</h1>"

echo "Pages"
for page in "/users|Users" "/users/alice|Edit alice" "/config|configuration.yml" "/activity|Admin actions" \
            "/backups|Backups" "/security|Security"; do
  path="${page%%|*}"; marker="${page#*|}"
  get "$path"
  check "$path opens" bash -c "awk '{print \$2}' '$WORK/url' | grep -qx 200 && grep -q -- '$marker' '$WORK/body'"
done

echo "Editor files"
check "editor bundle is served" bash -c "[ \$(curl -s -b '$JAR' '$B/static/editor.js' | wc -c) -gt 100000 ]"
check "Authelia schema is served" bash -c "curl -s -b '$JAR' '$B/static/config-schema.json' | grep -q '\$defs'"

echo "Checks against the built-in Authelia CLI"
CFG="$(cat "$DATA/configuration.yml")"
get /dashboard/status
check "dashboard status: configuration valid" body_has '"config_ok":true'
get /config
T="$(csrf)"
BASE="$(grep -o 'name="base" value="[^"]*"' "$WORK/body" | sed 's/.*value="//; s/"$//')"
post /config --data-urlencode "csrf=$T" --data-urlencode "action=validate" --data-urlencode "base=$BASE" \
     --data-urlencode "content=$CFG"
check "Validate only passes for the sample configuration" body_has "Authelia validation passed"

echo "Changes"
post /config --data-urlencode "csrf=$T" --data-urlencode "action=save" --data-urlencode "base=$BASE" \
     --data-urlencode "content=$CFG"
check "saving the configuration works" landed_on "/config?saved=1"
check "a configuration backup was taken" bash -c "ls '$DATA'/backups/configuration.yml.* >/dev/null 2>&1"
get /users/alice
T="$(csrf)"
post /users/alice --data-urlencode "csrf=$T" --data-urlencode "displayname=Alice Changed" \
     --data-urlencode "email=alice@example.com" --data-urlencode "groups=admins"
check "saving a user returns to the Users list" landed_on "/users"
check "the user change is in users_database.yml" grep -q "Alice Changed" "$DATA/users_database.yml"
OLD_HASH="$(grep -A6 '^  bob:' "$DATA/users_database.yml" | grep password)"
get /users/bob
T="$(csrf)"
post /users/bob --data-urlencode "csrf=$T" --data-urlencode "action=password" --data-urlencode "password=New-smoke-pw-123"
NEW_HASH="$(grep -A6 '^  bob:' "$DATA/users_database.yml" | grep password)"
check "setting a password stores a new argon2id hash" bash -c "[ '$OLD_HASH' != '$NEW_HASH' ] && echo '$NEW_HASH' | grep -q 'argon2id'"
check "actions are recorded in the admin log" bash -c "grep -q 'Configuration saved' '$DATA/admin_audit.log' && grep -q 'Password set' '$DATA/admin_audit.log'"

echo "Container"
check "no errors in the container log" bash -c "! docker logs '$NAME' 2>&1 | grep -qE 'Traceback|ERROR in app'"

echo
echo "$PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ]
