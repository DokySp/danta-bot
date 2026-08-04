"""SQLite persistence for deterministic market-news collection."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


DEFAULT_LOCK_TIMEOUT_SECONDS = 900


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class UpsertResult:
    fetched_count: int
    inserted_count: int
    duplicate_count: int


class MarketNewsStore:
    """Own canonical articles, collection provenance, and run status."""

    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        self.db_path = Path(db_path)
        self.read_only = read_only
        if self.read_only:
            if not self.db_path.is_file():
                raise FileNotFoundError(self.db_path)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        target = f"{self.db_path.resolve().as_uri()}?mode=ro" if self.read_only else str(self.db_path)
        connection = sqlite3.connect(
            target,
            timeout=30,
            isolation_level=None,
            uri=self.read_only,
        )
        try:
            if not self.read_only:
                connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_url TEXT NOT NULL DEFAULT '',
                    title_hash TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL DEFAULT '',
                    domain TEXT NOT NULL DEFAULT '',
                    source_country TEXT NOT NULL DEFAULT '',
                    source_language TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    collected_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_articles_canonical_url
                    ON articles(canonical_url) WHERE canonical_url != '';
                CREATE UNIQUE INDEX IF NOT EXISTS uq_articles_title_hash
                    ON articles(title_hash) WHERE title_hash != '';
                CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at);

                CREATE TABLE IF NOT EXISTS article_provenance (
                    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                    source_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    provider_article_id TEXT NOT NULL DEFAULT '',
                    classification TEXT NOT NULL,
                    first_collected_at TEXT NOT NULL,
                    PRIMARY KEY (article_id, source_id, provider, provider_article_id)
                );
                DROP INDEX IF EXISTS uq_provenance_provider_article;

                CREATE TABLE IF NOT EXISTS collection_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    fetched_count INTEGER NOT NULL DEFAULT 0,
                    inserted_count INTEGER NOT NULL DEFAULT 0,
                    duplicate_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_collection_runs_source_id
                    ON collection_runs(source_id, id);

                CREATE TABLE IF NOT EXISTS process_lock (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    locked_until TEXT,
                    owner_token TEXT NOT NULL DEFAULT ''
                );
                INSERT OR IGNORE INTO process_lock (id, locked_until) VALUES (1, NULL);

                """
            )
            lock_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(process_lock)").fetchall()}
            if "owner_token" not in lock_columns:
                connection.execute("ALTER TABLE process_lock ADD COLUMN owner_token TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def acquire_run_lock(self, *, timeout_seconds: int = DEFAULT_LOCK_TIMEOUT_SECONDS) -> Iterator[bool]:
        acquired = False
        owner_token = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT locked_until FROM process_lock WHERE id = 1").fetchone()
                locked_until = parse_iso(row[0]) if row and row[0] else None
                current = datetime.now(timezone.utc)
                if locked_until is not None and locked_until > current:
                    connection.execute("COMMIT")
                else:
                    expires = (current + timedelta(seconds=max(1, timeout_seconds))).replace(microsecond=0).isoformat()
                    connection.execute(
                        "UPDATE process_lock SET locked_until = ?, owner_token = ? WHERE id = 1",
                        (expires, owner_token),
                    )
                    connection.execute("COMMIT")
                    acquired = True
            except Exception:
                connection.execute("ROLLBACK")
                raise
        try:
            yield acquired
        finally:
            if acquired:
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE process_lock SET locked_until = NULL, owner_token = '' WHERE id = 1 AND owner_token = ?",
                        (owner_token,),
                    )

    @staticmethod
    def _existing_article_id(connection: sqlite3.Connection, provider: str, article: dict[str, Any]) -> int | None:
        provider_article_id = str(article.get("provider_article_id") or "")
        if provider_article_id:
            row = connection.execute(
                "SELECT article_id FROM article_provenance WHERE provider = ? AND provider_article_id = ?",
                (provider, provider_article_id),
            ).fetchone()
            if row:
                return int(row[0])
        canonical_url = str(article.get("canonical_url") or "")
        if canonical_url:
            row = connection.execute(
                "SELECT id FROM articles WHERE canonical_url = ?",
                (canonical_url,),
            ).fetchone()
            if row:
                return int(row[0])
        title_hash = str(article.get("title_hash") or "")
        if title_hash:
            row = connection.execute(
                "SELECT id FROM articles WHERE title_hash = ?",
                (title_hash,),
            ).fetchone()
            if row:
                return int(row[0])
        return None

    def upsert_articles(self, source_id: str, provider: str, articles: list[dict[str, Any]]) -> UpsertResult:
        inserted_count = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for article in articles:
                    article_id = self._existing_article_id(connection, provider, article)
                    if article_id is None:
                        cursor = connection.execute(
                            """
                            INSERT INTO articles (
                                canonical_url, title_hash, title, url, domain, source_country,
                                source_language, published_at, collected_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                article.get("canonical_url") or "",
                                article["title_hash"],
                                article["title"],
                                article.get("url") or "",
                                article.get("domain") or "",
                                article.get("source_country") or "",
                                article.get("source_language") or "",
                                article.get("published_at") or "",
                                article.get("collected_at") or now_iso(),
                            ),
                        )
                        article_id = int(cursor.lastrowid)
                        inserted_count += 1
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO article_provenance (
                            article_id, source_id, provider, provider_article_id,
                            classification, first_collected_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            article_id,
                            source_id,
                            provider,
                            article.get("provider_article_id") or "",
                            article.get("classification") or source_id,
                            article.get("collected_at") or now_iso(),
                        ),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return UpsertResult(
            fetched_count=len(articles),
            inserted_count=inserted_count,
            duplicate_count=len(articles) - inserted_count,
        )

    def record_run(
        self,
        *,
        source_id: str,
        started_at: str,
        finished_at: str,
        status: str,
        window_start: str,
        window_end: str,
        fetched_count: int,
        inserted_count: int,
        duplicate_count: int,
        error: str = "",
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO collection_runs (
                    source_id, started_at, finished_at, status, window_start, window_end,
                    fetched_count, inserted_count, duplicate_count, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    started_at,
                    finished_at,
                    status,
                    window_start,
                    window_end,
                    fetched_count,
                    inserted_count,
                    duplicate_count,
                    error[:500],
                ),
            )

    def latest_run_statuses(self) -> dict[str, dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_id, started_at, finished_at, status, window_start, window_end,
                       fetched_count, inserted_count, duplicate_count, error
                FROM collection_runs
                WHERE id IN (SELECT MAX(id) FROM collection_runs GROUP BY source_id)
                """
            ).fetchall()
        return {
            str(row[0]): {
                "source_id": row[0],
                "started_at": row[1],
                "finished_at": row[2],
                "status": row[3],
                "window_start": row[4],
                "window_end": row[5],
                "fetched_count": row[6],
                "inserted_count": row[7],
                "duplicate_count": row[8],
                "error": row[9],
            }
            for row in rows
        }

    def query_between(
        self,
        window_start_iso: str,
        window_end_iso: str,
        *,
        limit: int = 500,
        source_id: str = "",
    ) -> list[dict[str, Any]]:
        source_filter = str(source_id or "").strip()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.title, a.url, a.canonical_url, a.domain, a.source_country,
                       a.source_language, a.published_at, a.collected_at,
                       GROUP_CONCAT(DISTINCT p.source_id),
                       GROUP_CONCAT(DISTINCT p.provider),
                       GROUP_CONCAT(DISTINCT p.classification)
                FROM articles a
                LEFT JOIN article_provenance p ON p.article_id = a.id
                WHERE (
                    (a.published_at != '' AND a.published_at >= ? AND a.published_at <= ?)
                    OR
                    (a.published_at = '' AND a.collected_at >= ? AND a.collected_at <= ?)
                )
                AND (
                    ? = ''
                    OR EXISTS (
                        SELECT 1
                        FROM article_provenance source_filter
                        WHERE source_filter.article_id = a.id
                          AND source_filter.source_id = ?
                    )
                )
                GROUP BY a.id
                ORDER BY CASE WHEN a.published_at = '' THEN a.collected_at ELSE a.published_at END DESC
                LIMIT ?
                """,
                (
                    window_start_iso,
                    window_end_iso,
                    window_start_iso,
                    window_end_iso,
                    source_filter,
                    source_filter,
                    max(1, limit),
                ),
            ).fetchall()

        def split(value: Any) -> list[str]:
            return sorted({item for item in str(value or "").split(",") if item})

        return [
            {
                "title": row[0],
                "url": row[1],
                "canonical_url": row[2],
                "domain": row[3],
                "source_country": row[4],
                "source_language": row[5],
                "published_at": row[6],
                "collected_at": row[7],
                "source_ids": split(row[8]),
                "providers": split(row[9]),
                "classifications": split(row[10]),
            }
            for row in rows
        ]
