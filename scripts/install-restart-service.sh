#!/usr/bin/env bash
# Sets up the host side of the "Restart Authelia" button (flag-file mode, no Docker socket needed):
# a systemd path unit watches for the request file the admin app creates in your Authelia folder, and a
# service restarts the Authelia container when it appears. Run it on the Docker host (e.g. your Raspberry Pi).
#
#   sudo ./install-restart-service.sh --folder /path/to/authelia [--container authelia] [--flag .restart-request]
#   ./install-restart-service.sh --folder /path/to/authelia --dry-run     # show what would be written, change nothing
#   sudo ./install-restart-service.sh --uninstall
#
# --folder     the Authelia folder on the host, the one mounted as /config in both containers (required)
# --container  name of the Authelia container (default: found automatically, the one container made from the
#              authelia/authelia image; if there are several, the script lists them and asks you to choose)
# --flag       file name of the request inside that folder (default: .restart-request)
#
# Afterwards, set in the admin container's compose file:  RESTART_FLAG_FILE: /config/<flag>
set -euo pipefail

FOLDER="" CONTAINER="" FLAG=".restart-request" DRY=0 UNINSTALL=0
UNIT_DIR="/etc/systemd/system"
NAME="authelia-restart"

die()  { echo "Error: $*" >&2; exit 1; }
info() { echo "  $*"; }
usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --folder)     FOLDER="${2:-}"; shift 2 ;;
    --container)  CONTAINER="${2:-}"; shift 2 ;;
    --flag)       FLAG="${2:-}"; shift 2 ;;
    --dry-run)    DRY=1; shift ;;
    --uninstall)  UNINSTALL=1; shift ;;
    -h|--help)    usage 0 ;;
    *)            echo "Unknown option: $1" >&2; usage 1 ;;
  esac
done

need_root() { [ "$(id -u)" -eq 0 ] || die "run it with sudo (it writes to $UNIT_DIR and uses systemctl)."; }

if [ "$UNINSTALL" -eq 1 ]; then
  [ "$DRY" -eq 1 ] && { echo "Would stop and remove $UNIT_DIR/$NAME.path and $UNIT_DIR/$NAME.service"; exit 0; }
  need_root
  systemctl disable --now "$NAME.path" 2>/dev/null || true
  rm -f "$UNIT_DIR/$NAME.path" "$UNIT_DIR/$NAME.service"
  systemctl daemon-reload
  echo "Removed the restart service. Also remove RESTART_FLAG_FILE from the admin container's compose file."
  exit 0
fi

# ---- checks ----
[ -n "$FOLDER" ] || die "--folder is required: the Authelia folder on this host (mounted as /config). See --help."
FOLDER="${FOLDER%/}"
case "$FOLDER" in /*) ;; *) die "--folder must be an absolute path, for example /raid1/Config/Authelia" ;; esac
# systemd unit values: keep to plain path characters, so nothing needs escaping
[[ "$FOLDER" =~ ^[A-Za-z0-9/._-]+$ ]] || die "the folder path may only contain letters, digits, / . _ - (no spaces)."
[[ "$FLAG" =~ ^[A-Za-z0-9._-]+$ ]]    || die "--flag must be a plain file name, for example .restart-request"
[ -d "$FOLDER" ] || die "$FOLDER doesn't exist."

DOCKER="$(command -v docker || true)"
[ -n "$DOCKER" ] || die "docker isn't installed or not on PATH."
[ "$DRY" -eq 1 ] || need_root

# ---- which container to restart ----
if [ -z "$CONTAINER" ]; then
  # containers made from the official image (any registry or tag), running or stopped;
  # this deliberately doesn't match the admin image (authelia-admin)
  found="$("$DOCKER" ps -a --format '{{.Names}}|{{.Image}}|{{.State}}' 2>/dev/null \
           | awk -F'|' '$2 ~ /(^|\/)authelia\/authelia([:@]|$)/ {print $1 "|" $3}')" \
    || die "couldn't list containers (is Docker running, and do you have access to it?)."
  count="$(printf '%s\n' "$found" | grep -c . || true)"
  if [ "$count" -eq 1 ]; then
    CONTAINER="${found%%|*}"
    info "found the Authelia container: $CONTAINER (${found##*|})"
  elif [ "$count" -eq 0 ]; then
    die "no container made from the authelia/authelia image was found. Pass its name with --container (see: docker ps)."
  else
    echo "Error: several Authelia containers were found; choose one with --container:" >&2
    printf '%s\n' "$found" | awk -F'|' '{printf "    %s  (%s)\n", $1, $2}' >&2
    exit 1
  fi
fi
[[ "$CONTAINER" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "--container doesn't look like a container name."
if ! "$DOCKER" inspect --type container "$CONTAINER" >/dev/null 2>&1; then
  echo "Warning: no container named '$CONTAINER' right now. The service will try to restart that name; check it with: docker ps" >&2
fi

if [ "$DRY" -eq 0 ]; then
  command -v systemctl >/dev/null || die "this system doesn't use systemd (systemctl not found)."
fi

REQUEST="$FOLDER/$FLAG"
PATH_UNIT="[Unit]
Description=Watch for Authelia restart requests from Authelia Admin

[Path]
PathExists=$REQUEST
Unit=$NAME.service

[Install]
WantedBy=multi-user.target"

SERVICE_UNIT="[Unit]
Description=Restart the Authelia container (requested by Authelia Admin)

[Service]
Type=oneshot
ExecStartPre=/bin/rm -f $REQUEST
ExecStart=$DOCKER restart $CONTAINER"

if [ "$DRY" -eq 1 ]; then
  echo "Would write $UNIT_DIR/$NAME.path:"; echo "$PATH_UNIT" | sed 's/^/    /'
  echo; echo "Would write $UNIT_DIR/$NAME.service:"; echo "$SERVICE_UNIT" | sed 's/^/    /'
  echo; echo "Then: systemctl daemon-reload && systemctl enable --now $NAME.path"
  exit 0
fi

# ---- install ----
# an old request lying around would restart Authelia the moment the watcher starts
if [ -e "$REQUEST" ]; then
  rm -f "$REQUEST"
  info "removed an old, unanswered restart request ($REQUEST)"
fi
printf '%s\n' "$PATH_UNIT" > "$UNIT_DIR/$NAME.path"
printf '%s\n' "$SERVICE_UNIT" > "$UNIT_DIR/$NAME.service"
systemctl daemon-reload
systemctl enable --now "$NAME.path" >/dev/null 2>&1 || die "couldn't enable $NAME.path (see: systemctl status $NAME.path)"

state="$(systemctl is-active "$NAME.path" || true)"
echo "Installed and started ($NAME.path is $state)."
info "Watching: $REQUEST"
info "Restarts: container '$CONTAINER'"
echo
echo "Next: in the admin container's compose file set"
echo "    RESTART_FLAG_FILE: /config/$FLAG"
echo "then recreate it (docker compose up -d). The Restart button then works without the Docker socket."
echo "Check the watcher any time with: systemctl status $NAME.path"
