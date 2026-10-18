#!/usr/bin/env bash
set -Eeuo pipefail

deploy_root="${MUTSUMI_DEPLOY_ROOT:-/opt/mutsumi-sync-v3}"
service_name="${MUTSUMI_SERVICE_NAME:-mutsumi-sync-v3.service}"
data_dir="$deploy_root/shared/data"
assume_yes=false
dry_run=false

usage() {
  cat <<'EOF'
Usage: reset_production_data.sh [--yes] [--dry-run]

Stop the Mutsumi SYNC service, remove all current shared bot data, and start
the service again. This clears the SQLite database, logs, media artifacts,
and their temporary files, but keeps code, configuration, and system prompts.

  --yes       Skip the interactive CLEAR confirmation.
  --dry-run   Show the target and current size without changing anything.
EOF
}

for arg in "$@"; do
  case "$arg" in
    --yes) assume_yes=true ;;
    --dry-run) dry_run=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "This script must run as root." >&2
  exit 1
fi

if [[ "$data_dir" != "$deploy_root/shared/data" ]]; then
  echo "Refusing to use an unexpected data path: $data_dir" >&2
  exit 1
fi

if [[ ! -d "$data_dir" ]]; then
  echo "Data directory does not exist: $data_dir" >&2
  exit 1
fi

echo "Target: $data_dir"
echo "Current size: $(du -sh -- "$data_dir" | awk '{print $1}')"

if [[ "$dry_run" == true ]]; then
  echo "Dry run: no service or data changes made."
  exit 0
fi

if [[ "$assume_yes" != true ]]; then
  read -r -p "Type CLEAR to delete all current bot data: " confirmation
  if [[ "$confirmation" != "CLEAR" ]]; then
    echo "Cancelled."
    exit 0
  fi
fi

was_active=false
if systemctl is-active --quiet "$service_name"; then
  was_active=true
fi

echo "Stopping $service_name..."
systemctl stop "$service_name"

restore_service() {
  if [[ "$was_active" == true ]] && ! systemctl is-active --quiet "$service_name"; then
    echo "Cleanup failed; attempting to restore $service_name..." >&2
    systemctl start "$service_name" || true
  fi
}
trap restore_service ERR

echo "Removing shared bot data..."
find "$data_dir" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +

echo "Starting $service_name..."
systemctl start "$service_name"
trap - ERR

systemctl is-active --quiet "$service_name"
echo "Reset complete. Service is active."
systemctl status "$service_name" --no-pager -l
