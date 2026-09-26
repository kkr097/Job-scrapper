#!/usr/bin/env bash
set -Eeuo pipefail

MODE="run"
case "${1:-}" in
  "") ;;
  --validate-only) MODE="validate" ;;
  --preflight-only) MODE="preflight" ;;
  *) echo "Usage: $0 [--validate-only|--preflight-only]" >&2; exit 64 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$REPO_ROOT/logs"
LOCK_FILE="$LOG_DIR/weekday_pipeline.lock"
SUMMARY_LOG="$LOG_DIR/weekday_pipeline_summary.log"
RUN_LOG="$LOG_DIR/weekday_pipeline_$(date +%Y-%m-%d).log"
PYTHON_BIN="${PIPELINE_PYTHON:-$REPO_ROOT/.venv/bin/python}"
MAIN_SCRIPT="${PIPELINE_MAIN_SCRIPT:-$REPO_ROOT/main.py}"
ORCHESTRATOR_SCRIPT="${PIPELINE_ORCHESTRATOR_SCRIPT:-$REPO_ROOT/pipeline_orchestrator.py}"
MODEL_KEY="${LMSTUDIO_MODEL_KEY:-gemma-4-26b-a4b-it-qat}"
MODEL_IDENTIFIER="${LMSTUDIO_MODEL_IDENTIFIER:-gemma-4-26b-a4b-it-qat}"
LMSTUDIO_PORT="${LMSTUDIO_PORT:-1234}"
SCRAPE_TIMEOUT_SECONDS="${SCRAPE_TIMEOUT_SECONDS:-8400}"
SCORE_TIMEOUT_SECONDS="${SCORE_TIMEOUT_SECONDS:-8400}"

mkdir -p "$LOG_DIR"
touch "$RUN_LOG" "$SUMMARY_LOG" "$LOCK_FILE"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(date --iso-8601=seconds) status=skipped reason=pipeline_already_running" | tee -a "$RUN_LOG" "$SUMMARY_LOG"
  exit 75
fi

START_EPOCH="$(date +%s)"
PIPELINE_STATUS="failed"
PIPELINE_ERROR="unknown"
SCRAPE_SECONDS=0
SCORE_SECONDS=0
QUEUED_BEFORE_SCORE="unknown"
SERVER_STARTED=0
MODEL_LOADED=0

log() {
  printf '%s %s\n' "$(date --iso-8601=seconds)" "$*" | tee -a "$RUN_LOG"
}

count_csv_rows() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    printf '0'
    return
  fi
  "$PYTHON_BIN" -c 'import csv,sys; print(sum(1 for _ in csv.DictReader(open(sys.argv[1], encoding="utf-8", newline=""))))' "$path"
}

line_count_without_header() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    printf '0'
    return
  fi
  local lines
  lines="$(wc -l < "$path")"
  (( lines > 0 )) && printf '%s' "$((lines - 1))" || printf '0'
}

discover_lms() {
  if [[ -n "${LMS_EXE:-}" && -x "$LMS_EXE" ]]; then
    printf '%s' "$LMS_EXE"
    return
  fi
  local windows_profile
  windows_profile="$(cmd.exe /d /c echo %USERPROFILE% 2>/dev/null | tr -d '\r' | tail -n 1)"
  local candidate
  candidate="$(wslpath -u "${windows_profile}\\.lmstudio\\bin\\lms.exe")"
  [[ -x "$candidate" ]] || return 1
  printf '%s' "$candidate"
}

discover_windows_host() {
  local gateway
  gateway="$(ip route show default 2>/dev/null | awk 'NR==1 {print $3}')"
  if [[ -z "$gateway" ]]; then
    gateway="$(awk '/^nameserver / {print $2; exit}' /etc/resolv.conf)"
  fi
  [[ -n "$gateway" ]] || return 1
  printf '%s' "$gateway"
}

server_is_running() {
  local status
  status="$($LMS_EXE server status 2>&1 || true)"
  if grep -qi 'not running' <<<"$status"; then
    return 1
  fi
  grep -qi 'running' <<<"$status"
}

model_is_loaded() {
  "$LMS_EXE" ps 2>&1 | grep -Fq "$MODEL_IDENTIFIER"
}

cleanup() {
  local exit_code=$?
  set +e
  if (( MODEL_LOADED == 1 )); then
    log "cleanup=unload_model identifier=$MODEL_IDENTIFIER"
    "$LMS_EXE" unload "$MODEL_IDENTIFIER" >>"$RUN_LOG" 2>&1
  fi
  if (( SERVER_STARTED == 1 )); then
    log "cleanup=stop_lmstudio_server"
    "$LMS_EXE" server stop >>"$RUN_LOG" 2>&1
  fi
  local finished elapsed pending scored matches nonmatches rejected failed_requests source_summary
  finished="$(date --iso-8601=seconds)"
  elapsed="$(( $(date +%s) - START_EPOCH ))"
  pending="$(count_csv_rows "$REPO_ROOT/score_pending_jobs.csv" 2>/dev/null || echo unknown)"
  scored="unknown"
  if [[ "$QUEUED_BEFORE_SCORE" =~ ^[0-9]+$ && "$pending" =~ ^[0-9]+$ ]]; then
    if (( QUEUED_BEFORE_SCORE >= pending )); then
      scored="$((QUEUED_BEFORE_SCORE - pending))"
    fi
  fi
  matches="$(count_csv_rows "$REPO_ROOT/daily_jobs.csv" 2>/dev/null || echo unknown)"
  nonmatches="$(count_csv_rows "$REPO_ROOT/daily_jobs_nonmatch.csv" 2>/dev/null || echo unknown)"
  rejected="$(count_csv_rows "$REPO_ROOT/rejected_jobs.csv" 2>/dev/null || echo unknown)"
  failed_requests="$(line_count_without_header "$REPO_ROOT/failed_requests_log.csv" 2>/dev/null || echo unknown)"
  source_summary="$(tail -n 1 "$REPO_ROOT/daily_summary_log.csv" 2>/dev/null | tr '\r\n' ' ' || true)"
  printf '%s status=%s exit_code=%s error=%q elapsed_sec=%s scrape_sec=%s score_sec=%s queued=%s scored=%s pending=%s matches=%s nonmatches=%s rejected=%s failed_requests=%s source_summary=%q\n' \
    "$finished" "$PIPELINE_STATUS" "$exit_code" "$PIPELINE_ERROR" "$elapsed" "$SCRAPE_SECONDS" "$SCORE_SECONDS" \
    "$QUEUED_BEFORE_SCORE" "$scored" "$pending" "$matches" "$nonmatches" "$rejected" "$failed_requests" "$source_summary" >>"$SUMMARY_LOG"
}
on_signal() {
  PIPELINE_ERROR="signal_received"
  exit 130
}
trap cleanup EXIT
trap on_signal INT TERM

fail() {
  PIPELINE_ERROR="$1"
  log "status=failed reason=$1"
  exit "${2:-1}"
}

for command in flock timeout curl cmd.exe wslpath ip awk; do
  command -v "$command" >/dev/null 2>&1 || fail "missing_command_$command" 69
done
[[ -x "$PYTHON_BIN" ]] || fail "python_not_executable" 69
[[ -f "$MAIN_SCRIPT" ]] || fail "main_script_missing" 69
[[ -f "$ORCHESTRATOR_SCRIPT" ]] || fail "orchestrator_script_missing" 69
LMS_EXE="$(discover_lms)" || fail "lms_executable_missing" 69
WSL_HOST="$(discover_windows_host)" || fail "windows_host_not_found" 69
export LMSTUDIO_API_BASE="http://${WSL_HOST}:${LMSTUDIO_PORT}/v1"
HEALTH_URL="http://${WSL_HOST}:${LMSTUDIO_PORT}/api/v1/models"

log "mode=$MODE repo=$REPO_ROOT lmstudio_base=$LMSTUDIO_API_BASE"
if [[ "$MODE" == "validate" ]]; then
  PIPELINE_STATUS="validated"
  PIPELINE_ERROR="none"
  exit 0
fi

if ! server_is_running; then
  log "action=start_lmstudio_server port=$LMSTUDIO_PORT bind=$WSL_HOST"
  "$LMS_EXE" server start --port "$LMSTUDIO_PORT" --bind "$WSL_HOST" >>"$RUN_LOG" 2>&1 || fail "lmstudio_server_start_failed" 70
  SERVER_STARTED=1
fi

if ! model_is_loaded; then
  log "action=load_model model=$MODEL_KEY identifier=$MODEL_IDENTIFIER"
  "$LMS_EXE" load "$MODEL_KEY" --identifier "$MODEL_IDENTIFIER" -y >>"$RUN_LOG" 2>&1 || fail "lmstudio_model_load_failed" 70
  MODEL_LOADED=1
fi

healthy=0
for attempt in 1 2 3 4 5 6; do
  if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  log "lmstudio_health_retry=$attempt"
  sleep 5
done
(( healthy == 1 )) || fail "lmstudio_health_check_failed" 70
log "lmstudio_health=ok model=$MODEL_IDENTIFIER"

if [[ "$MODE" == "preflight" ]]; then
  PIPELINE_STATUS="preflight_ok"
  PIPELINE_ERROR="none"
  exit 0
fi

cd "$REPO_ROOT"
pipeline_started="$(date +%s)"
log "phase=multi_profile_pipeline status=started profiles=kk,sandra"
set +e
total_timeout="$((SCRAPE_TIMEOUT_SECONDS + SCORE_TIMEOUT_SECONDS * 2))"
timeout --signal=TERM --kill-after=30s "${total_timeout}s" "$PYTHON_BIN" "$ORCHESTRATOR_SCRIPT" --profile all 2>&1 | tee -a "$RUN_LOG"
pipeline_exit=${PIPESTATUS[0]}
set -e
if (( pipeline_exit != 0 )); then
  [[ $pipeline_exit -eq 124 ]] && fail "multi_profile_pipeline_timeout" 124
  fail "multi_profile_pipeline_failed_exit_$pipeline_exit" "$pipeline_exit"
fi
SCORE_SECONDS="$(( $(date +%s) - pipeline_started ))"
log "phase=multi_profile_pipeline status=completed elapsed_sec=$SCORE_SECONDS"

PIPELINE_STATUS="success"
PIPELINE_ERROR="none"
log "status=success"
