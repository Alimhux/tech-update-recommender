"""Небольшой SQLite-кеш для ответов deps.dev с TTL.

Тут мы сохраняем JSON-ответы по ключу (system, name, version),
чтобы не ходить в API каждый раз заново.

Для GetPackage используется специальная версия "__latest__",
потому что там нужна последняя версия пакета.

Кеш рассчитан на работу в одном event loop, поэтому отдельная
потокобезопасность тут не нужна. Соединение с SQLite открывается
при создании объекта и закрывается через close() или при удалении.
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
    """Простой SQLite-кеш с временем жизни записей.

    Сюда можно положить любой JSON-сериализуемый payload.
    Запись ищется по ключу (system, name, version).

    Если запись лежит дольше ttl_seconds, считаем её устаревшей
    и get() вернёт None.
    """

    def __init__(self, path: Path, ttl_seconds: int = 3600) -> None:
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds

        # На всякий случай создаём папку для файла кеша.
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Открываем соединение с SQLite. Оно живёт, пока живёт объект Cache.
        self._conn = sqlite3.connect(str(self.path))

        # Создаём таблицу, если кеш запускается первый раз.
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
    # Основные операции с кешем
    # ------------------------------------------------------------------

    def get(self, system: str, name: str, version: str) -> dict | None:
        """Достаём запись из кеша, если она есть и ещё не устарела."""

        cur = self._conn.execute(
            "SELECT payload, fetched_at FROM entries WHERE system = ? AND name = ? AND version = ?",
            (system, name, version),
        )
        row = cur.fetchone()

        # Ничего не нашли — значит, кеша для такого ключа нет.
        if not row:
            return None

        payload_text, fetched_at = row

        # Если запись слишком старая, просто считаем, что её нет.
        if time.time() - fetched_at > self.ttl_seconds:
            logger.debug("cache MISS (stale) for %s/%s/%s", system, name, version)
            return None

        # payload хранится строкой, поэтому пробуем превратить его обратно в dict.
        try:
            return json.loads(payload_text)
        except json.JSONDecodeError:
            # Если JSON почему-то сломан, не падаем, а просто игнорируем запись.
            logger.warning(
                "cache: corrupted JSON for %s/%s/%s, ignoring",
                system,
                name,
                version,
            )
            return None

    def set(self, system: str, name: str, version: str, payload: dict) -> None:
        """Кладём запись в кеш или обновляем уже существующую."""

        payload_text = json.dumps(payload)

        # Если такой ключ уже есть, SQLite обновит payload и время получения.
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

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Закрываем соединение с базой."""

        try:
            self._conn.close()
        except sqlite3.Error:
            # Если при закрытии что-то пошло не так, просто игнорируем.
            # Тут уже особо нечего спасать.
            pass

    def __del__(self) -> None:  # pragma: no cover - best-effort cleanup
        # Запасной вариант закрытия соединения, если close() забыли вызвать вручную.
        self.close()


__all__ = ["Cache", "_LATEST_KEY"]
