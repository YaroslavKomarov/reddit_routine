"""Шаг конвейера: сбор свежих постов из отслеживаемых сабреддитов.

Читает config.yaml (через config.py) и seen_posts (через db.py), получает
application-only OAuth-токен (client_credentials, read-only), тянет
oauth.reddit.com/r/{sub}/new по каждому сабреддиту, фильтрует и пишет батч в
data/tmp/posts_batch.json. Не импортирует другие шаги конвейера.
"""
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

import config
import db

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("fetch_posts")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RETRY_DELAYS = (2, 8, 30)
_SUB_PAUSE_SECONDS = 2
_SELFTEXT_MAX_LEN = 2000
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _batch_path() -> Path:
    override = os.environ.get("POSTS_BATCH_PATH")
    path = Path(override) if override else _REPO_ROOT / "data" / "tmp" / "posts_batch.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_access_token(client_id: str, client_secret: str, user_agent: str, timeout: float = 10.0):
    """Application-only OAuth-токен (client_credentials), read-only. None при неудаче.

    Токен и client_secret не логируются ни на одном уровне; тело ответа
    token-эндпоинта в логи не пишется (может содержать токен).
    """
    url = "https://www.reddit.com/api/v1/access_token"
    attempts = len(_RETRY_DELAYS)
    last_status = None
    for attempt in range(1, attempts + 1):
        logger.debug("[fetch_posts.get_access_token] attempt=%d", attempt)
        try:
            response = requests.post(
                url,
                auth=(client_id, client_secret),
                data={"grant_type": "client_credentials"},
                headers={"User-Agent": user_agent},
                timeout=timeout,
            )
        except requests.exceptions.RequestException as exc:
            logger.debug("[fetch_posts.get_access_token] attempt=%d network error: %s", attempt, exc)
            last_status = str(exc)
            if attempt < attempts:
                time.sleep(_RETRY_DELAYS[attempt - 1])
                continue
            logger.error("[fetch_posts.get_access_token] exhausted %d attempts, last error: %s",
                         attempts, last_status)
            return None

        if response.status_code == 200:
            try:
                token = response.json().get("access_token")
            except ValueError:
                token = None
            if not token:
                logger.error("[fetch_posts.get_access_token] status=200, but no access_token in response")
                return None
            logger.info("[fetch_posts.get_access_token] token obtained")
            return token

        last_status = response.status_code
        if response.status_code in _RETRYABLE_STATUSES:
            if attempt < attempts:
                logger.debug("[fetch_posts.get_access_token] attempt=%d status=%d, retrying",
                             attempt, response.status_code)
                time.sleep(_RETRY_DELAYS[attempt - 1])
                continue
            logger.error("[fetch_posts.get_access_token] exhausted %d attempts, last status=%s",
                         attempts, last_status)
            return None

        logger.warning("[fetch_posts.get_access_token] non-2xx status=%d, not retrying (проверьте credentials)",
                       response.status_code)
        return None

    return None  # unreachable: every branch above returns by the final attempt


def fetch_subreddit_raw(sub: str, limit: int, user_agent: str, token: str, timeout: float = 10.0):
    url = f"https://oauth.reddit.com/r/{sub}/new?limit={limit}"
    # Заголовок Authorization (токен) не логируется ни в одной строке ниже.
    headers = {"User-Agent": user_agent, "Authorization": f"Bearer {token}"}
    attempts = len(_RETRY_DELAYS)
    last_status = None
    for attempt in range(1, attempts + 1):
        logger.debug("[fetch_posts.fetch_subreddit_raw] sub='%s' attempt=%d url=%s", sub, attempt, url)
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            logger.debug("[fetch_posts.fetch_subreddit_raw] sub='%s' attempt=%d network error: %s", sub, attempt, exc)
            last_status = str(exc)
            if attempt < attempts:
                time.sleep(_RETRY_DELAYS[attempt - 1])
                continue
            logger.error("[fetch_posts.fetch_subreddit_raw] sub='%s' exhausted %d attempts, last error: %s",
                         sub, attempts, last_status)
            return None

        if response.status_code == 200:
            return response.json()

        last_status = response.status_code
        if response.status_code in _RETRYABLE_STATUSES:
            if attempt < attempts:
                logger.debug("[fetch_posts.fetch_subreddit_raw] sub='%s' attempt=%d status=%d, retrying",
                             sub, attempt, response.status_code)
                time.sleep(_RETRY_DELAYS[attempt - 1])
                continue
            logger.error("[fetch_posts.fetch_subreddit_raw] sub='%s' exhausted %d attempts, last status=%s",
                         sub, attempts, last_status)
            return None

        logger.warning("[fetch_posts.fetch_subreddit_raw] sub='%s' non-2xx status=%d, not retrying",
                        sub, response.status_code)
        return None

    return None  # unreachable: every branch above returns by the final attempt


def filter_and_map_posts(raw_children: list, sub: str, seen_ids: set, window_hours: int, min_post_score: int) -> list:
    cutoff = time.time() - window_hours * 3600
    kept = []
    dropped_seen = dropped_stickied = dropped_removed = dropped_window = dropped_score = 0
    for child in raw_children:
        post = child["data"]
        post_id = post["id"]
        if post_id in seen_ids:
            dropped_seen += 1
            continue
        if post.get("stickied"):
            dropped_stickied += 1
            continue
        if post.get("removed_by_category") is not None:
            dropped_removed += 1
            continue
        if post.get("created_utc", 0) < cutoff:
            dropped_window += 1
            continue
        if post.get("score", 0) < min_post_score:
            dropped_score += 1
            continue

        selftext = post.get("selftext", "") or ""
        kept.append({
            "id": post_id,
            "subreddit": sub,
            "title": post.get("title", ""),
            "selftext": selftext[:_SELFTEXT_MAX_LEN],
            "url": post.get("url", ""),
            "permalink": "https://www.reddit.com" + post.get("permalink", ""),
            "score": post.get("score", 0),
            "num_comments": post.get("num_comments", 0),
            "created_utc": post.get("created_utc"),
        })

    logger.debug(
        "[fetch_posts.filter_and_map_posts] sub='%s' before=%d after=%d "
        "dropped_seen=%d dropped_stickied=%d dropped_removed=%d dropped_window=%d dropped_score=%d",
        sub, len(raw_children), len(kept),
        dropped_seen, dropped_stickied, dropped_removed, dropped_window, dropped_score,
    )
    return kept


def main() -> int:
    # load_dotenv не перезаписывает уже установленные env — fallback для ручных
    # запусков; при запуске из run_daily.sh .env уже засорсен оркестратором.
    load_dotenv(_REPO_ROOT / ".env")
    client_id = os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    # Пустая строка = отсутствует (контракт для тестовой изоляции).
    if not client_id:
        logger.error("[fetch_posts.main] REDDIT_CLIENT_ID не задан — заполните .env, см. .env.example")
        return 1
    if not client_secret:
        logger.error("[fetch_posts.main] REDDIT_CLIENT_SECRET не задан — заполните .env, см. .env.example")
        return 1
    logger.debug("[fetch_posts.main] credentials загружены")

    cfg = config.load_config()
    seen_ids = db.get_seen_post_ids()
    fetch_cfg = cfg["fetch"]
    subreddits = cfg["subreddits"]

    token = get_access_token(client_id, client_secret, fetch_cfg["user_agent"])
    if token is None:
        logger.error("[fetch_posts.main] не удалось получить OAuth-токен — прерываю fetch")
        return 1

    batch = []
    succeeded = []
    failed = []

    for index, sub_cfg in enumerate(subreddits):
        sub = sub_cfg["name"]
        raw = fetch_subreddit_raw(sub, fetch_cfg["posts_per_sub_limit"], fetch_cfg["user_agent"], token)
        if raw is None:
            failed.append(sub)
        else:
            children = raw.get("data", {}).get("children", [])
            posts = filter_and_map_posts(
                children, sub, seen_ids, fetch_cfg["window_hours"], fetch_cfg["min_post_score"]
            )
            batch.extend(posts)
            succeeded.append(sub)

        if index < len(subreddits) - 1:
            time.sleep(_SUB_PAUSE_SECONDS)

    if not succeeded:
        logger.error("[fetch_posts.main] all %d subreddit(s) failed: %s", len(subreddits), failed)
        return 1

    logger.info(
        "[fetch_posts.main] batch size=%d succeeded=%s failed=%s",
        len(batch), succeeded, failed,
    )

    path = _batch_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(batch, f, ensure_ascii=False)
    logger.debug("[fetch_posts.main] wrote batch to %s", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
