#!/usr/bin/env bash
# Шаг конвейера: headless-вызов Claude Code (спека 5.3).
# Читает data/tmp/agent_input.json (stdin агента), пишет data/tmp/agent_raw.json.
# Лимиты: env AGENT_MAX_TURNS/AGENT_MAX_BUDGET из .env, иначе дефолты config.yaml.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    echo "[run_agent] ERROR: .env not found — скопируй .env.example в .env и заполни секреты" >&2
    exit 1
fi
source .env

PYTHON_BIN="${PYTHON_BIN:-python}"

# Пустая env-строка считается «не задано» (в .env.example ключи пустые)
if [ -n "${AGENT_MAX_TURNS:-}" ]; then
    MAX_TURNS="$AGENT_MAX_TURNS"
    TURNS_SOURCE="env"
else
    MAX_TURNS="$("$PYTHON_BIN" src/config.py agent.max_turns)"
    TURNS_SOURCE="config.yaml"
fi
if [ -n "${AGENT_MAX_BUDGET:-}" ]; then
    MAX_BUDGET="$AGENT_MAX_BUDGET"
    BUDGET_SOURCE="env"
else
    MAX_BUDGET="$("$PYTHON_BIN" src/config.py agent.max_budget_usd)"
    BUDGET_SOURCE="config.yaml"
fi
MODEL_FLAG="$("$PYTHON_BIN" src/config.py agent.model_flag)"

INPUT="${AGENT_INPUT_PATH:-data/tmp/agent_input.json}"
OUTPUT="${AGENT_RAW_PATH:-data/tmp/agent_raw.json}"

PROMPT="$(cat prompts/daily_digest.md)"
RETRY_NOTE="${AGENT_RETRY_NOTE:-}"
if [ -n "$RETRY_NOTE" ]; then
    PROMPT="$PROMPT
$RETRY_NOTE"
fi

echo "[run_agent] python_bin=$PYTHON_BIN" >&2
echo "[run_agent] max_turns=$MAX_TURNS (source: $TURNS_SOURCE)" >&2
echo "[run_agent] max_budget_usd=$MAX_BUDGET (source: $BUDGET_SOURCE)" >&2
echo "[run_agent] model_flag='${MODEL_FLAG}' (пусто = дефолтная модель)" >&2
echo "[run_agent] input=$INPUT output=$OUTPUT" >&2
if [ -n "$RETRY_NOTE" ]; then
    echo "[run_agent] retry note present — повторный вызов после невалидного ответа" >&2
fi

MODEL_ARGS=()
if [ -n "$MODEL_FLAG" ]; then
    MODEL_ARGS=(--model "$MODEL_FLAG")
fi

# Без --bare: bare-режим не подхватывает подписочную сессию Claude.ai
# (проверено на 2.1.204) — а аутентификация идёт по подписке, не по API-ключу.
# Изоляция контекста обеспечена окружением VPS: CLAUDE.md/.claude/MCP в
# репозиторий не входят и на сервере отсутствуют.
claude -p "$PROMPT" \
    --allowedTools "Read" \
    --max-turns "$MAX_TURNS" \
    --max-budget-usd "$MAX_BUDGET" \
    "${MODEL_ARGS[@]}" \
    --output-format json \
    < "$INPUT" \
    > "$OUTPUT"

echo "[run_agent] done, raw response written to $OUTPUT" >&2
