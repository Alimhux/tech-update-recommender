"""Маленький SQLite-кеш для ответов deps.dev с TTL.

Сохраняем JSON-ответы по ключу (system, name, version), чтобы не ходить
в API каждый раз заново. Для GetPackage используется специальная
"версия" "__latest__" — там нам нужна последняя версия пакета.

Кеш рассчитан на один event loop, потокобезопасность не нужна.
Соединение с SQLite открывается при создании и закрывается через close()
или при удалении объекта.
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
    """SQLite-кеш с временем жизни записей.

    Можно положить любой JSON-сериализуемый payload. Запись ищется
    по ключу (system, name, version). Если запись лежит дольше
    ttl_seconds, считаем её устаревшей и get() вернёт None.
    """

    def __init__(self, path: Path, ttl_seconds: int = 3600) -> None:
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds

        # на всякий случай создаём папку для файла кеша
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # соединение с SQLite живёт пока живёт объект Cache
        self._conn = sqlite3.connect(str(self.path))

        # создаём таблицу при первом запуске
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

    # основные операции

    def get(self, system: str, name: str, version: str) -> dict | None:
        """Достаём запись из кеша, если она есть и ещё не устарела."""

        cur = self._conn.execute(
            "SELECT payload, fetched_at FROM entries WHERE system = ? AND name = ? AND version = ?",
            (system, name, version),
        )
        row = cur.fetchone()

        # ничего не нашли — кеша для такого ключа нет
        if not row:
            return None

        payload_text, fetched_at = row

        # запись слишком старая — считаем, что её нет
        if time.time() - fetched_at > self.ttl_seconds:
            logger.debug("cache MISS (stale) for %s/%s/%s", system, name, version)
            return None

        # payload хранится строкой, превращаем обратно в dict
        try:
            return json.loads(payload_text)
        except json.JSONDecodeError:
            # JSON сломан — не падаем, просто игнорируем запись
            logger.warning(
                "cache: corrupted JSON for %s/%s/%s, ignoring",
                system,
                name,
                version,
            )
            return None

    def set(self, system: str, name: str, version: str, payload: dict) -> None:
        """Кладём запись в кеш или обновляем существующую."""

        payload_text = json.dumps(payload)

        # если такой ключ уже есть — SQLite обновит payload и время
        self._conn.execute(
            "INSERT INTO entries (system, name, version, payload, fetched_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(system, name, version) DO UPDATE SET "
            "payload = excluded.payload, fetched_at = excluded.fetched_at",
            (system, name, version, payload_text, time.time()),
        )
        self._conn.commit()

    def clear(self) -> None:
        """Полностью очищаем кеш."""

        self._conn.execute("DELETE FROM entries")
        self._conn.commit()

    # вспомогательное

    def close(self) -> None:
        """Закрываем соединение с базой."""

        try:
            self._conn.close()
        except sqlite3.Error:
            # при закрытии что-то пошло не так — игнорируем,
            # тут особо нечего спасать
            pass

    def __del__(self) -> None:  # pragma: no cover - best-effort cleanup
        # запасной вариант, если close() забыли вызвать руками
        self.close()


__all__ = ["Cache", "_LATEST_KEY"]
