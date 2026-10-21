#!/usr/bin/env bash
# Fetch the public audio library used by the demo.
#
# Four real recordings, no synthesised voices:
#   1. a 911 call released by a police department      (crime scene)
#   2. a high-school football championship broadcast    (sport, people talking)
#   3. a NASA news conference                           (public speech)
#   4. a university commencement address                (public speech)
#
# Each clip is cut to 2–3 minutes, downmixed to mono 44.1 kHz PCM, and
# peak-normalised to about -1.5 dBFS. Provenance, trims and rights notes live in
# data/public-audio/SOURCES.md — read that before publishing the demo anywhere.
#
# Usage:
#   scripts/fetch_public_audio.sh              # download and cut into data/public-audio/
#   scripts/fetch_public_audio.sh --upload     # ...and ingest into a running API
#   API=http://127.0.0.1:8000 scripts/fetch_public_audio.sh --upload
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${OUT:-$REPO_ROOT/data/public-audio}"
API="${API:-http://127.0.0.1:8000}"
UPLOAD=0
[[ "${1:-}" == "--upload" ]] && UPLOAD=1

for tool in yt-dlp ffmpeg curl; do
  command -v "$tool" >/dev/null || { echo "missing dependency: $tool" >&2; exit 1; }
done

mkdir -p "$OUT"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log() { printf '%s\n' "$*" >&2; }

# yt-dlp is slow on long sources: --download-sections pulls only the window we
# need, which is why the two broadcast sources don't cost a gigabyte.
# build <video-id> <out-name> <start> <duration> <gain-db> [section]
build() {
  local id="$1" name="$2" start="$3" dur="$4" gain="$5" section="${6:-}"
  if [[ -f "$OUT/$name" ]]; then
    log "skip $name (already present)"
    return
  fi
  log "→ $name"
  local args=(-f bestaudio --no-warnings --extract-audio --audio-format wav -o "$WORK/$name.%{ext}")
  if [[ -n "$section" ]]; then
    args+=(--download-sections "$section" --force-keyframes-at-cuts)
  fi
  yt-dlp "${args[@]}" "https://www.youtube.com/watch?v=$id" >/dev/null
  ffmpeg -y -hide_banner -loglevel error -ss "$start" -i "$WORK/$name.wav" -t "$dur" \
    -af "volume=${gain}dB" -ac 1 -ar 44100 -c:a pcm_s16le "$OUT/$name"
  log "  wrote $name"
}

upload() {  # upload <file> <source-type> <recorded-at>
  local file="$1" type="$2" when="$3"
  log "  ingesting $(basename "$file") as $type"
  curl -sS -X POST "$API/api/recordings" \
    -H "X-Actor: demo.curator" -H "X-Role: admin" \
    -F "file=@$file" -F "source_type=$type" -F "recorded_at=$when" \
    | grep -o '"id":"[^"]*"' | head -1 || true
  echo
}

# 1. Crime scene: 911 call released by the Phoenix Police Department.
#    Peak is already at full scale, so no gain is applied.
build A_JcfrFcabk 2022-09-07_911_call_phoenix.wav 0 160 0

# 2. Sport: 2025 Class AA high-school football championship, two commentators in
#    the booth at Highmark Stadium. Trimmed from the opening series of the game.
build 6QnA3dtbKZ0 2025-11-15_sport_football-championship.wav 0 180 6.2 "00:31:00-00:35:30"

# 3. Public speech: NASA Moon Base news conference.
build Nyki6O1oxIg 2026-05-26_speech_nasa-moonbase.wav 0 180 11.5 "00:15:00-00:21:00"

# 4. Public speech: commencement address, trimmed to a continuous stretch.
build UgUntNCldAY 2025-07-08_speech_commencement-northwestern.wav 200 180 5.9

if [[ "$UPLOAD" == "1" ]]; then
  upload "$OUT/2022-09-07_911_call_phoenix.wav" phone_call "2022-09-07T12:00:00-07:00"
  upload "$OUT/2025-11-15_sport_football-championship.wav" sports "2025-11-15T19:00:00-05:00"
  upload "$OUT/2026-05-26_speech_nasa-moonbase.wav" speech "2026-05-26T14:00:00-04:00"
  upload "$OUT/2025-07-08_speech_commencement-northwestern.wav" speech "2025-07-08T10:00:00-05:00"
fi

log "done — clips in $OUT"
