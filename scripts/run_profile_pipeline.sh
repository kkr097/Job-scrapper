#!/usr/bin/env bash
set -Eeuo pipefail

PROFILE="${1:-}"
if [[ "$PROFILE" != "kk" && "$PROFILE" != "sandra" ]]; then
  echo "Usage: $0 <kk|sandra>" >&2
  exit 64
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PIPELINE_PYTHON:-$REPO_ROOT/.venv/bin/python}"
exec "$PYTHON_BIN" "$REPO_ROOT/pipeline_orchestrator.py" --profile "$PROFILE"
