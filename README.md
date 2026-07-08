# Reddit Routine

Система ежедневного Reddit-дайджеста для продвижения Chrome-расширения (CWS).
Каждый день по cron собирает свежие посты из отслеживаемых сабреддитов, прогоняет
их через Claude Code для отбора релевантных постов и черновиков комментариев, и
присылает готовый дайджест владельцу в Telegram.

**Система ничего не постит сама.** Дайджест — это черновики для ручной публикации.
Владелец сам решает, что и куда постить.

Полная спецификация: [reddit-routine-spec.md](reddit-routine-spec.md).

## Требования

- Python 3
- Claude Code на VPS (для headless-вызовов агента):
  ```
  npm install -g @anthropic-ai/claude-code
  ```

## Установка и деплой на VPS

Ниже — полный путь от чистого Ubuntu/Debian VPS до первого боевого прогона по
cron. Подробности отдельных шагов конвейера — в `reddit-routine-spec.md`
(разделы 5.3, 5.8, 5.9).

1. **Подготовка системы:**

   ```bash
   sudo apt update
   sudo apt install -y python3 python3-pip python3-venv nodejs npm
   ```

2. **Установка Claude Code** (нужен для headless-вызова агента, шаг `run_agent.sh`):

   ```bash
   npm install -g @anthropic-ai/claude-code
   claude --version
   ```

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

6. **Регистрация Reddit-приложения.** На <https://www.reddit.com/prefs/apps>
   создать приложение типа **script**: `client_id` — строка под названием
   приложения, `client_secret` — поле secret. Это read-only доступ
   (application-only OAuth, `client_credentials`) без прав публикации —
   значения понадобятся в `.env` на следующем шаге.

7. **Секреты:**

   ```bash
   cp .env.example .env
   ```

   Заполнить `.env` реальными значениями: `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`, `REDDIT_CLIENT_ID`,
   `REDDIT_CLIENT_SECRET` (см. комментарии в `.env.example`).
   Headless-вызов `claude --bare` (шаг `run_agent.sh`) аутентифицируется через
   `ANTHROPIC_API_KEY` из `.env`.

8. **Наполнение владельцем** — см. раздел «Что нужно заполнить владельцу» ниже
   (`context/product.md`, `context/rules/<sub>.md`, `context/tone.md`,
   `config.yaml`, очередь вопросов).

9. **Smoke-проверка перед cron:**

   ```bash
   PYTHON_BIN=python3 ./run_daily.sh --dry-run
   ```

   Важно: `--dry-run` — это **полный** конвейер минус отправка в Telegram. Он
   реально обращается к Reddit и вызывает платного агента (нужен валидный
   `ANTHROPIC_API_KEY`, тратится бюджет `--max-budget-usd`), но **не пишет**
   строку в `run_log` — готовый дайджест печатается в stdout.

10. **Первый боевой прогон вручную:**

    ```bash
    PYTHON_BIN=python3 ./run_daily.sh
    ```

    Логи смотреть в `logs/YYYY-MM-DD.log` (детальный лог шагов) и
    `logs/cron.log` (вывод самого cron-запуска).

11. **Cron.** Пример строки (см. также раздел «Cron» ниже):

    ```
    0 9 * * * cd /opt/reddit-routine && PYTHON_BIN=/opt/reddit-routine/.venv/bin/python ./run_daily.sh >> logs/cron.log 2>&1
    ```

    Время выполняется по таймзоне VPS, а не по локальной таймзоне владельца —
    при необходимости сменить таймзону сервера (`timedatectl`) или
    скорректировать время в самой cron-строке.

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

## Cron

Пример строки cron (см. reddit-routine-spec.md, раздел 5.9; см. также шаг 11
раздела «Установка и деплой на VPS» выше про `PYTHON_BIN`):

```
0 9 * * * cd /opt/reddit-routine && PYTHON_BIN=/opt/reddit-routine/.venv/bin/python ./run_daily.sh >> logs/cron.log 2>&1
```

Если зависимости стоят системно (без venv), `PYTHON_BIN` можно не задавать —
тогда используется дефолт `python` (должен резолвиться на сервере).

Время выполняется по таймзоне VPS, а не по локальной таймзоне владельца — при
необходимости поменять таймзону сервера (`timedatectl`) или скорректировать
время в cron.

## После публикации промо-коммента

Система не знает, что вы реально запостили — залогируйте это вручную:

```
python src/question_queue.py log-promo <subreddit> comment_promo
```

См. reddit-routine-spec.md, раздел 5.7.

## Разработка

Разработка ведётся на Windows, деплой-таргет — Linux VPS (Ubuntu/Debian).

Запуск тестов:

```bash
python -m unittest discover tests
```

## Слой данных (`src/db.py`)

Единственная точка доступа к SQLite (`data/routine.db`). Миграции — идемпотентный
`CREATE TABLE IF NOT EXISTS` при каждом подключении, без системы версий. Путь к
БД можно переопределить через `ROUTINE_DB_PATH` (используется в тестах).

Запись статуса прогона (вызывается оркестратором `run_daily.sh`, см.
reddit-routine-spec.md, раздел 5.8):

```bash
python src/db.py --log-run ok --posts-fetched 12 --posts-suggested 5 --cost-usd 0.08
```

Допустимые статусы: `ok | fetch_failed | agent_failed | tg_failed`.

## Сбор постов (`src/fetch_posts.py`)

Работает через официальный read-only OAuth API Reddit: получает
application-only токен (`client_credentials`, credentials из
`REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` в `.env`, без прав записи), затем
тянет `oauth.reddit.com/r/{sub}/new` по каждому сабреддиту из `config.yaml`
(честный User-Agent, ретраи с backoff 2s/8s/30s на 429/5xx и сетевые ошибки,
пауза 2с между сабами), фильтрует по окну времени, `seen_posts`, `stickied` и
`removed_by_category`, пишет батч в `data/tmp/posts_batch.json`. Причина
перехода с анонимного `/new.json`: Reddit отдаёт 403 на запросы с
датацентровых IP (VPS):

```bash
python src/fetch_posts.py
```

Код выхода `1`, только если **все** сабреддиты не удалось опросить — частичный
отказ или пустой батч после фильтрации ошибкой не считаются.

## Очередь вопросов (`src/question_queue.py`)

```bash
python src/question_queue.py add            # интерактивно
python src/question_queue.py add --file questions.md   # батчем, блоки через ---
python src/question_queue.py list            # неиспользованные вопросы
python src/question_queue.py pop             # достать самый старый и пометить использованным
python src/question_queue.py stats           # unused / used
python src/question_queue.py log-promo <subreddit> comment_promo [--url ...]
```

## Вход агента (`src/build_agent_input.py`)

Собирает единый JSON для агента из батча постов, файлов `context/` и стейта БД
(промо-кулдауны AND `promo_allowed` из `config.yaml`, вопрос дня из очереди) и
пишет его в `data/tmp/agent_input.json`:

```bash
python src/build_agent_input.py
```

`context/product.md` и `context/tone.md` обязательны — без них шаг падает с кодом
`1`. Отсутствующий `context/rules/<sub>.md` не ошибка: подставляется заглушка
«только безопасные немаркетинговые комментарии» с WARN в лог.

**Внимание:** вопрос дня помечается использованным уже на этом шаге. Если прогон
упадёт дальше, вопрос потрачен — пере-добавить через `question_queue.py add`.

## Вызов агента (`src/run_agent.sh`)

Headless-вызов Claude Code строго с `--bare`, `--max-turns`, `--max-budget-usd`
(раздел 5.3 спеки). Промпт — `prompts/daily_digest.md`, вход — stdin из
`agent_input.json`, выход — `data/tmp/agent_raw.json`:

```bash
bash src/run_agent.sh
```

Лимиты берутся из env `AGENT_MAX_TURNS` / `AGENT_MAX_BUDGET` (`.env`), а при
пустых значениях — из `config.yaml` через mini-CLI:

```bash
python src/config.py agent.max_turns    # напечатает значение ключа
```

## Парсинг ответа (`src/parse_agent_output.py`)

Извлекает `result` и `total_cost_usd` из конверта `--output-format json`,
снимает markdown-обёртки (`` ```json ``), валидирует схему (обязательные поля,
`is_promo` — строго bool,
`post_id` из батча). При невалидном ответе — одна повторная попытка вызова
`run_agent.sh` с уточнением промпта, после второй неудачи — код выхода `1`:

```bash
python src/parse_agent_output.py
```

При успехе пишет предложенные посты в `seen_posts` и кладёт
`data/tmp/digest.json` (`{"digest": ..., "stats": {cost_usd, posts_fetched,
posts_suggested}}`) — вход для `send_telegram.py` (фаза 3). Промо-нарушения
(например `is_promo=true` в сабе на кулдауне) — WARN в лог, не ошибка: владелец
видит и решает сам.

## Env-переопределения путей

Все пути файлов-контрактов конвейера переопределяются через env (используется в
тестах, дефолты — `data/tmp/` и `context/`):

| Переменная | Дефолт |
|-----------|--------|
| `POSTS_BATCH_PATH` | `data/tmp/posts_batch.json` |
| `AGENT_INPUT_PATH` | `data/tmp/agent_input.json` |
| `AGENT_RAW_PATH` | `data/tmp/agent_raw.json` |
| `DIGEST_PATH` | `data/tmp/digest.json` |
| `CONTEXT_DIR` | `context/` |
| `ROUTINE_DB_PATH` | `data/routine.db` |

## Статус компонентов

- Скелет проекта и конфигурация — готово (этап 1)
- Слой данных `db.py` — готово
- Сбор постов `fetch_posts.py` и очередь вопросов `question_queue.py` — готово
- Вход и агент (`build_agent_input.py`, `prompts/daily_digest.md`, `run_agent.sh`,
  `parse_agent_output.py`) — готово (фаза 2); приёмка на реальном batch — ручной
  прогон по критерию milestone
- Доставка в Telegram и оркестрация (`send_telegram.py`, `run_daily.sh`, cron) —
  готово (фаза 3)
- Деплой и приёмка — текущий этап: инструкция деплоя выше, критерий приёмки —
  см. раздел «Приёмка» ниже

## Приёмка

Критерий milestone «Деплой и приёмка»: **три ежедневных прогона подряд на
реальных данных без ручного вмешательства** (по cron или запущенные вручную),
без повторов постов между прогонами.

**Прогоны обязаны быть боевыми, не `--dry-run`.** Строку в `run_log` со
статусом `ok` и заполненным `cost_usd` пишет только `send_telegram.py` в
режиме реальной отправки (см. `send_telegram.py:343`) — dry-run-прогоны в
`run_log` не попадают и в зачёт приёмки не идут.

1. Дождаться (или запустить вручную) три последовательных боевых прогона —
   через cron или `PYTHON_BIN=... ./run_daily.sh` без флага `--dry-run`.
2. Проверить результат:

   ```bash
   python src/db.py --show-runs 3
   ```

   Ожидается три строки со статусом `ok` и заполненным `cost_usd`.
3. Проверить отсутствие повторов постов между прогонами. Дедупликация
   гарантируется структурно: `seen_posts.post_id` — `PRIMARY KEY`,
   `fetch_posts.py` пишет через `INSERT OR IGNORE` и фильтрует уже
   встреченные посты на входе. Быстрая ручная проверка — просмотреть
   присланные в Telegram дайджесты за все три дня и убедиться, что среди них
   нет повторяющихся постов.
4. При статусе `fetch_failed` / `agent_failed` / `tg_failed`:
   - посмотреть поле `error` в выводе `--show-runs` — короткая причина;
   - подробности — в лог-файле соответствующего дня, `logs/YYYY-MM-DD.log`
     (и `logs/cron.log`, если прогон шёл через cron).
