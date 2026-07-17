"""Единственный слой доступа к SQLite (data/routine.db).

Ничего не знает о конвейере: только stdlib. Каждая публичная функция
открывает и закрывает своё соединение. Миграции — идемпотентный
CREATE TABLE IF NOT EXISTS, выполняемый при каждом connect().
"""
import argparse
import contextlib
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("db")

_SCHEMA = {
    "seen_posts": """
        CREATE TABLE IF NOT EXISTS seen_posts (
            post_id      TEXT PRIMARY KEY,
            subreddit    TEXT NOT NULL,
            title        TEXT NOT NULL,
            url          TEXT NOT NULL,
            suggested_at TEXT NOT NULL,
            was_promo    INTEGER DEFAULT 0
        )
    """,
    "promo_history": """
        CREATE TABLE IF NOT EXISTS promo_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            subreddit  TEXT NOT NULL,
            type       TEXT NOT NULL CHECK(type IN ('comment_promo','question_post','review_post')),
            post_url   TEXT,
            logged_at  TEXT NOT NULL
        )
    """,
    "question_queue": """
        CREATE TABLE IF NOT EXISTS question_queue (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            text       TEXT NOT NULL,
            target_sub TEXT,
            created_at TEXT NOT NULL,
            used_at    TEXT
        )
    """,
    "run_log": """
        CREATE TABLE IF NOT EXISTS run_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at          TEXT NOT NULL,
            status          TEXT NOT NULL,
            posts_fetched   INTEGER,
            posts_suggested INTEGER,
            cost_usd        REAL,
            error           TEXT
        )
    """,
    "telegram_state": """
        CREATE TABLE IF NOT EXISTS telegram_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """,
    "sub_pause": """
        CREATE TABLE IF NOT EXISTS sub_pause (
            subreddit  TEXT PRIMARY KEY,
            paused_at  TEXT NOT NULL
        )
    """,
}

_TELEGRAM_OFFSET_KEY = "updates_offset"

_PROMO_TYPES = {"comment_promo", "question_post", "review_post"}
_RUN_STATUSES = {"ok", "fetch_failed", "agent_failed", "tg_failed"}


def _db_path() -> Path:
    override = os.environ.get("ROUTINE_DB_PATH")
    path = Path(override) if override else Path(__file__).resolve().parent.parent / "data" / "routine.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def ensure_schema(conn: sqlite3.Connection) -> None:
    for name, statement in _SCHEMA.items():
        logger.debug("[db.ensure_schema] ensuring table '%s'", name)
        conn.execute(statement)
    conn.commit()
    logger.debug("[db.ensure_schema] schema check complete")


def connect() -> sqlite3.Connection:
    path = _db_path()
    logger.debug("[db.connect] opening connection at %s", path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --- seen_posts -------------------------------------------------------------

def get_seen_post_ids() -> set:
    logger.debug("[db.get_seen_post_ids] fetching seen post ids")
    with contextlib.closing(connect()) as conn:
        rows = conn.execute("SELECT post_id FROM seen_posts").fetchall()
    ids = {row["post_id"] for row in rows}
    logger.debug("[db.get_seen_post_ids] found %d seen post ids", len(ids))
    return ids


def mark_posts_seen(posts: list) -> int:
    logger.debug("[db.mark_posts_seen] received %d posts", len(posts))
    inserted = 0
    duplicates = 0
    suggested_at = utcnow_iso()
    with contextlib.closing(connect()) as conn:
        with conn:
            for post in posts:
                missing = [f for f in ("post_id", "subreddit", "title", "url") if f not in post]
                if missing:
                    logger.warning("[db.mark_posts_seen] post missing required field(s) %s: %s", missing, post)
                    continue
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO seen_posts (post_id, subreddit, title, url, suggested_at, was_promo) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        post["post_id"],
                        post["subreddit"],
                        post["title"],
                        post["url"],
                        suggested_at,
                        int(bool(post.get("was_promo", False))),
                    ),
                )
                if cursor.rowcount:
                    inserted += 1
                else:
                    duplicates += 1
    logger.debug("[db.mark_posts_seen] inserted=%d duplicates_skipped=%d", inserted, duplicates)
    return inserted


# --- promo_history -----------------------------------------------------------

def log_promo(subreddit: str, promo_type: str, post_url: str = None) -> None:
    if promo_type not in _PROMO_TYPES:
        logger.error("[db.log_promo] invalid promo type '%s' for subreddit '%s'", promo_type, subreddit)
        raise ValueError(f"invalid promo type: {promo_type!r}, expected one of {sorted(_PROMO_TYPES)}")
    logged_at = utcnow_iso()
    with contextlib.closing(connect()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO promo_history (subreddit, type, post_url, logged_at) VALUES (?, ?, ?, ?)",
                (subreddit, promo_type, post_url, logged_at),
            )
    logger.info("[db.log_promo] logged promo type='%s' for subreddit='%s'", promo_type, subreddit)


def last_promo_days_ago(subreddit: str):
    with contextlib.closing(connect()) as conn:
        row = conn.execute(
            "SELECT MAX(logged_at) AS last FROM promo_history WHERE subreddit = ?", (subreddit,)
        ).fetchone()
    if row is None or row["last"] is None:
        logger.debug("[db.last_promo_days_ago] no promo history for '%s'", subreddit)
        return None
    days_ago = (datetime.now(timezone.utc) - parse_iso(row["last"])).days
    logger.debug("[db.last_promo_days_ago] subreddit='%s' days_ago=%d", subreddit, days_ago)
    return days_ago


def promo_allowed_today(subreddit: str, cooldown_days: int) -> bool:
    days_ago = last_promo_days_ago(subreddit)
    allowed = days_ago is None or days_ago >= cooldown_days
    logger.debug(
        "[db.promo_allowed_today] subreddit='%s' days_ago=%s cooldown_days=%d allowed=%s",
        subreddit, days_ago, cooldown_days, allowed,
    )
    return allowed


def get_promo_state(subs: list) -> list:
    state = []
    for subreddit, cooldown_days in subs:
        days_ago = last_promo_days_ago(subreddit)
        allowed = days_ago is None or days_ago >= cooldown_days
        logger.debug(
            "[db.get_promo_state] subreddit='%s' last_promo_days_ago=%s cooldown_days=%d promo_allowed_today=%s",
            subreddit, days_ago, cooldown_days, allowed,
        )
        state.append({
            "subreddit": subreddit,
            "last_promo_days_ago": days_ago,
            "promo_allowed_today": allowed,
        })
    return state


# --- question_queue -----------------------------------------------------------

def add_question(text: str, target_sub: str = None) -> int:
    if not text or not text.strip():
        logger.error("[db.add_question] empty question text rejected")
        raise ValueError("question text must not be empty")
    created_at = utcnow_iso()
    with contextlib.closing(connect()) as conn:
        with conn:
            cursor = conn.execute(
                "INSERT INTO question_queue (text, target_sub, created_at) VALUES (?, ?, ?)",
                (text, target_sub, created_at),
            )
            question_id = cursor.lastrowid
    logger.debug("[db.add_question] added question id=%d target_sub=%s", question_id, target_sub)
    return question_id


def list_unused_questions() -> list:
    with contextlib.closing(connect()) as conn:
        rows = conn.execute(
            "SELECT id, text, target_sub, created_at FROM question_queue "
            "WHERE used_at IS NULL ORDER BY created_at ASC, id ASC"
        ).fetchall()
    questions = [dict(row) for row in rows]
    logger.debug("[db.list_unused_questions] found %d unused questions", len(questions))
    return questions


def pop_oldest_question():
    with contextlib.closing(connect()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, text, target_sub, created_at FROM question_queue "
                "WHERE used_at IS NULL ORDER BY created_at ASC, id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                conn.commit()
                logger.warning("[db.pop_oldest_question] question queue is empty")
                return None
            conn.execute(
                "UPDATE question_queue SET used_at = ? WHERE id = ?", (utcnow_iso(), row["id"])
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    result = {"id": row["id"], "text": row["text"], "target_sub": row["target_sub"], "created_at": row["created_at"]}
    logger.info("[db.pop_oldest_question] popped question id=%d target_sub=%s", result["id"], result["target_sub"])
    return result


def queue_stats() -> dict:
    with contextlib.closing(connect()) as conn:
        unused = conn.execute(
            "SELECT COUNT(*) AS n FROM question_queue WHERE used_at IS NULL"
        ).fetchone()["n"]
        used = conn.execute(
            "SELECT COUNT(*) AS n FROM question_queue WHERE used_at IS NOT NULL"
        ).fetchone()["n"]
    logger.debug("[db.queue_stats] unused=%d used=%d", unused, used)
    return {"unused": unused, "used": used}


# --- telegram_state -----------------------------------------------------------

def get_telegram_offset() -> int:
    with contextlib.closing(connect()) as conn:
        row = conn.execute(
            "SELECT value FROM telegram_state WHERE key = ?", (_TELEGRAM_OFFSET_KEY,)
        ).fetchone()
    if row is None:
        logger.debug("[db.get_telegram_offset] no stored offset, defaulting to 0")
        return 0
    try:
        offset = int(row["value"])
    except (TypeError, ValueError):
        logger.error("[db.get_telegram_offset] corrupted offset value: %r", row["value"])
        raise ValueError(f"corrupted telegram_state offset value: {row['value']!r}")
    logger.debug("[db.get_telegram_offset] offset=%d", offset)
    return offset


def set_telegram_offset(update_id: int) -> None:
    with contextlib.closing(connect()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO telegram_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_TELEGRAM_OFFSET_KEY, str(update_id)),
            )
    logger.debug("[db.set_telegram_offset] offset=%d", update_id)


# --- run_log -------------------------------------------------------------------

def log_run(status: str, posts_fetched: int = None, posts_suggested: int = None,
            cost_usd: float = None, error: str = None) -> None:
    if status not in _RUN_STATUSES:
        logger.error("[db.log_run] invalid run status '%s'", status)
        raise ValueError(f"invalid run status: {status!r}, expected one of {sorted(_RUN_STATUSES)}")
    run_at = utcnow_iso()
    with contextlib.closing(connect()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO run_log (run_at, status, posts_fetched, posts_suggested, cost_usd, error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_at, status, posts_fetched, posts_suggested, cost_usd, error),
            )
    logger.info(
        "[db.log_run] status='%s' posts_fetched=%s posts_suggested=%s cost_usd=%s",
        status, posts_fetched, posts_suggested, cost_usd,
    )


def recent_runs(limit: int = 10) -> list:
    logger.debug("[db.recent_runs] fetching last %d runs", limit)
    with contextlib.closing(connect()) as conn:
        rows = conn.execute(
            "SELECT id, run_at, status, posts_fetched, posts_suggested, cost_usd, error "
            "FROM run_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    runs = [dict(row) for row in rows]
    logger.debug("[db.recent_runs] returned %d rows", len(runs))
    return runs


# --- sub_pause -----------------------------------------------------------

def pause_sub(subreddit: str) -> bool:
    with contextlib.closing(connect()) as conn:
        with conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO sub_pause (subreddit, paused_at) VALUES (?, ?)",
                (subreddit, utcnow_iso()),
            )
    paused_now = bool(cursor.rowcount)
    logger.info(
        "[db.pause_sub] subreddit='%s' result=%s",
        subreddit, "paused" if paused_now else "already paused",
    )
    return paused_now


def resume_sub(subreddit: str) -> bool:
    with contextlib.closing(connect()) as conn:
        with conn:
            cursor = conn.execute("DELETE FROM sub_pause WHERE subreddit = ?", (subreddit,))
    was_paused = bool(cursor.rowcount)
    logger.info(
        "[db.resume_sub] subreddit='%s' result=%s",
        subreddit, "resumed" if was_paused else "was not paused",
    )
    return was_paused


def get_paused_subs() -> set:
    with contextlib.closing(connect()) as conn:
        rows = conn.execute("SELECT subreddit FROM sub_pause").fetchall()
    paused = {row["subreddit"] for row in rows}
    logger.debug("[db.get_paused_subs] count=%d paused=%s", len(paused), sorted(paused))
    return paused


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reddit Routine data-layer CLI")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--log-run", choices=sorted(_RUN_STATUSES), help="run status to record")
    mode.add_argument("--show-runs", type=int, nargs="?", const=10, help="print last N runs (default 10)")
    parser.add_argument("--posts-fetched", type=int, default=None)
    parser.add_argument("--posts-suggested", type=int, default=None)
    parser.add_argument("--cost-usd", type=float, default=None)
    parser.add_argument("--error", default=None)
    return parser


def _print_runs(runs: list) -> None:
    if not runs:
        print("no runs logged yet")
        return
    columns = ("id", "run_at", "status", "posts_fetched", "posts_suggested", "cost_usd", "error")
    print("\t".join(columns))
    for run in runs:
        print("\t".join(str(run[col]) for col in columns))


def main(argv=None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    logger.debug("[db.main] CLI invoked with args=%s", vars(args))
    if args.show_runs is not None:
        logger.debug("[db.main] show-runs mode limit=%d", args.show_runs)
        _print_runs(recent_runs(args.show_runs))
        return 0
    try:
        log_run(
            args.log_run,
            posts_fetched=args.posts_fetched,
            posts_suggested=args.posts_suggested,
            cost_usd=args.cost_usd,
            error=args.error,
        )
    except ValueError as exc:
        logger.error("[db.main] %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
