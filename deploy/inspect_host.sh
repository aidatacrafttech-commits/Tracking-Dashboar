#!/usr/bin/env bash
# Read-only inventory of the EC2 host, to run BEFORE the first deployment.
#
#   sudo bash inspect_host.sh > host-inventory.txt
#
# Changes nothing, starts or stops nothing, and prints no secrets: it never reads .env
# files, environment variables of processes, private keys or certificate keys. nginx
# configuration is summarised to listen/server_name/proxy_pass lines only.
set -uo pipefail

section() { printf '\n===== %s =====\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

section "Host"
. /etc/os-release 2>/dev/null && echo "OS: $PRETTY_NAME"
echo "Kernel: $(uname -r)  Arch: $(uname -m)"
echo "Uptime: $(uptime -p 2>/dev/null)"
echo "CPUs: $(nproc)"; free -h | sed -n '1,2p'
have systemctl && echo "systemd: $(systemctl --version | head -1)"

section "EC2 instance metadata (IMDSv2; no credentials are requested)"
TOKEN=$(curl -s -m 2 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' || true)
if [[ -n "$TOKEN" ]]; then
  for key in instance-id instance-type placement/region public-ipv4; do
    printf '%-16s %s\n' "$key:" "$(curl -s -m 2 -H "X-aws-ec2-metadata-token: $TOKEN" "http://169.254.169.254/latest/meta-data/$key")"
  done
  ROLE=$(curl -s -m 2 -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/iam/info)
  if grep -q InstanceProfileArn <<<"$ROLE"; then
    echo "instance profile: $(grep -o '"InstanceProfileArn" *: *"[^"]*"' <<<"$ROLE" | cut -d'"' -f4)"
  else
    echo "instance profile: none attached"
  fi
else
  echo "IMDSv2 not reachable (not EC2, or metadata disabled)"
fi

section "Disk"
df -hT -x tmpfs -x devtmpfs -x squashfs 2>/dev/null
have journalctl && journalctl --disk-usage 2>/dev/null

section "Python"
for p in python3.12 python3.11 python3; do have "$p" && echo "$p -> $($p --version 2>&1) ($(command -v $p))"; done
python3 -c 'import venv, ensurepip' 2>/dev/null && echo "venv + ensurepip: available" || echo "venv/ensurepip: MISSING (apt install python3.12-venv)"
have uv && echo "uv: $(uv --version)"

section "Listening TCP ports (process names only)"
ss -ltnp 2>/dev/null | awk 'NR==1 || /LISTEN/ {print $4, $6}' | sed -E 's/pid=[0-9]+,fd=[0-9]+//g'
ss -ltn 2>/dev/null | grep -q ':8100 ' && echo "WARNING: port 8100 is already in use" || echo "port 8100: free"

section "Running services"
systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null | awk '{print $1}'
echo "--- enabled timers"
systemctl list-timers --no-pager --no-legend 2>/dev/null | awk '{print $(NF-1), $NF}'
echo "--- other process managers"
have pm2 && echo "pm2 present"; have supervisorctl && echo "supervisor present"; have docker && echo "docker present"
pgrep -a gunicorn 2>/dev/null | sed -E 's/--?[a-zA-Z-]*(key|token|secret|password)[^ ]*//Ig' | head -5
pgrep -a uvicorn 2>/dev/null | head -5

section "nginx"
if have nginx; then
  nginx -v 2>&1
  nginx -T 2>/dev/null | grep -E '^\s*(# configuration file|listen|server_name|proxy_pass|ssl_certificate |include .*sites|limit_req_zone)' | sed 's/^\s*//' | head -80
else
  echo "nginx not installed"
fi

section "TLS certificates (names and expiry only)"
if have certbot; then certbot certificates 2>/dev/null | grep -E 'Certificate Name|Domains|Expiry'; else echo "certbot not installed"; fi

section "Firewall"
have ufw && ufw status 2>/dev/null | head -20
have firewall-cmd && firewall-cmd --list-all 2>/dev/null | head -20

section "Time sync"
timedatectl 2>/dev/null | grep -E 'Time zone|synchronized|NTP'

section "Existing API Cost Monitor install (if any)"
ls -ld /opt/api-cost-monitor /var/lib/api-cost-monitor /etc/api-cost-monitor 2>&1
id apimon 2>&1

section "Done — nothing was changed"
