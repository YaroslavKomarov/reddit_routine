[Back to README](../README.md) · [Архитектура конвейера →](architecture.md)

# Установка и деплой

Полный путь от чистого Ubuntu/Debian VPS до первого боевого прогона по cron.
Подробности отдельных шагов конвейера — в [reddit-routine-spec.md](../reddit-routine-spec.md)
(разделы 5.3, 5.8, 5.9).

## Требования

- Python 3
- Claude Code на VPS (для headless-вызовов агента):
  ```
  npm install -g @anthropic-ai/claude-code
  ```

## Шаги деплоя

1. **Подготовка системы:**

   ```bash
   sudo apt update
   sudo apt install -y python3 python3-pip python3-venv nodejs npm
   ```

2. **Установка Claude Code и логин по подписке** (нужен для headless-вызова
   агента, шаг `run_agent.sh`):

   ```bash
   npm install -g @anthropic-ai/claude-code
   claude --version
   claude        # интерактивно: выбрать вход через аккаунт Claude.ai (подписка)
   ```

   При `claude` CLI выдаст ссылку — открыть в браузере на любом устройстве,
   авторизоваться под аккаунтом с активной подпиской Pro/Max, вставить код
   обратно в терминал. Логиниться нужно под тем же пользователем, от которого
   пойдёт cron (сессия хранится в его `~/.claude/`). После входа — `/exit`.

   > Важно: `run_agent.sh` вызывает `claude -p` **без** `--bare` — bare-режим
   > не подхватывает подписочную сессию (отвечает «Not logged in»). Поэтому
   > `ANTHROPIC_API_KEY` в `.env` оставляем пустым.

3. **Размещение проекта и виртуальное окружение** (например, в `/opt/reddit-routine`):

   ```bash
   cd /opt/reddit-routine
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. **Интерпретатор Python (важно для cron).** Cron не активирует venv сам —
   нужно явно задать единый интерпретатор, который увидят и `run_daily.sh`, и
   `run_agent.sh` (оба читают переменную `PYTHON_BIN`):

   - либо задать `PYTHON_BIN=/opt/reddit-routine/.venv/bin/python` в
     cron-строке (см. пример в разделе «Cron» ниже);
   - либо ставить зависимости системно (`pip install -r requirements.txt` без
     venv) и убедиться, что команда `python` резолвится на сервере (на
     стоковом Ubuntu/Debian её может не быть — только `python3`).

   Без этого шага прогон под cron может упасть с `fetch_failed`/`agent_failed`
   на первой же Python-команде.

5. **Права на запуск.** При разработке на Windows исполняемый бит теряется, а
   cron зовёт скрипты как `./run_daily.sh`:

   ```bash
   chmod +x run_daily.sh src/run_agent.sh
   ```

6. **Секреты:**

   ```bash
   cp .env.example .env
   ```

   Заполнить `.env` реальными значениями: `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID` (см. комментарии в `.env.example`). `ANTHROPIC_API_KEY`
   оставить **пустым** — headless-вызов `claude -p` (шаг `run_agent.sh`)
   аутентифицируется по подписке владельца (см. шаг с `claude /login` ниже).
   Reddit-credentials не нужны: сбор постов идёт через публичный анонимный
   Atom-фид.

7. **Наполнение владельцем** — см. раздел «Что нужно заполнить владельцу» ниже
   (`context/product.md`, `context/rules/<sub>.md`, `context/tone.md`,
   `config.yaml`, очередь вопросов).

8. **Smoke-проверка перед cron:**

   ```bash
   PYTHON_BIN=python3 ./run_daily.sh --dry-run
   ```

   Важно: `--dry-run` — это **полный** конвейер минус отправка в Telegram. Он
   реально обращается к Reddit и вызывает платного агента (нужен валидный
   `ANTHROPIC_API_KEY`, тратится бюджет `--max-budget-usd`), но **не пишет**
   строку в `run_log` — готовый дайджест печатается в stdout. Сам сбор постов
   занимает ориентировочно ~4 минуты на 8 сабреддитов из-за жёсткого
   анонимного rate limit Reddit (~1 запрос на ~30-секундное окно).

9. **Первый боевой прогон вручную:**

   ```bash
   PYTHON_BIN=python3 ./run_daily.sh
   ```

   Логи смотреть в `logs/YYYY-MM-DD.log` (детальный лог шагов) и
   `logs/cron.log` (вывод самого cron-запуска).

10. **Cron.** Пример строки:

    ```
    0 9 * * * cd /opt/reddit-routine && PYTHON_BIN=/opt/reddit-routine/.venv/bin/python ./run_daily.sh >> logs/cron.log 2>&1
    ```

    Если зависимости стоят системно (без venv), `PYTHON_BIN` можно не
    задавать — тогда используется дефолт `python` (должен резолвиться на
    сервере).

    Время выполняется по таймзоне VPS, а не по локальной таймзоне владельца —
    при необходимости сменить таймзону сервера (`timedatectl`) или
    скорректировать время в самой cron-строке. Пример строки также см. в
    reddit-routine-spec.md, раздел 5.9.

Управление уровнем логов — переменной окружения `LOG_LEVEL` (например,
`LOG_LEVEL=INFO`, дефолт `DEBUG`).

## Что нужно заполнить владельцу

- `context/product.md` — описание расширения (см. TODO-пометки внутри файла)
- `context/rules/<subreddit>.md` — правила каждого отслеживаемого сабреддита
  (скопировать из `context/rules/_template.md`)
- `context/tone.md` — при желании адаптировать дефолтный тон под свой голос
- `config.yaml` — список сабреддитов и их промо-настройки
- Очередь вопросов-постов — заполнить через `python src/question_queue.py add`
  (интерактивно) или `add --file questions.md` (батчем, блоки через `---`)

## Разработка

Разработка ведётся на Windows, деплой-таргет — Linux VPS (Ubuntu/Debian).

Запуск тестов:

```bash
python -m unittest discover tests
```

## See Also

- [Архитектура конвейера](architecture.md) — назначение и контракт каждого шага
- [Конфигурация](configuration.md) — env-переменные и пути файлов-контрактов
- [Эксплуатация](operations.md) — промо-логирование, статус, критерий приёмки
