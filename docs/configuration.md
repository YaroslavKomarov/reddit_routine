[← Архитектура конвейера](architecture.md) · [Back to README](../README.md) · [Эксплуатация →](operations.md)

# Конфигурация

## `.env` (секреты)

Скопировать `.env.example` в `.env` и заполнить:

| Переменная | Назначение |
|-----------|-----------|
| `TELEGRAM_BOT_TOKEN` | Токен бота (получить у @BotFather) |
| `TELEGRAM_CHAT_ID` | ID чата/пользователя для дайджеста |
| `ANTHROPIC_API_KEY` (опционально) | Оставить **пустым** — тогда `claude -p` идёт по подписке владельца (`claude /login` на VPS). Заполнять только для сознательной оплаты через API. Пустая строка = «не задано»; не вписывать пробел/значение просто так |
| `AGENT_MAX_TURNS` (опционально) | Override лимита ходов агента поверх `config.yaml` |
| `AGENT_MAX_BUDGET` (опционально) | Override бюджета агента поверх `config.yaml` |

Reddit-credentials не нужны — сбор постов идёт через публичный анонимный
Atom-фид, без OAuth.

## `config.yaml`

Список сабреддитов (промо-настройки, кулдауны) и лимиты конвейера
(`fetch.window_hours`, `fetch.posts_per_sub_limit`, `agent.max_turns`,
`agent.max_budget_usd`, `telegram.send_time`). Полная схема полей —
reddit-routine-spec.md, раздел 3.

## Env-переопределения путей

Все пути файлов-контрактов конвейера переопределяются через env (используется
в тестах, дефолты — `data/tmp/` и `context/`):

| Переменная | Дефолт |
|-----------|--------|
| `POSTS_BATCH_PATH` | `data/tmp/posts_batch.json` |
| `AGENT_INPUT_PATH` | `data/tmp/agent_input.json` |
| `AGENT_RAW_PATH` | `data/tmp/agent_raw.json` |
| `DIGEST_PATH` | `data/tmp/digest.json` |
| `CONTEXT_DIR` | `context/` |
| `ROUTINE_DB_PATH` | `data/routine.db` |

Уровень логов — переменной `LOG_LEVEL` (например, `LOG_LEVEL=INFO`, дефолт
`DEBUG`).

## See Also

- [Установка и деплой](getting-started.md) — где и когда заполнять `.env`/`config.yaml`
- [Архитектура конвейера](architecture.md) — какой шаг читает какую переменную
- [Эксплуатация](operations.md) — промо-логирование, статус, критерий приёмки
