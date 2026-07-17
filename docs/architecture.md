[← Установка и деплой](getting-started.md) · [Back to README](../README.md) · [Конфигурация →](configuration.md)

# Архитектура конвейера

Проект — конвейер независимых шагов, оркестрируемых `run_daily.sh`:

```
fetch_posts.py → build_agent_input.py → run_agent.sh → parse_agent_output.py → send_telegram.py
```

Шаги не импортируют бизнес-логику друг друга — обмен данными только через
JSON-файлы в `data/tmp/` и SQLite (`data/routine.db`, единственная точка
доступа — `src/db.py`). Подробное архитектурное обоснование и правила
зависимостей — в [.ai-factory/ARCHITECTURE.md](../.ai-factory/ARCHITECTURE.md).

## Слой данных (`src/db.py`)

Единственная точка доступа к SQLite (`data/routine.db`). Миграции —
идемпотентный `CREATE TABLE IF NOT EXISTS` при каждом подключении, без
системы версий. Путь к БД можно переопределить через `ROUTINE_DB_PATH`
(используется в тестах).

Запись статуса прогона (вызывается оркестратором `run_daily.sh`, см.
reddit-routine-spec.md, раздел 5.8):

```bash
python src/db.py --log-run ok --posts-fetched 12 --posts-suggested 5 --cost-usd 0.08
```

Допустимые статусы: `ok | fetch_failed | agent_failed | tg_failed`.

## Сбор постов (`src/fetch_posts.py`)

Работает через публичный анонимный Atom-фид Reddit
(`https://www.reddit.com/r/{sub}/new/.rss`) — без OAuth и без credentials:
тянет фид по каждому сабреддиту из `config.yaml` (честный User-Agent, ретраи с
backoff 2s/8s/30s на 429/5xx и сетевые ошибки), фильтрует по окну времени и
`seen_posts`, пишет батч в `data/tmp/posts_batch.json`. Анонимный бюджет rate
limit жёсткий (~1 запрос на ~30-секундное окно) — пауза между сабреддитами
подстраивается под заголовки `x-ratelimit-remaining`/`x-ratelimit-reset`
ответа, полный прогон по 8 сабреддитам занимает ориентировочно ~4 минуты.
`score`/`num_comments` публичный фид не отдаёт — в батче они всегда `0`.

```bash
python src/fetch_posts.py
```

Код выхода `1`, только если **все** сабреддиты не удалось опросить (включая
невалидный XML фида) — частичный отказ или пустой батч после фильтрации
ошибкой не считаются. Сабреддиты на паузе (см. ниже) фид не запрашивают вовсе —
код выхода `1`, если на паузе оказались все сабы разом.

## Пауза сабреддитов (`src/manage_subs.py`)

Оперативное включение/выключение сабреддита без правки `config.yaml` — состояние
хранится в таблице `sub_pause` (`data/routine.db`), а не в YAML: `config.yaml` —
курация владельца, БД — оперативный флаг. Паузнутый саб пропускают ровно две
точки входа конвейера — `fetch_posts.py` (фид не запрашивается) и
`build_agent_input.py` (саб и его посты не попадают в `agent_input.json`);
дальше по конвейеру саб «не существует».

```bash
python src/manage_subs.py pause <subreddit>
python src/manage_subs.py resume <subreddit>
python src/manage_subs.py list             # статус ⏸/✅ по всем сабам из config.yaml
```

То же самое доступно из Telegram командами `/pause`, `/resume`, `/subs` —
см. [Эксплуатация](operations.md). Вопрос дня с `target_sub` на паузе не
пропадает: `build_agent_input.py` обнуляет `target_sub`, и агент сам
перетаргетирует вопрос на доступный саб с `question_posts_allowed: true`.

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
(промо-кулдауны и `promo_allowed` из `config.yaml`, вопрос дня из очереди) и
пишет его в `data/tmp/agent_input.json`:

```bash
python src/build_agent_input.py
```

`context/product.md` и `context/tone.md` обязательны — без них шаг падает с
кодом `1`. Отсутствующий `context/rules/<sub>.md` не ошибка: подставляется
заглушка «только безопасные немаркетинговые комментарии» с WARN в лог.
Сабреддиты на паузе выпадают из `promo_state`, `subreddit_rules` и батча постов
(двойная защита — фильтр по `sub_pause` ещё раз здесь, на случай гонки между
`fetch_posts.py` и паузой TG-командой или ручного перезапуска шага).

**Внимание:** вопрос дня помечается использованным уже на этом шаге. Если
прогон упадёт дальше, вопрос потрачен — пере-добавить через
`question_queue.py add`.

## Вызов агента (`src/run_agent.sh`)

Headless-вызов Claude Code (`claude -p`, аутентификация по подписке владельца)
с жёсткими `--max-turns` и `--max-budget-usd` (раздел 5.3 спеки). Флаг `--bare`
не используется — он несовместим с подписочной сессией (отвечает «Not logged
in»), а изоляция контекста и так обеспечена отсутствием `CLAUDE.md`/`.claude/`
на VPS. Промпт — `prompts/daily_digest.md`, вход — stdin из `agent_input.json`,
выход — `data/tmp/agent_raw.json`:

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
`is_promo` — строго bool, `post_id` из батча). При невалидном ответе — одна
повторная попытка вызова `run_agent.sh` с уточнением промпта, после второй
неудачи — код выхода `1`:

```bash
python src/parse_agent_output.py
```

При успехе пишет предложенные посты в `seen_posts` и кладёт
`data/tmp/digest.json` (`{"digest": ..., "stats": {cost_usd, posts_fetched,
posts_suggested}}`) — вход для `send_telegram.py`. Промо-нарушения (например
`is_promo=true` в сабе на кулдауне) — WARN в лог, не ошибка: владелец видит и
решает сам. Так же WARN, если агент вопреки входу назначил `question_post` в
саб на паузе или неизвестный саб (вне `promo_state`).

## See Also

- [Установка и деплой](getting-started.md) — путь от чистого VPS до первого прогона
- [Конфигурация](configuration.md) — env-переменные и пути файлов-контрактов
- [Эксплуатация](operations.md) — промо-логирование, статус, критерий приёмки
