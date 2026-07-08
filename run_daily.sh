#!/usr/bin/env bash
# Оркестратор ежедневного прогона (спека, раздел 4).
# Последовательность: fetch_posts → build_agent_input → run_agent → parse_agent_output → send_telegram.
# Ошибка любого шага → статус в run_log + короткое уведомление в Telegram + exit 1.
# Флаг --dry-run: дайджест печатается в stdout вместо отправки, итоговый run_log не пишется.
# PYTHON_BIN: переопределение python-бинаря (на Windows-разработке, например PYTHON_BIN=py).
set -uo pipefail  # без -e: коды выхода шагов обрабатываются явно через ||
cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "[run_daily] ERROR: .env not found — скопируй .env.example в .env и заполни секреты" >&2
    exit 1
fi
source .env

mkdir -p data/tmp logs
LOG_FILE="logs/$(date +%F).log"
PYTHON_BIN="${PYTHON_BIN:-python}"

log() {
    echo "[run_daily] $* ts=$(date -u +%FT%TZ)" | tee -a "$LOG_FILE"
}

# Защита от наложения запусков; flock есть только на VPS —
# на Windows/Git Bash предупреждаем и продолжаем без лока.
if command -v flock >/dev/null 2>&1; then
    exec 9>data/.lock
    if ! flock -n 9; then
        log "already running (lock data/.lock busy), exiting"
        exit 0
    fi
else
    log "WARN: flock not available — running without lock (dev-среда?)"
fi

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=1
    log "dry-run mode: дайджест уйдёт в stdout, run_log не пишется"
fi

# Запуск шага с маркерами start/exit и дублированием вывода (включая
# stderr с DEBUG-логами шага) в лог-файл; код выхода — через PIPESTATUS.
run_step() {
    local name="$1"
    shift
    log "step=$name start"
    "$@" 2>&1 | tee -a "$LOG_FILE"
    local code="${PIPESTATUS[0]}"
    log "step=$name exit=$code"
    return "$code"
}

# Ошибка шага: сначала статус в run_log, потом уведомление — запись
# статуса не должна пропасть, если Telegram сам недоступен.
fail() {
    local status="$1" db_error="$2" tg_message="$3"
    "$PYTHON_BIN" src/db.py --log-run "$status" --error "$db_error" 2>&1 | tee -a "$LOG_FILE"
    "$PYTHON_BIN" src/send_telegram.py --error "$tg_message" 2>&1 | tee -a "$LOG_FILE"
    log "finished with status=$status"
    exit 1
}

run_step "fetch_posts" "$PYTHON_BIN" src/fetch_posts.py \
    || fail fetch_failed "fetch_posts failed" "fetch failed"
run_step "build_agent_input" "$PYTHON_BIN" src/build_agent_input.py \
    || fail fetch_failed "build_agent_input failed" "build input failed"
run_step "run_agent" bash src/run_agent.sh \
    || fail agent_failed "run_agent failed" "agent failed"
run_step "parse_agent_output" "$PYTHON_BIN" src/parse_agent_output.py \
    || fail agent_failed "parse_agent_output failed" "agent output invalid"

SEND_ARGS=()
if [ "$DRY_RUN" = "1" ]; then
    SEND_ARGS=(--dry-run)
fi
if ! run_step "send_telegram" "$PYTHON_BIN" src/send_telegram.py "${SEND_ARGS[@]}"; then
    # уведомление в Telegram не шлём: канал и так не работает — владелец
    # увидит отсутствие дайджеста, запись tg_failed в run_log и лог-файл
    "$PYTHON_BIN" src/db.py --log-run tg_failed --error "telegram send failed" 2>&1 | tee -a "$LOG_FILE"
    log "finished with status=tg_failed"
    exit 1
fi

log "finished ok"
