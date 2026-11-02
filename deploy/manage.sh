#!/usr/bin/env bash
# API Cost Monitor — host setup, deploy, rollback, restore and status.
#
#   sudo ./manage.sh setup-host                  first time only: user, directories, units
#   sudo ./manage.sh deploy  RELEASE.tar.gz      install a release; auto-rollback on failure
#   sudo ./manage.sh rollback [RELEASE_DIR_NAME] switch back to the previous (or named) release
#   sudo ./manage.sh restore  BACKUP.db          restore the database from a backup file
#   sudo ./manage.sh backup                      take a backup now (online, verified)
#        ./manage.sh status                      releases, service state, readiness
#
# It only ever touches its own paths: /opt/api-cost-monitor, /var/lib/api-cost-monitor,
# /etc/api-cost-monitor and the api-cost-monitor* systemd units. It never edits nginx, the
# main application, its services or its .env, and never overwrites an existing config file.
#
# Every path can be overridden through the environment (used by the WSL rehearsal).
set -Eeuo pipefail
umask 022

SERVICE="${SERVICE:-api-cost-monitor}"
SERVICE_USER="${SERVICE_USER:-apimon}"
APP_ROOT="${APP_ROOT:-/opt/api-cost-monitor}"
STATE_DIR="${STATE_DIR:-/var/lib/api-cost-monitor}"
CONFIG_DIR="${CONFIG_DIR:-/etc/api-cost-monitor}"
CONFIG_FILE="${CONFIG_FILE:-$CONFIG_DIR/monitor.env}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
PORT="${PORT:-8100}"
KEEP_RELEASES="${KEEP_RELEASES:-5}"
READY_TIMEOUT="${READY_TIMEOUT:-90}"
DB_FILE="$STATE_DIR/monitor.db"
BACKUP_DIR="$STATE_DIR/backups"
BASE_URL="http://127.0.0.1:$PORT"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '%s [manage] %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

as_service_user() {
  # Data files must be created by the service account, never by root, and private (0600).
  if [[ $EUID -eq 0 ]]; then (umask 077; runuser -u "$SERVICE_USER" -- "$@"); else (umask 077; "$@"); fi
}

need_root() {
  [[ $EUID -eq 0 || "${ALLOW_NON_ROOT:-0}" == 1 ]] || die "run with sudo"
}

pick_python() {
  if [[ -n "${PYTHON:-}" ]]; then echo "$PYTHON"; return; fi
  for candidate in python3.12 python3; do
    if command -v "$candidate" >/dev/null &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
      command -v "$candidate"; return
    fi
  done
  die "Python 3.12 not found. Install python3.12 (+ venv), or set PYTHON=/path/to/python3.12"
}

current_release() { [[ -L "$APP_ROOT/current" ]] && basename "$(readlink -f "$APP_ROOT/current")" || true; }

switch_to() {
  ln -sfn "$APP_ROOT/releases/$1" "$APP_ROOT/current.tmp"
  mv -Tf "$APP_ROOT/current.tmp" "$APP_ROOT/current"
  log "current -> releases/$1"
}

start_service() {
  # A release that crash-looped can leave the unit at its start limit ("Start request
  # repeated too quickly"); clear that first or the start (e.g. of a rollback) is refused.
  $SYSTEMCTL reset-failed "$SERVICE" >/dev/null 2>&1 || true
  $SYSTEMCTL start "$SERVICE"
}

wait_ready() {
  local python="$APP_ROOT/current/venv/bin/python" deadline=$((SECONDS + READY_TIMEOUT))
  while (( SECONDS < deadline )); do
    if "$python" "$APP_ROOT/current/deploy/smoke_test.py" --base-url "$BASE_URL" --ready-only >/dev/null 2>&1; then
      log "service is ready"; return 0
    fi
    if $SYSTEMCTL is-failed --quiet "$SERVICE"; then
      log "service failed to start (systemd gave up restarting it)"; break
    fi
    sleep 2
  done
  log "service did not become ready within ${READY_TIMEOUT}s"
  "$python" "$APP_ROOT/current/deploy/smoke_test.py" --base-url "$BASE_URL" --ready-only || true
  return 1
}

# Standard-library only; during a deploy it is the copy from the release being installed.
DB_TOOL="${DB_TOOL:-$HERE/db_tool.py}"
db_tool() { as_service_user "$(pick_python)" "$DB_TOOL" "$@"; }

backup_db() {  # $1 = label
  [[ -f "$DB_FILE" ]] || { log "no database yet; skipping backup"; return 0; }
  local dest="$BACKUP_DIR/$1-$(date -u +%Y%m%dT%H%M%SZ).db"
  db_tool backup "$DB_FILE" "$dest" >&2
  echo "$dest"
}

restore_file() {  # $1 = backup file; service must be stopped
  if [[ $EUID -eq 0 ]]; then
    install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" "$1" "$DB_FILE.restoring"
  else
    install -m 0600 "$1" "$DB_FILE.restoring"
  fi
  rm -f "$DB_FILE-wal" "$DB_FILE-shm" "$DB_FILE-journal"
  mv -f "$DB_FILE.restoring" "$DB_FILE"
  db_tool clear-locks "$DB_FILE"  # leases held by whichever process took the backup
}

migrate() {  # runs in the release that `current` points to
  log "applying database migrations"
  (cd "$APP_ROOT/current/backend" &&
   as_service_user env DATABASE_URL="sqlite:///$DB_FILE" "$APP_ROOT/current/venv/bin/python" -m alembic upgrade head)
  chmod 600 "$DB_FILE"  # also repairs databases created by older versions of this script
}

cmd_setup_host() {
  need_root
  if [[ $EUID -eq 0 ]] && ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$STATE_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    log "created system user $SERVICE_USER"
  fi
  install -d -m 0755 "$APP_ROOT" "$APP_ROOT/releases"
  install -d -m 0700 "$CONFIG_DIR"
  if [[ $EUID -eq 0 ]]; then install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR" "$BACKUP_DIR"
  else install -d -m 0700 "$STATE_DIR" "$BACKUP_DIR"; fi
  if [[ -e "$CONFIG_FILE" ]]; then
    log "keeping existing $CONFIG_FILE (never overwritten)"
  else
    install -m 0600 "$HERE/monitor.env.example" "$CONFIG_FILE"
    log "created $CONFIG_FILE from the example — edit it before the first deploy"
  fi
  local unit
  for unit in "$SERVICE.service" "$SERVICE-healthcheck.service" "$SERVICE-healthcheck.timer"; do
    install -m 0644 "$HERE/systemd/$unit" "$UNIT_DIR/$unit"
  done
  if [[ $EUID -eq 0 ]]; then
    install -m 0644 "$HERE/systemd/journald@$SERVICE.conf" "/etc/systemd/journald@$SERVICE.conf"
  fi
  $SYSTEMCTL daemon-reload
  $SYSTEMCTL enable "$SERVICE.service" "$SERVICE-healthcheck.timer"
  log "units installed and enabled (not started). Next: edit $CONFIG_FILE, then: $0 deploy RELEASE.tar.gz"
}

validate_config() {
  [[ -f "$CONFIG_FILE" ]] || die "missing $CONFIG_FILE (run setup-host)"
  local mode; mode=$(stat -c '%a' "$CONFIG_FILE")
  [[ "$mode" == 600 || "$mode" == 400 ]] || die "$CONFIG_FILE must be mode 600 (is $mode)"
  grep -Eq '^DASHBOARD_OWNER_TOKEN=.{32,}' "$CONFIG_FILE" || die "set DASHBOARD_OWNER_TOKEN (32+ chars) in $CONFIG_FILE"
  if grep -Eq '^DASHBOARD_OWNER_TOKEN=(change-me|CHANGE)' "$CONFIG_FILE"; then die "replace the example token in $CONFIG_FILE"; fi
}

cmd_deploy() {
  need_root
  local tarball="${1:?usage: deploy RELEASE.tar.gz}"
  [[ -f "$tarball" ]] || die "no such file: $tarball"
  validate_config
  local python; python=$(pick_python)

  local version
  version=$(tar -xzOf "$tarball" --wildcards '*/VERSION' | head -1)
  [[ "$version" =~ ^[A-Za-z0-9._-]+$ ]] || die "release has no valid VERSION file"
  if tar -tzf "$tarball" | grep -Eq '(^|/)\.env$|\.db$|(^|/)venv/'; then
    die "release contains a .env, database or venv; rebuild it with build_release.py"
  fi
  local target="$APP_ROOT/releases/$version"
  [[ -e "$target" ]] && die "release $version is already installed (use rollback to switch to it)"

  log "installing release $version"
  install -d -m 0755 "$target"
  tar -xzf "$tarball" -C "$target" --strip-components=1 --no-same-owner
  "$python" -m venv "$target/venv"
  "$target/venv/bin/python" -m pip install --quiet --no-cache-dir --disable-pip-version-check \
      --require-virtualenv -r "$target/backend/requirements.lock"
  (cd "$target/backend" && "$target/venv/bin/python" -c "import app.main") ||
      die "release $version does not import; not switching to it"
  if [[ $EUID -eq 0 ]]; then chown -R root:root "$target"; fi
  chmod -R go-w "$target"
  DB_TOOL="$target/deploy/db_tool.py"  # readable by the service account, unlike /root or /tmp copies

  local previous; previous=$(current_release)
  local backup="" before_rev="none"
  if [[ -n "$previous" ]]; then
    log "stopping $SERVICE (previous release: $previous)"
    $SYSTEMCTL stop "$SERVICE"
    backup=$(backup_db "pre-deploy-$version")
    [[ -f "$DB_FILE" ]] && before_rev=$(db_tool revision "$DB_FILE")
  fi

  switch_to "$version"
  if migrate && start_service && wait_ready &&
     "$APP_ROOT/current/venv/bin/python" "$APP_ROOT/current/deploy/smoke_test.py" --base-url "$BASE_URL"; then
    [[ -n "$previous" ]] && echo "$previous" > "$APP_ROOT/previous_release"
    log "deployed $version"
    prune_releases
    return 0
  fi

  log "DEPLOY FAILED — rolling back"
  $SYSTEMCTL stop "$SERVICE" || true
  local failed_dir; failed_dir="$APP_ROOT/failed-$version-$(date -u +%Y%m%dT%H%M%SZ)"
  if [[ -z "$previous" ]]; then
    rm -f "$APP_ROOT/current"; mv "$target" "$failed_dir"
    die "first install failed (kept in $failed_dir). Check: journalctl --namespace=$SERVICE -u $SERVICE"
  fi
  switch_to "$previous"
  mv "$target" "$failed_dir"; log "failed release kept for inspection in $failed_dir"
  if [[ -n "$backup" && "$(db_tool revision "$DB_FILE" 2>/dev/null || echo none)" != "$before_rev" ]]; then
    log "schema changed during the failed deploy; restoring $backup"
    restore_file "$backup"
  fi
  start_service || die "rolled back to $previous but systemd could not start it — see the journal"
  wait_ready || die "rollback started but the service is not ready — see the journal"
  die "deploy of $version failed; rolled back to $previous (failed release kept in $failed_dir)"
}

cmd_rollback() {
  need_root
  local current; current=$(current_release)
  local target="${1:-}"
  if [[ -z "$target" && -f "$APP_ROOT/previous_release" ]]; then
    target=$(cat "$APP_ROOT/previous_release")
  fi
  [[ "$target" != "$current" ]] || die "$target is already the current release"
  [[ -n "$target" && -d "$APP_ROOT/releases/$target" ]] || die "no release to roll back to"
  log "rolling back $current -> $target"
  $SYSTEMCTL stop "$SERVICE"
  local safety; safety=$(backup_db "pre-rollback-$current")
  switch_to "$target"
  echo "$current" > "$APP_ROOT/previous_release"
  local pre; pre=$(ls -1t "$BACKUP_DIR"/pre-deploy-"$current"-*.db 2>/dev/null | head -1 || true)
  if [[ -n "$pre" && "$(db_tool revision "$DB_FILE")" != "$(db_tool revision "$pre")" ]]; then
    log "the newer release changed the schema; restoring $pre (current data kept in $safety)"
    restore_file "$pre"
  fi
  start_service || die "rolled back but systemd could not start the service — see the journal"
  wait_ready || die "rolled back but not ready — see the journal"
  log "rolled back to $target"
}

cmd_restore() {
  need_root
  local file="${1:?usage: restore BACKUP.db}"
  [[ -f "$file" ]] || die "no such file: $file"
  db_tool check "$file" || die "backup failed its integrity check; not restoring"
  $SYSTEMCTL stop "$SERVICE"
  local safety; safety=$(backup_db "pre-restore")
  log "current database kept as ${safety:-<none>}"
  restore_file "$file"
  migrate  # a backup from an older release is brought up to the installed schema
  start_service || die "restored but systemd could not start the service — see the journal"
  wait_ready || die "restored but the service is not ready — see the journal"
  log "restore complete: $(db_tool check "$DB_FILE")"
}

cmd_backup() {
  need_root
  local dest; dest=$(backup_db "manual")
  if [[ -n "$dest" ]]; then log "backup written: $dest"; fi
}

prune_releases() {
  local current; current=$(current_release)
  ls -1t "$APP_ROOT/releases" | grep -vx "$current" | tail -n +"$KEEP_RELEASES" | while read -r old; do
    rm -rf "${APP_ROOT:?}/releases/$old"; log "pruned old release $old"
  done
}

cmd_status() {
  echo "current release: $(current_release || echo none)"
  echo "installed releases:"; ls -1t "$APP_ROOT/releases" 2>/dev/null | sed 's/^/  /'
  $SYSTEMCTL --no-pager status "$SERVICE" 2>/dev/null | head -5 || true
  if [[ -x "$APP_ROOT/current/venv/bin/python" ]]; then
    "$APP_ROOT/current/venv/bin/python" "$APP_ROOT/current/deploy/smoke_test.py" --base-url "$BASE_URL" --ready-only --disk-path "$STATE_DIR" || true
  fi
  echo "backups:"; ls -1t "$BACKUP_DIR" 2>/dev/null | head -5 | sed 's/^/  /'
}

case "${1:-}" in
  setup-host) shift; cmd_setup_host "$@" ;;
  deploy)     shift; cmd_deploy "$@" ;;
  rollback)   shift; cmd_rollback "$@" ;;
  restore)    shift; cmd_restore "$@" ;;
  backup)     shift; cmd_backup "$@" ;;
  status)     shift; cmd_status "$@" ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
