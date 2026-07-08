"""Валидация config.yaml, .env.example и requirements.txt (без сети, без БД)."""
import logging
import os
import unittest
from pathlib import Path

import yaml

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("test_config.check")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
REQUIREMENTS_PATH = REPO_ROOT / "requirements.txt"

REQUIRED_SECTIONS = ("subreddits", "fetch", "selection", "agent", "telegram")
REQUIRED_SUBREDDIT_FIELDS = (
    "name",
    "promo_allowed",
    "promo_cooldown_days",
    "question_posts_allowed",
    "review_post_allowed",
)
REQUIRED_ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY")
ALLOWED_PACKAGES = {"requests", "pyyaml", "python-dotenv"}


class TestConfigYaml(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logger.debug("[test_config.check] parsing %s", CONFIG_PATH)
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cls.config = yaml.safe_load(f)
        if cls.config is None:
            logger.error("[test_config.check] %s parsed to empty document", CONFIG_PATH)
            raise AssertionError(f"{CONFIG_PATH} parsed to an empty document")
        logger.debug("[test_config.check] %s parsed OK", CONFIG_PATH)

    def test_required_sections_present(self):
        for section in REQUIRED_SECTIONS:
            logger.debug("[test_config.check] checking section '%s' present", section)
            if section not in self.config:
                logger.error("[test_config.check] missing section '%s'", section)
            self.assertIn(section, self.config)

    def test_subreddits_have_required_fields_and_types(self):
        subreddits = self.config["subreddits"]
        self.assertIsInstance(subreddits, list)
        self.assertGreater(len(subreddits), 0)
        for sub in subreddits:
            name = sub.get("name", "<no-name>")
            logger.debug("[test_config.check] checking subreddit '%s' fields", name)
            for field in REQUIRED_SUBREDDIT_FIELDS:
                if field not in sub:
                    logger.error(
                        "[test_config.check] subreddit '%s' missing field '%s'", name, field
                    )
                self.assertIn(field, sub, f"subreddit '{name}' missing field '{field}'")
            self.assertIsInstance(sub["name"], str)
            self.assertIsInstance(sub["promo_allowed"], bool)
            self.assertIsInstance(sub["promo_cooldown_days"], int)
            self.assertIsInstance(sub["question_posts_allowed"], bool)
            self.assertIsInstance(sub["review_post_allowed"], bool)

    def test_fetch_section(self):
        fetch = self.config["fetch"]
        logger.debug("[test_config.check] checking fetch section")
        self.assertIsInstance(fetch["window_hours"], int)
        self.assertIsInstance(fetch["posts_per_sub_limit"], int)
        self.assertIsInstance(fetch["min_post_score"], int)
        user_agent = fetch.get("user_agent", "")
        if not user_agent:
            logger.error("[test_config.check] fetch.user_agent is empty")
        self.assertIsInstance(user_agent, str)
        self.assertGreater(len(user_agent.strip()), 0)

    def test_selection_section(self):
        selection = self.config["selection"]
        logger.debug("[test_config.check] checking selection.posts_per_sub")
        posts_per_sub = selection["posts_per_sub"]
        self.assertIsInstance(posts_per_sub, list)
        self.assertEqual(len(posts_per_sub), 2)
        for value in posts_per_sub:
            self.assertIsInstance(value, int)

    def test_agent_section(self):
        agent = self.config["agent"]
        logger.debug("[test_config.check] checking agent.max_turns and agent.max_budget_usd")
        self.assertIsInstance(agent["max_turns"], int)
        self.assertIsInstance(agent["max_budget_usd"], (int, float))

    def test_telegram_section(self):
        telegram = self.config["telegram"]
        logger.debug("[test_config.check] checking telegram section")
        self.assertIn("send_time", telegram)
        self.assertIsInstance(telegram["split_by_subreddit"], bool)


class TestEnvExample(unittest.TestCase):
    def test_required_keys_present(self):
        logger.debug("[test_config.check] checking %s", ENV_EXAMPLE_PATH)
        content = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        for key in REQUIRED_ENV_KEYS:
            if key not in content:
                logger.error("[test_config.check] %s missing key '%s'", ENV_EXAMPLE_PATH, key)
            self.assertIn(key, content)


class TestRequirementsTxt(unittest.TestCase):
    def test_only_allowed_packages(self):
        logger.debug("[test_config.check] checking %s", REQUIREMENTS_PATH)
        lines = REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines()
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            package_name = (
                stripped.replace(">=", "==").split("==")[0].split("<")[0].strip().lower()
            )
            logger.debug("[test_config.check] found package '%s'", package_name)
            if package_name not in ALLOWED_PACKAGES:
                logger.error(
                    "[test_config.check] disallowed package '%s' in requirements.txt",
                    package_name,
                )
            self.assertIn(package_name, ALLOWED_PACKAGES)


if __name__ == "__main__":
    unittest.main()
