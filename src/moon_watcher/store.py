"""State that should survive a restart, kept in the kv_state table.

Everything here is best effort: if the database is down the bot keeps
running on what it has in memory and tries to save again next time.
"""

import copy
import logging
from typing import Any

from moon_watcher import database as db

logger = logging.getLogger(__name__)


def load(key: str, default: Any = None) -> Any:
    try:
        return db.kv_get(key, default)
    except Exception as e:
        logger.warning("Could not load %s from the database: %s", key, e)
        return default


def save(key: str, value: Any) -> bool:
    try:
        db.kv_set(key, value)
        return True
    except Exception as e:
        logger.warning("Could not save %s to the database: %s", key, e)
        return False


class Saved:
    """A value that is written to the database only when it changed."""

    def __init__(self, key: str):
        self.key = key
        self._written: Any = None

    def load(self, default: Any) -> Any:
        value = load(self.key, None)
        if value is None:
            return default
        self._written = copy.deepcopy(value)
        return value

    def save(self, value: Any) -> None:
        if value == self._written:
            return
        if save(self.key, value):
            self._written = copy.deepcopy(value)
