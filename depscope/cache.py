"""SQLite-кеш ответов deps.dev с TTL.

Хранит JSON-ответы по ключу ``(system, name, version)``. Для GetPackage
используем специальный «версионный» ключ ``__latest__``.

Кеш используется одним event loop'ом, поэтому потокобезопасность не
нужна. SQLite-соединение создаётся в конструкторе и закрывается при
``close()`` или сборке мусора.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)


_LATEST_KEY = "__latest__"


class Cache:
    """SQLite-кеш с TTL для ответов deps.dev API.

    Сохраняет произвольный JSON-сериализуемый ``payload`` по ключу
    ``(system, name, version)``. Записи старше ``ttl_seconds`` считаются
    протухшими: ``get`` для них вернёт ``None``.
    """

    def __init__(self, path: Path, ttl_seconds: int = 3600) -> None:
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                system    TEXT NOT NULL,
                name      TEXT NOT NULL,
                version   TEXT NOT NULL,
                payload   TEXT NOT NULL,
                fetched_at REAL NOT NULL,
                PRIMARY KEY (system, name, version)
            )
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def get(self, system: str, name: str, version: str) -> dict | None:
        """Вернуть payload, если запись свежая. Иначе — ``None``."""

        cur = self._conn.execute(
            "SELECT payload, fetched_at FROM entries WHERE system = ? AND name = ? AND version = ?",
            (system, name, version),
        )
        row = cur.fetchone()
        if not row:
            return None

        payload_text, fetched_at = row
        if time.time() - fetched_at > self.ttl_seconds:
            logger.debug("cache MISS (stale) for %s/%s/%s", system, name, version)
            return None

        try:
            return json.loads(payload_text)
        except json.JSONDecodeError:
            logger.warning(
                "cache: corrupted JSON for %s/%s/%s, ignoring",
                system,
                name,
                version,
            )
            return None

    def set(self, system: str, name: str, version: str, payload: dict) -> None:
        """UPSERT записи в кеш."""

        payload_text = json.dumps(payload)
        self._conn.execute(
            "INSERT INTO entries (system, name, version, payload, fetched_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(system, name, version) DO UPDATE SET "
            "payload = excluded.payload, fetched_at = excluded.fetched_at",
            (system, name, version, payload_text, time.time()),
        )
        self._conn.commit()

    def clear(self) -> None:
        """Удалить все записи."""

        self._conn.execute("DELETE FROM entries")
        self._conn.commit()

    # ------------------------------------------------------------------
    # Удобство
    # ------------------------------------------------------------------

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __del__(self) -> None:  # pragma: no cover - best-effort cleanup
        self.close()


__all__ = ["Cache", "_LATEST_KEY"]
