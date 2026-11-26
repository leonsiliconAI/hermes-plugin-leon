#!/usr/bin/env bash
# Bring up grok-box-browser in Leon's Grok Bot cloud computer: userspace Tailscale on the tailnet as
# tag:grok-box, `tailscale serve` for the broker's port, and the broker. Safe to rerun: after a box
# restart, run it again. Prints a summary without secrets. See ../README.md.
set -euo pipefail

home=${GBB_HOME:-/workspace/hermes-browsers}
port=${GBB_PORT:-9400}
hostname=${GBB_TS_HOSTNAME:-grok-box}
ts_version=1.104.7
ts_dir=$home/tailscale
sock=$ts_dir/tailscaled.sock
logs=$home/logs
here=$(cd "$(dirname "$0")" && pwd)

umask 077
mkdir -p "$home" "$ts_dir/state" "$logs" "$home/profiles"
[ -e "$home/.created" ] || date -u +%FT%TZ > "$home/.created"
[ -s "$home/token" ] || { echo "missing $home/token (the broker's bearer token)" >&2; exit 1; }

# Tailscale, from pkgs.tailscale.com with its published sha256.
case "$(uname -m)" in
  x86_64 | amd64) arch=amd64 sum=e56693ff74a64f2591b7e819c280635a100aae1ddfb0ef058fcd052b7cd8d16c ;;
  aarch64 | arm64) arch=arm64 sum=d6bed2720310b328b861f943d7b4016c5358a9bad60575eb51e8143b79068484 ;;
  *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac
if ! "$ts_dir/bin/tailscale" version 2>/dev/null | head -1 | grep -qx "$ts_version"; then
  archive=$ts_dir/tailscale_${ts_version}_${arch}.tgz
  curl -fsSL "https://pkgs.tailscale.com/stable/tailscale_${ts_version}_${arch}.tgz" -o "$archive"
  echo "$sum  $archive" | sha256sum -c - >/dev/null
  mkdir -p "$ts_dir/bin"
  tar -xzf "$archive" -C "$ts_dir/bin" --strip-components=1 \
    "tailscale_${ts_version}_${arch}/tailscale" "tailscale_${ts_version}_${arch}/tailscaled"
  rm -f "$archive"
fi
ts() { "$ts_dir/bin/tailscale" --socket="$sock" "$@"; }

running() { [ -s "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

if ! running "$ts_dir/tailscaled.pid"; then
  nohup "$ts_dir/bin/tailscaled" --tun=userspace-networking --statedir="$ts_dir/state" \
    --socket="$sock" --port=0 >>"$logs/tailscaled.log" 2>&1 &
  echo $! > "$ts_dir/tailscaled.pid"
  for _ in $(seq 1 50); do ts status --json >/dev/null 2>&1 && break; sleep 0.2; done
fi

state() { ts status --json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin).get("BackendState",""))' 2>/dev/null || true; }
if [ "$(state)" != Running ]; then
  # The first run joins with the one-time key in tailscale-authkey; after that the login lives in state/.
  key=$home/tailscale-authkey
  if [ -s "$key" ]; then
    ts up --auth-key="file:$key" --hostname="$hostname" --advertise-tags=tag:grok-box \
      --accept-dns=false --accept-routes=false --timeout=60s
    rm -f "$key"
  else
    ts up --hostname="$hostname" --accept-dns=false --accept-routes=false --timeout=30s || true
  fi
fi
ts serve --bg --tcp="$port" "tcp://127.0.0.1:$port" >/dev/null

# The broker, on Node 22+ (it needs the built-in WebSocket client) or Bun.
runtime=bun
if command -v node >/dev/null && [ "$(node -p 'process.versions.node.split(".")[0]')" -ge 22 ]; then runtime=node; fi
if ! curl -fsS -m 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
  GBB_HOME=$home GBB_PORT=$port nohup "$runtime" "$here/broker.mjs" >>"$logs/broker.log" 2>&1 &
  echo $! > "$home/broker.pid"
  for _ in $(seq 1 50); do curl -fsS -m 1 "http://127.0.0.1:$port/health" >/dev/null 2>&1 && break; sleep 0.2; done
fi

echo "tailscale: $(state) $(ts ip -4 2>/dev/null | head -1) $hostname"
echo "serve: $(ts serve status 2>/dev/null | tr -s ' \n' ' ' | cut -c1-160)"
echo "broker: $(curl -fsS -m 2 "http://127.0.0.1:$port/health" 2>&1 || echo unreachable) ($runtime $("$runtime" --version 2>/dev/null))"
echo "memory: $(free -m | awk '/^Mem:/{print $2" MB total, "$7" MB available"}')"
echo "home created: $(cat "$home/.created")"
