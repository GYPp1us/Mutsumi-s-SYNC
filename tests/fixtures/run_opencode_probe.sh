#!/usr/bin/env bash
set -euo pipefail

BASE="${MUTSUMI_PROBE_ROOT:-/tmp/mutsumi-opencode-probe}"
OPENCODE_BIN="${OPENCODE_BIN:-/root/.opencode/bin/opencode}"
PROVIDER="${MUTSUMI_PROBE_PROVIDER:-stubprovider}"
PORT="${MUTSUMI_PROBE_PORT:-18080}"
TIMEOUT="${MUTSUMI_PROBE_TIMEOUT_SECONDS:-60}"
SDK_IMPORT="${OPENCODE_PLUGIN_SDK:-file:///root/.opencode/node_modules/@opencode-ai/plugin/dist/index.js}"

mkdir -p "$BASE/workspace" "$BASE/run" "$BASE/xdg-config/opencode"
rm -f "$BASE/report.json" "$BASE/server.log" "$BASE/run/opencode.jsonl"

sed "s#file:///root/.config/opencode/node_modules/@opencode-ai/plugin/dist/index.js#$SDK_IMPORT#" \
  "$(dirname "$0")/opencode_report_probe.mjs" > "$BASE/report-plugin.mjs"
cp "$(dirname "$0")/opencode_stub_server.py" "$BASE/stub.py"

cat > "$BASE/xdg-config/opencode/opencode.json" <<EOF
{
  "model": "$PROVIDER/stub",
  "plugin": ["$BASE/report-plugin.mjs"],
  "provider": {
    "$PROVIDER": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://127.0.0.1:$PORT/v1",
        "apiKey": "stub"
      },
      "models": {
        "stub": {
          "name": "stub",
          "limit": {"context": 128000, "output": 4096}
        }
      }
    }
  }
}
EOF

if command -v git >/dev/null 2>&1; then
  git -C "$BASE/workspace" init -q 2>/dev/null || true
fi

MUTSUMI_PROBE_PORT="$PORT" nohup python3 "$BASE/stub.py" \
  >"$BASE/server.log" 2>&1 < /dev/null &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT
sleep 1

set +e
env \
  XDG_CONFIG_HOME="$BASE/xdg-config" \
  XDG_DATA_HOME="$BASE/xdg-data" \
  XDG_STATE_HOME="$BASE/xdg-state" \
  XDG_CACHE_HOME="$BASE/xdg-cache" \
  OPENCODE_DISABLE_PROJECT_CONFIG=1 \
  OPENCODE_DISABLE_MODELS_FETCH=1 \
  OPENCODE_DISABLE_AUTOUPDATE=1 \
  MUTSUMI_REPORT_PATH="$BASE/report.json" \
  MUTSUMI_REPORT_TOKEN=probe-token \
  timeout "${TIMEOUT}s" "$OPENCODE_BIN" run --print-logs --format json \
    --dir "$BASE/workspace" --auto --model "$PROVIDER/stub" \
    --title "Mutsumi OpenCode probe" \
    "Complete the probe by calling report_task with report=probe complete." \
    < /dev/null >"$BASE/run/opencode.jsonl" 2>&1
status=$?
set -e

echo "EXIT=$status"
echo "REPORT"
cat "$BASE/report.json" 2>/dev/null || true
echo "OPENCODE"
tail -120 "$BASE/run/opencode.jsonl" || true
echo "SERVER"
cat "$BASE/server.log" || true

if [ "$status" -ne 0 ] || [ ! -s "$BASE/report.json" ]; then
  exit 1
fi
