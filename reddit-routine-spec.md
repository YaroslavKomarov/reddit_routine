# Reddit Routine — Implementation Spec

Спецификация для реализации системы ежедневной Reddit-рутины продвижения Chrome-расширения (CWS). Реализуется как **отдельный проект** (не в репозитории расширения). Деплой-таргет: Linux VPS (Ubuntu/Debian), запуск по cron.

---

## 1. Цель и принцип работы

Система каждый день в заданное время:

1. Достаёт из локальной очереди один заранее подготовленный "технический вопрос-пост" и назначает ему целевой сабреддит.
2. Собирает свежие посты (последние ~12 часов) из списка отслеживаемых сабреддитов через публичный Reddit JSON API.
3. Прогоняет собранные посты через Claude Code в headless-режиме (`claude -p --bare`): агент отбирает 3–5 релевантных постов на сабреддит и пишет черновики комментариев с учётом правил каждого сабреддита.
4. Отправляет владельцу дайджест в Telegram: пост дня + список постов с черновиками комментов.
5. Владелец постит вручную. Система **никогда не публикует ничего сама** — это жёсткое требование.

Система хранит стейт в SQLite: какие посты уже предлагались, когда и в какой сабреддит уходило промо (для соблюдения кулдаунов), очередь вопросов.

## 2. Структура репозитория

```
reddit-routine/
├── CLAUDE.md                  # контекст проекта для интерактивной разработки
├── README.md                  # как деплоить и эксплуатировать
├── config.yaml                # конфиг: сабреддиты, лимиты, расписание promo-кулдаунов
├── .env.example               # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY
├── requirements.txt           # requests, pyyaml, python-dotenv (минимум зависимостей)
├── run_daily.sh               # оркестратор: fetch → agent → format → send → commit state
├── src/
│   ├── fetch_posts.py         # сбор свежих постов из Reddit
│   ├── build_agent_input.py   # сборка входного JSON для агента (посты + стейт + правила)
│   ├── run_agent.sh           # обёртка над claude -p --bare
│   ├── parse_agent_output.py  # валидация JSON-ответа агента
│   ├── send_telegram.py       # форматирование и отправка дайджеста
│   ├── question_queue.py      # CLI для управления очередью вопросов (add/list/pop/stats)
│   └── db.py                  # SQLite-слой, миграции при первом запуске
├── context/
│   ├── product.md             # описание расширения: название, фичи, ссылка CWS, позиционирование
│   ├── tone.md                # тон комментариев, что можно/нельзя, анти-LLM-стиль гайдлайны
│   └── rules/
│       ├── _template.md       # шаблон файла правил сабреддита
│       └── <subreddit>.md     # по одному файлу на сабреддит (заполняет владелец)
├── prompts/
│   └── daily_digest.md        # системный промпт для ежедневного прогона
├── data/
│   └── routine.db             # SQLite (в .gitignore)
└── logs/                      # логи запусков (в .gitignore)
```

## 3. Конфиг (`config.yaml`)

```yaml
subreddits:
  - name: SEO
    promo_allowed: true          # можно ли вообще упоминать продукт в комментах
    promo_cooldown_days: 7       # минимум дней между промо-комментами в этом сабе
    question_posts_allowed: true # подходит ли для вопросов-постов
    review_post_allowed: false   # разрешён ли пост "смотрите что я сделал" (разовая акция)
  # ... остальные сабреддиты

fetch:
  window_hours: 12
  posts_per_sub_limit: 50        # сколько тянуть из /new.json
  min_post_score: 0              # фильтр мусора, 0 = без фильтра
  user_agent: "web:reddit-routine:v1.0 (personal digest tool)"

selection:
  posts_per_sub: [3, 5]          # диапазон отбора
  promo_ratio_target: "1 из 6"   # ориентир для агента: доля промо-комментов в дайджесте

agent:
  model_flag: ""                 # пусто = дефолтная модель; можно задать --model
  max_turns: 10
  max_budget_usd: 1.00

telegram:
  send_time: "09:00"             # информативно; фактическое время задаёт cron
  split_by_subreddit: true       # длинный дайджест бить на сообщения по сабам
```

## 4. Схема БД (SQLite, `data/routine.db`)

```sql
CREATE TABLE IF NOT EXISTS seen_posts (
    post_id      TEXT PRIMARY KEY,      -- reddit id, например "1abcde"
    subreddit    TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    suggested_at TEXT NOT NULL,         -- ISO8601 UTC
    was_promo    INTEGER DEFAULT 0      -- 1 если черновик был промо-комментом
);

CREATE TABLE IF NOT EXISTS promo_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subreddit  TEXT NOT NULL,
    type       TEXT NOT NULL CHECK(type IN ('comment_promo','question_post','review_post')),
    post_url   TEXT,
    logged_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS question_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,           -- полный текст поста-вопроса (title + body)
    target_sub TEXT,                    -- предпочтительный саб; NULL = агент выберет
    created_at TEXT NOT NULL,
    used_at    TEXT                     -- NULL = не использован
);

CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      TEXT NOT NULL,
    status      TEXT NOT NULL,          -- ok | fetch_failed | agent_failed | tg_failed
    posts_fetched INTEGER,
    posts_suggested INTEGER,
    cost_usd    REAL,                   -- из --output-format json (total_cost_usd)
    error       TEXT
);
```

Важно: **promo_history пополняется вручную** через `question_queue.py log-promo <sub> <type>` (или отдельную мини-команду), потому что система не знает, что владелец реально запостил. В README описать: "запостил промо — залогируй одной командой". Альтернатива (фаза 2): кнопка-callback в Telegram "✅ Запостил".

## 5. Компоненты

### 5.1 `fetch_posts.py`

- Для каждого сабреддита из конфига: GET `https://www.reddit.com/r/{sub}/new.json?limit={posts_per_sub_limit}` с User-Agent из конфига.
- Без OAuth. Ретраи: 3 попытки с экспоненциальным backoff (2s/8s/30s) на 429 и 5xx. Пауза 2 секунды между сабреддитами.
- Фильтрация: `created_utc` в пределах `window_hours`; отбросить посты, чей `id` уже есть в `seen_posts`; отбросить stickied и посты с `removed_by_category`.
- Выход: `data/tmp/posts_batch.json` — массив объектов `{id, subreddit, title, selftext (усечь до 2000 символов), url, permalink, score, num_comments, created_utc}`.
- Если хотя бы один сабреддит отдал данные — продолжаем; если все упали — статус `fetch_failed`, отправить в Telegram короткое сообщение об ошибке и выйти с ненулевым кодом.

### 5.2 `build_agent_input.py`

Собирает один входной JSON для агента:

```json
{
  "date": "2026-07-03",
  "product": "<содержимое context/product.md>",
  "tone": "<содержимое context/tone.md>",
  "subreddit_rules": { "SEO": "<content/rules/SEO.md>", "...": "..." },
  "promo_state": [
    {"subreddit": "SEO", "last_promo_days_ago": 4, "promo_allowed_today": false}
  ],
  "question_of_the_day": {"text": "...", "target_sub": "TechSEO"},
  "posts": [ ...из posts_batch.json... ],
  "selection_config": {"posts_per_sub": [3,5], "promo_ratio_target": "1 из 6"}
}
```

`promo_allowed_today` вычисляется кодом (детерминированно) из `promo_history` и `promo_cooldown_days` — агент получает готовый флаг, а не сырую историю.

`question_of_the_day`: `question_queue.py pop` — достать самый старый неиспользованный вопрос, пометить `used_at`. Если очередь пуста — в дайджест добавляется предупреждение "⚠️ Очередь вопросов пуста, пополни: `python src/question_queue.py add`".

### 5.3 `run_agent.sh` — headless-вызов Claude Code

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source .env

claude --bare -p "$(cat prompts/daily_digest.md)" \
  --allowedTools "Read" \
  --max-turns "${AGENT_MAX_TURNS:-10}" \
  --max-budget-usd "${AGENT_MAX_BUDGET:-1.00}" \
  --output-format json \
  < data/tmp/agent_input.json \
  > data/tmp/agent_raw.json
```

Ключевые моменты:
- `--bare` — не подтягивать локальные CLAUDE.md/хуки/MCP, детерминированный запуск. Аутентификация в bare-режиме идёт через `ANTHROPIC_API_KEY` из окружения — он должен быть в `.env`.
- `--output-format json` — из ответа берём поле `result` (текст агента) и `total_cost_usd` (пишем в `run_log`).
- Весь контекст передаётся через stdin одним JSON — агенту не нужны файловые инструменты, `Read` оставлен на всякий случай.

### 5.4 `prompts/daily_digest.md`

Промпт должен требовать от агента **строго JSON без markdown-обёрток** такого вида:

```json
{
  "question_post": {
    "subreddit": "TechSEO",
    "title": "...",
    "body": "...",
    "notes": "почему этот саб, на что обратить внимание"
  },
  "suggestions": [
    {
      "subreddit": "SEO",
      "posts": [
        {
          "post_id": "1abcde",
          "post_title": "...",
          "post_url": "https://reddit.com/...",
          "comment_draft": "...",
          "is_promo": false,
          "why": "одно предложение: почему этот пост и почему такой коммент"
        }
      ]
    }
  ],
  "skipped_subs": [{"subreddit": "bigseo", "reason": "нет релевантных постов за 12ч"}]
}
```

Содержательные требования к промпту (написать развёрнуто):
- Отбирать 3–5 постов на саб; если релевантных меньше — меньше, не натягивать.
- В каждом сабе, где `promo_allowed_today=true`, **желательно** (не обязательно) один пост с `is_promo=true` — где упоминание продукта является прямым ответом на проблему автора. Если такого поста нет — не выдумывать.
- Соблюдать общую пропорцию: промо-комменты — не более ~1 из 6 предложенных черновиков за день.
- Черновики писать по `tone.md`: короткие абзацы, без буллет-простыней, без "Great question!", без em-dash-стиля, конкретика > общие слова. Черновик — это заготовка, владелец дорабатывает.
- Строго следовать правилам сабреддита из `subreddit_rules` (ссылки запрещены → без ссылок; самопромо запрещено → `is_promo` только false для этого саба).
- В `comment_draft` для промо: сначала польза/ответ по существу, упоминание тула — вторично и естественно.

### 5.5 `parse_agent_output.py`

- Извлечь `result` из `agent_raw.json`, снять возможные ```json-обёртки, распарсить.
- Валидация схемы: обязательные поля, `is_promo` — bool, `post_id` существует в batch. При невалидном JSON — одна повторная попытка вызова агента с добавкой "предыдущий ответ не распарсился, верни строго JSON"; после второй неудачи — статус `agent_failed`, уведомление в Telegram.
- Записать все предложенные `post_id` в `seen_posts`.

### 5.6 `send_telegram.py`

- Отправка через `https://api.telegram.org/bot{TOKEN}/sendMessage`, `parse_mode=HTML` (не Markdown — меньше проблем с экранированием в черновиках).
- Формат дайджеста:
  - Сообщение 1: `📝 Пост дня → r/{sub}` + title + body вопроса + notes.
  - Далее по одному сообщению на сабреддит: `💬 r/{sub}`, затем для каждого поста: заголовок (ссылкой), 🔥 если `is_promo`, черновик коммента в `<blockquote>` или моноширинным блоком для удобного копирования, строка `why` курсивом.
  - Финальное сообщение: статистика (постов собрано/предложено, стоимость прогона, остаток очереди вопросов) + напоминание про `log-promo`, если сегодня есть 🔥.
- Лимит Telegram — 4096 символов на сообщение: длинные блоки резать по постам.

### 5.7 `question_queue.py` (CLI)

```
python src/question_queue.py add            # интерактивно или из файла: --file questions.md (парсить по разделителю ---)
python src/question_queue.py list           # неиспользованные, с id
python src/question_queue.py stats          # сколько осталось
python src/question_queue.py log-promo SEO comment_promo [--url ...]
```

Пополнение очереди — отдельная интерактивная сессия владельца с Claude Code (вне этой системы): нагенерить 20–30 вопросов, отревьюить, залить через `add --file`.

### 5.8 `run_daily.sh` (оркестратор)

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source .env
mkdir -p data/tmp logs

exec 9>data/.lock
flock -n 9 || { echo "already running"; exit 0; }   # защита от наложения запусков

python src/fetch_posts.py          || { python src/send_telegram.py --error "fetch failed"; exit 1; }
python src/build_agent_input.py
bash   src/run_agent.sh            || { python src/send_telegram.py --error "agent failed"; exit 1; }
python src/parse_agent_output.py   || { python src/send_telegram.py --error "agent output invalid"; exit 1; }
python src/send_telegram.py
python src/db.py --log-run ok
```

Каждый шаг логирует в `logs/YYYY-MM-DD.log`. Ошибки любого шага → короткое сообщение в Telegram (владелец должен узнать, что дайджеста сегодня не будет, а не гадать).

### 5.9 Cron

```
0 9 * * * cd /opt/reddit-routine && ./run_daily.sh >> logs/cron.log 2>&1
```

Время — по таймзоне VPS; в README указать, как поменять. `.env` должен подхватываться самим скриптом (cron не читает shell-профили).

## 6. Жёсткие ограничения (не нарушать при реализации)

1. **Никакой автопубликации.** Система не должна иметь кода, который постит/комментирует на Reddit. Никаких Reddit OAuth-токенов с правами на запись.
2. **Только чтение публичного JSON API** с честным User-Agent и паузами. Не обходить rate limit, не парсить HTML.
3. Секреты только в `.env` (в `.gitignore`), в репозитории — `.env.example`.
4. Агент вызывается только с `--bare`, `--max-turns`, `--max-budget-usd` — без исключений.
5. Минимум зависимостей: stdlib + `requests` + `pyyaml` + `python-dotenv`. Без фреймворков, без async, без ORM.

## 7. Порядок реализации (фазы)

**Фаза 1 — скелет и данные:** структура репо, `db.py` с миграциями, `config.yaml`, `fetch_posts.py`, `question_queue.py`. Критерий: `fetch_posts.py` собирает реальные посты из 2–3 сабов в JSON, очередь вопросов работает end-to-end.

**Фаза 2 — агент:** `build_agent_input.py`, `prompts/daily_digest.md`, `run_agent.sh`, `parse_agent_output.py`. Критерий: на реальном batch агент возвращает валидный JSON, повторный прогон не предлагает те же посты (seen_posts работает), promo-флаги соответствуют кулдаунам.

**Фаза 3 — доставка и оркестрация:** `send_telegram.py`, `run_daily.sh`, cron, README с инструкцией деплоя (установка Claude Code на VPS: `npm install -g @anthropic-ai/claude-code`, `ANTHROPIC_API_KEY`). Критерий: полный прогон одной командой присылает корректно отформатированный дайджест в Telegram, ошибки на каждом шаге дают уведомление.

**Фаза 4 (опционально, отдельным заданием):** inline-кнопка "✅ Запостил" в Telegram → callback → авто-`log-promo` (потребует webhook или long-polling демон — оценить, стоит ли усложнение).

## 8. Тесты и приёмка

- Unit: фильтрация по окну времени и seen_posts; вычисление `promo_allowed_today` (граничные случаи: ровно cooldown дней, пустая история); парсинг ответа агента (валидный / обёрнутый в ```json / битый); разбиение Telegram-сообщений по лимиту 4096.
- Интеграционный smoke: `run_daily.sh --dry-run` — всё до отправки, дайджест печатается в stdout вместо Telegram.
- Приёмка: три подряд ежедневных прогона на реальных данных без ручного вмешательства; в дайджестах нет повторов постов; стоимость прогона в `run_log` заполнена.

## 9. Что заполняет владелец вручную (не задача агента-разработчика)

- `context/product.md` — скопировать/адаптировать из материалов расширения.
- `context/rules/<sub>.md` — уже существующие наработки по правилам сабреддитов.
- `context/tone.md` — на базе шаблона, который агент-разработчик должен создать с разумным дефолтом.
- Первичное наполнение `question_queue` (20–30 вопросов) — отдельная интерактивная сессия.
- `.env` на VPS.
