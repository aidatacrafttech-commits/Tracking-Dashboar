#!/usr/bin/env bash
# Deployment rehearsal in WSL/Ubuntu without root. Exercises the REAL deploy kit
# (manage.sh, units, smoke test, nginx site) with only these differences from EC2:
#   * paths under ~/acm-rehearsal instead of /opt, /var/lib, /etc
#   * `systemctl --user` instead of the system manager (no dedicated user account)
#   * sandbox options that need root, and the journal namespace, are dropped from the unit
#   * nginx runs unprivileged on 8443/8080 with a self-signed certificate
#   * a local SMTP sink instead of a real mail server; no provider credentials
#
#   rehearse_wsl.sh setup RELEASE.tar.gz     host setup + config + mail sink
#   rehearse_wsl.sh manage ARGS...           run manage.sh with rehearsal paths
#   rehearse_wsl.sh smoke [URL] [FLAGS...]   smoke test with the rehearsal owner token
#   rehearse_wsl.sh accept CMD [ARGS...]     acceptance.py (writes rehearsal data only)
#   rehearse_wsl.sh nginx-start | nginx-stop
#   rehearse_wsl.sh verify-units             systemd-analyze verify on the shipped units
#   rehearse_wsl.sh teardown
set -Eeuo pipefail
R="$HOME/acm-rehearsal"
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export APP_ROOT="$R/opt" STATE_DIR="$R/var" CONFIG_DIR="$R/etc" UNIT_DIR="$HOME/.config/systemd/user"
export SYSTEMCTL="systemctl --user" ALLOW_NON_ROOT=1
CONFIG_FILE="$CONFIG_DIR/monitor.env"

user_unit() {  # transform a shipped system unit into a user unit with rehearsal paths
  sed -e "s#/opt/api-cost-monitor#$APP_ROOT#g" \
      -e "s#/var/lib/api-cost-monitor#$STATE_DIR#g" \
      -e "s#/etc/api-cost-monitor#$CONFIG_DIR#g" \
      -e '/^\(User\|Group\|LogNamespace\|StateDirectory\|StateDirectoryMode\|PrivateDevices\|ProtectKernelTunables\|ProtectKernelModules\|ProtectKernelLogs\|ProtectControlGroups\|ProtectClock\|ProtectHostname\|RestrictNamespaces\|RemoveIPC\|CapabilityBoundingSet\|AmbientCapabilities\|ProtectSystem\|ProtectHome\|PrivateTmp\)=/d' \
      -e 's/WantedBy=multi-user.target/WantedBy=default.target/' "$1"
}

token() { python3 -c "import secrets; print(secrets.token_urlsafe(48))"; }
set_conf() { sed -i "s#^$1=.*#$1=$2#" "$CONFIG_FILE"; }

cmd_setup() {
  local tarball="$1" stage="$R/stage"
  mkdir -p "$R/secrets" && chmod 700 "$R/secrets"
  rm -rf "$stage" && mkdir -p "$stage"
  tar -xzf "$tarball" -C "$stage" --strip-components=1
  for unit in "$stage"/deploy/systemd/*.service "$stage"/deploy/systemd/*.timer; do
    user_unit "$unit" > "$unit.user" && mv "$unit.user" "$unit"
  done
  mkdir -p "$UNIT_DIR"
  "$stage/deploy/manage.sh" setup-host
  if ! grep -q '^DASHBOARD_OWNER_TOKEN=.\{32,\}' "$CONFIG_FILE"; then
    local owner viewer; owner=$(token); viewer=$(token)
    set_conf DASHBOARD_OWNER_TOKEN "$owner"; set_conf DASHBOARD_VIEWER_TOKEN "$viewer"
    umask 077; printf 'SMOKE_TOKEN=%s\n' "$owner" > "$R/secrets/owner.env"
  fi
  set_conf EMAIL_DELIVERY_ENABLED true; set_conf SMTP_HOST 127.0.0.1; set_conf SMTP_PORT 2525
  set_conf SMTP_SECURITY none; set_conf SMTP_FROM monitor@example.com; set_conf ALERT_RECIPIENTS ops@example.com
  set_conf HEALTH_CHECK_INTERVAL_MINUTES 10
  if ! pgrep -f "mail_sink.py 2525" >/dev/null; then
    nohup python3 "$KIT/rehearsal/mail_sink.py" 2525 "$R/mail" > "$R/mail_sink.log" 2>&1 &
    sleep 1
  fi
  echo "rehearsal host ready: config $CONFIG_FILE (mode $(stat -c %a "$CONFIG_FILE")), units in $UNIT_DIR"
}

cmd_manage() {
  # Like production: `deploy` runs the manage.sh shipped in the NEW release (extracted
  # first); every other command runs the one in the current release.
  local script="$APP_ROOT/current/deploy/manage.sh"
  if [[ "${1:-}" == deploy ]]; then
    rm -rf "$R/incoming" && mkdir -p "$R/incoming"
    tar -xzf "$2" -C "$R/incoming" --strip-components=1 --wildcards '*/deploy/*'
    script="$R/incoming/deploy/manage.sh"
  fi
  [[ -x "$script" ]] || script="$R/stage/deploy/manage.sh"
  "$script" "$@"
}

load_token() { set -a; . "$R/secrets/owner.env"; set +a; }

cmd_smoke() {
  load_token
  local url="${1:-http://127.0.0.1:8100}"; shift || true
  "$APP_ROOT/current/venv/bin/python" "$KIT/smoke_test.py" --base-url "$url" "$@"
}

cmd_accept() {
  load_token
  local url="${ACCEPT_URL:-http://127.0.0.1:8100}"
  "$APP_ROOT/current/venv/bin/python" "$KIT/rehearsal/acceptance.py" "$url" "$@"
}

NGX="$R/nginx"
cmd_nginx_start() {
  mkdir -p "$NGX"/{pkg,conf,logs,tmp,certs,www}
  if [[ ! -x "$NGX/pkg/usr/sbin/nginx" ]]; then
    (cd "$NGX" && apt-get download nginx nginx-common >/dev/null && for deb in nginx_*.deb nginx-common_*.deb; do dpkg -x "$deb" pkg; done)
  fi
  if [[ ! -f "$NGX/certs/fullchain.pem" ]]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 7 -subj "/CN=localhost" \
      -addext "subjectAltName=DNS:localhost" \
      -keyout "$NGX/certs/privkey.pem" -out "$NGX/certs/fullchain.pem" 2>/dev/null
  fi
  # The shipped site, with only hostname, ports, certificate and log paths changed.
  # On a non-default port the Host header must keep the port ($http_host) to match the
  # browser's Origin; on 443 the shipped `$host` is equivalent.
  sed -e 's/monitor\.example\.com/localhost/g' \
      -e 's/listen 80;/listen 8080;/; s/listen \[::\]:80;/listen [::]:8080;/' \
      -e 's/listen 443 ssl http2;/listen 8443 ssl http2;/; s/listen \[::\]:443 ssl http2;/listen [::]:8443 ssl http2;/' \
      -e "s#/etc/letsencrypt/live/localhost#$NGX/certs#g" \
      -e "s#/var/log/nginx/#$NGX/logs/#g" -e "s#/var/www/letsencrypt#$NGX/www#g" \
      -e 's/proxy_set_header Host              $host;/proxy_set_header Host              $http_host;/' \
      -e 's#return 301 https://$host$request_uri;#return 301 https://$host:8443$request_uri;#' \
      "$KIT/nginx/api-cost-monitor.conf" > "$NGX/conf/site.conf"
  cat > "$NGX/conf/nginx.conf" <<EOF
pid $NGX/nginx.pid;
error_log $NGX/logs/error.log warn;
events { worker_connections 256; }
http {
    include $NGX/pkg/etc/nginx/mime.types;
    client_body_temp_path $NGX/tmp/body; proxy_temp_path $NGX/tmp/proxy;
    fastcgi_temp_path $NGX/tmp/fcgi; uwsgi_temp_path $NGX/tmp/uwsgi; scgi_temp_path $NGX/tmp/scgi;
    access_log $NGX/logs/access.log;
    include $NGX/conf/site.conf;
}
EOF
  local bin="$NGX/pkg/usr/sbin/nginx"
  "$bin" -v 2>&1
  "$bin" -t -p "$NGX" -c "$NGX/conf/nginx.conf" 2>&1
  [[ -f "$NGX/nginx.pid" ]] && kill "$(cat "$NGX/nginx.pid")" 2>/dev/null && sleep 1
  "$bin" -p "$NGX" -c "$NGX/conf/nginx.conf"
  echo "nginx up: https://localhost:8443 (CA file: $NGX/certs/fullchain.pem)"
}

cmd_nginx_stop() { [[ -f "$NGX/nginx.pid" ]] && kill "$(cat "$NGX/nginx.pid")" && echo "nginx stopped"; }

cmd_verify_units() {
  # Real system units, checked for unknown/invalid directives (missing binaries are expected
  # here because /opt/api-cost-monitor does not exist in the rehearsal).
  local out; out=$(SYSTEMD_LOG_LEVEL=warning systemd-analyze verify "$KIT"/systemd/*.service "$KIT"/systemd/*.timer 2>&1 || true)
  echo "$out" | grep -vE "is not executable|No such file or directory|Failed to prepare filename|Unit .* not found|Command .* is not executable" || true
  echo "$out" | grep -qiE "unknown key|unknown section|invalid|failed to parse|ignoring" && { echo "UNIT PROBLEMS FOUND"; return 1; }
  echo "units: no unknown or invalid directives"
}

cmd_teardown() {
  cmd_nginx_stop 2>/dev/null || true
  systemctl --user disable --now api-cost-monitor.service api-cost-monitor-healthcheck.timer 2>/dev/null || true
  rm -f "$UNIT_DIR"/api-cost-monitor*.service "$UNIT_DIR"/api-cost-monitor*.timer
  systemctl --user daemon-reload
  pkill -f "mail_sink.py 2525" || true
  rm -rf "$R"
  echo "rehearsal removed"
}

case "${1:-}" in
  setup) shift; cmd_setup "$@" ;;
  manage) shift; cmd_manage "$@" ;;
  smoke) shift; cmd_smoke "$@" ;;
  accept) shift; cmd_accept "$@" ;;
  nginx-start) cmd_nginx_start ;;
  nginx-stop) cmd_nginx_stop ;;
  verify-units) cmd_verify_units ;;
  teardown) cmd_teardown ;;
  *) sed -n '2,18p' "$0"; exit 2 ;;
esac
