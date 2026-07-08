"""Общий загрузчик config.yaml.

Ничего не знает о Reddit/Telegram/агенте — как db.py ничего не знает
о конвейере. Каждый шаг импортирует этот модуль вместо повторного
парсинга yaml. Mini-CLI (`python src/config.py agent.max_turns`) даёт
bash-скриптам доступ к значениям config.yaml без парсинга yaml в bash.
"""
import argparse
import logging
import os
import sys
from pathlib import Path

import yaml

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("config")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"


def load_config(path: Path = CONFIG_PATH) -> dict:
    logger.debug("[config.load_config] loading %s", path)
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data:
        logger.error("[config.load_config] %s parsed to an empty document", path)
        raise RuntimeError(f"{path} parsed to an empty document")
    logger.debug("[config.load_config] %s parsed OK", path)
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Print a config.yaml value by dotted key")
    parser.add_argument("key", help="dotted key, e.g. agent.max_turns")
    args = parser.parse_args(argv)

    value = load_config()
    for part in args.key.split("."):
        if not isinstance(value, dict) or part not in value:
            logger.error("[config.main] key '%s' not found in config.yaml", args.key)
            print(f"Error: key '{args.key}' not found in config.yaml", file=sys.stderr)
            return 1
        value = value[part]
    logger.debug("[config.main] key '%s' resolved to %r", args.key, value)
    print(value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
