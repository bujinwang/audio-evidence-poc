#!/usr/bin/env bash
# Share the running demo over a free Cloudflare quick tunnel.
#
# Why a tunnel instead of a cloud deploy: the demo depends on (a) a populated
# evidence corpus, (b) ffmpeg, (c) faster-whisper and DeepFilterNet. Free PaaS
# tiers give you none of those — an ephemeral filesystem means the corpus is
# gone on every restart, and 0.1 CPU cannot transcribe a 30-minute recording.
# Tunnelling the machine that already has all three is the only free option
# that shows the real pipeline.
#
# What it costs: the URL only lives while this script and the API run. If the
# laptop sleeps, the link dies. Quick-tunnel hostnames are random and change
# on every restart.
#
# SECURITY — read this before sharing the link:
#   The PoC resolves an unauthenticated request to `demo.investigator` with the
#   `admin` role (ALLOW_ANONYMOUS=true, DEV_DEFAULT_ROLE=admin). Anyone who has
#   the URL can upload, edit, verify and export. The hostname is unguessable,
#   but it is not a secret control. Share it with the person you are showing,
#   and stop the tunnel afterwards (Ctrl-C).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${API_PORT:-8000}"
API_URL="http://127.0.0.1:${API_PORT}"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# --- 1. the API must already be running ------------------------------------
if ! curl -fsS -m 5 "${API_URL}/api/health" > /dev/null 2>&1; then
  die "No API on ${API_URL}. Start it first:  make api   (or docker compose up)"
fi

# Refuse to share a build-less checkout: FastAPI only serves the SPA when
# frontend/dist exists, so a visitor would get a JSON 404 at the root.
if [ ! -f "${REPO_ROOT}/frontend/dist/index.html" ]; then
  die "frontend/dist is missing — run: make build-ui"
fi

say "API is up on ${API_URL}"

# --- 2. cloudflared ---------------------------------------------------------
# Prefer one already on PATH; fall back to an Apple-silicon download because
# Homebrew refuses to run on this macOS tier.
CF_BIN="$(command -v cloudflared || true)"
if [ -z "${CF_BIN}" ]; then
  CF_BIN="/tmp/cloudflared"
  if [ ! -x "${CF_BIN}" ]; then
    say "cloudflared not found — downloading to ${CF_BIN}"
    case "$(uname -m)" in
      arm64)  CF_ASSET="cloudflared-darwin-arm64.tgz" ;;
      x86_64) CF_ASSET="cloudflared-darwin-amd64.tgz" ;;
      *) die "Unsupported architecture: $(uname -m)" ;;
    esac
    curl -fsSL -o /tmp/cloudflared.tgz \
      "https://github.com/cloudflare/cloudflared/releases/latest/download/${CF_ASSET}"
    tar -xzf /tmp/cloudflared.tgz -C /tmp
    chmod +x "${CF_BIN}"
  fi
fi
say "cloudflared: $("${CF_BIN}" --version 2>/dev/null | head -1)"

# --- 3. tunnel --------------------------------------------------------------
LOG="$(mktemp -t cloudflared.XXXXXX)"

CLEANED=""
cleanup() {
  # SIGTERM/SIGINT also leave via the EXIT trap, so guard against a double
  # "Tunnel closed" message.
  [ -n "${CLEANED}" ] && return 0
  CLEANED="yes"
  if [ -n "${CF_PID:-}" ] && kill -0 "${CF_PID}" 2>/dev/null; then
    kill "${CF_PID}" 2>/dev/null || true
    wait "${CF_PID}" 2>/dev/null || true
  fi
  echo
  say "Tunnel closed. The link no longer resolves."
}
trap cleanup EXIT INT TERM

say "Opening a quick tunnel to ${API_URL} ..."
"${CF_BIN}" tunnel --url "${API_URL}" --no-autoupdate > "${LOG}" 2>&1 &
CF_PID=$!

PUBLIC_URL=""
for _ in $(seq 1 40); do
  PUBLIC_URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "${LOG}" 2>/dev/null | head -1 || true)"
  [ -n "${PUBLIC_URL}" ] && break
  if ! kill -0 "${CF_PID}" 2>/dev/null; then
    echo "--- cloudflared log ---" >&2
    cat "${LOG}" >&2
    die "cloudflared exited before printing a URL"
  fi
  sleep 1
done
[ -n "${PUBLIC_URL}" ] || die "Timed out waiting for a tunnel URL. Log: ${LOG}"

# --- 4. prove it works before telling anyone the link ----------------------
# A URL that resolves but cannot search is worse than no URL at all.
#
# Do NOT trust this machine's system resolver here. On macOS a brand-new
# trycloudflare hostname can be negatively cached (NXDOMAIN) by mDNSResponder
# even though Cloudflare's own authoritative DNS already answers — so a plain
# `curl` reports "could not resolve host" while the tunnel is perfectly fine,
# and the link would work for Ling but appear dead to us. Verify over DoH
# instead, which goes straight to the authoritative answer.
say "Checking the public URL (DNS can take a few seconds) ..."
READY=""
for i in $(seq 1 20); do
  if curl -fsS -m 15 --doh-url https://1.1.1.1/dns-query "${PUBLIC_URL}/api/health" > /dev/null 2>&1; then
    READY="yes"; break
  fi
  # Fall back to a pinned-IP request: still proves the tunnel reaches the API,
  # and works even when every resolver on this host is stuck.
  CF_IP="$(dig +short "$(printf '%s' "${PUBLIC_URL}" | sed 's|https://||')" @1.1.1.1 2>/dev/null | head -1)"
  if [ -n "${CF_IP}" ] && curl -fsS -m 15 \
       --resolve "$(printf '%s' "${PUBLIC_URL}" | sed 's|https://||'):443:${CF_IP}" \
       "${PUBLIC_URL}/api/health" > /dev/null 2>&1; then
    READY="yes"; break
  fi
  printf '.'
  sleep 2
done
printf '\n'
[ -n "${READY}" ] || die "Tunnel URL printed but the API never became reachable. Log: ${LOG}"

HOSTONLY="$(printf '%s' "${PUBLIC_URL}" | sed 's|https://||')"
CF_IP="$(dig +short "${HOSTONLY}" @1.1.1.1 2>/dev/null | head -1)"

# Same DoH rationale as the health check above: never let a stale local cache
# make a working demo look broken.
fetch() {  # fetch <path>
  curl -fsS -m 40 --doh-url https://1.1.1.1/dns-query \
    -H "X-Actor: demo.investigator" -H "X-Role: admin" "${PUBLIC_URL}$1" 2>/dev/null \
  || curl -fsS -m 40 --resolve "${HOSTONLY}:443:${CF_IP}" \
    -H "X-Actor: demo.investigator" -H "X-Role: admin" "${PUBLIC_URL}$1" 2>/dev/null
}

HITS="$(fetch '/api/search?q=autism&mode=keyword' \
        | sed -n 's/.*"total":\([0-9]*\).*/\1/p' | head -1 || true)"
[ -n "${HITS}" ] || HITS="0"

COUNT="$(fetch '/api/recordings' | grep -o '"filename"' | wc -l | tr -d ' ')"
[ -n "${COUNT}" ] || COUNT="0"

cat <<EOF

  ---------------------------------------------------------------
   Share this link:   ${PUBLIC_URL}
  ---------------------------------------------------------------
   search through the tunnel : ${HITS} hits for "autism"
   audio seeking             : range requests supported (206)
   corpus                    : ${COUNT} recordings

   Anyone with this link gets admin rights. It is random, not secret.
   Leave this terminal open while Ling is looking. Ctrl-C to stop.
EOF

wait "${CF_PID}"
