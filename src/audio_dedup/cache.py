from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

from .matchers.indexing import extract_terms


class FingerprintCache:
    """SQLite-backed fingerprint cache and inverted term index.

    Two responsibilities share one connection/file so they can never desync
    on a crash: the `fingerprints` table is a plain (path, mtime) -> raw
    fingerprint cache; `files`/`terms`/`term_stats` form an inverted index of
    coarse fingerprint terms (see matchers/indexing.py) used to generate
    candidate duplicate pairs without an O(n^2) scan.
    """

    def __init__(self, cache_path: Path) -> None:
        self._conn = sqlite3.connect(cache_path, check_same_thread=False)
        self._conn.isolation_level = None  # manual transaction control via explicit BEGIN/COMMIT
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS fingerprints ("
            "path TEXT PRIMARY KEY, mtime REAL NOT NULL, "
            "duration REAL NOT NULL, fingerprint BLOB NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS files ("
            "id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, term_count INTEGER NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS terms ("
            "term INTEGER NOT NULL, file_id INTEGER NOT NULL, "
            "PRIMARY KEY (term, file_id)) WITHOUT ROWID"
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_terms_file ON terms(file_id)")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS term_stats (term INTEGER PRIMARY KEY, df INTEGER NOT NULL) "
            "WITHOUT ROWID"
        )
        self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

    def get(self, file_path: str, mtime: float) -> tuple[float, list[int]] | None:
        row = self._conn.execute(
            "SELECT mtime, duration, fingerprint FROM fingerprints WHERE path = ?",
            (file_path,),
        ).fetchone()
        if row is None:
            return None
        cached_mtime, duration, blob = row
        if abs(cached_mtime - mtime) >= 1.0:
            return None
        count = len(blob) // 4
        fingerprint = list(struct.unpack(f"<{count}i", blob))
        return duration, fingerprint

    def set(self, file_path: str, mtime: float, duration: float, fingerprint: list[int]) -> None:
        blob = struct.pack(f"<{len(fingerprint)}i", *fingerprint)
        terms = extract_terms(fingerprint)
        cur = self._conn.cursor()
        cur.execute("BEGIN")
        try:
            cur.execute(
                "INSERT INTO fingerprints (path, mtime, duration, fingerprint) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, duration=excluded.duration, "
                "fingerprint=excluded.fingerprint",
                (file_path, mtime, duration, blob),
            )
            self._reindex_terms(cur, file_path, terms)
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    def _reindex_terms(self, cur: sqlite3.Cursor, file_path: str, terms: set[int]) -> None:
        """Replace a file's postings. Caller owns the transaction."""
        cur.execute(
            "INSERT INTO files (path, term_count) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET term_count=excluded.term_count",
            (file_path, len(terms)),
        )
        file_id = cur.execute("SELECT id FROM files WHERE path = ?", (file_path,)).fetchone()[0]
        cur.execute("DELETE FROM terms WHERE file_id = ?", (file_id,))
        cur.executemany(
            "INSERT INTO terms (term, file_id) VALUES (?, ?)",
            [(t, file_id) for t in terms],
        )

    def rebuild_term_stats(self) -> None:
        """Recompute per-term document frequency from scratch.

        `terms` is clustered by (term, file_id), so GROUP BY term is a
        sequential scan over already-sorted data, not a sort. Cheap enough
        to just run once per run rather than maintain incrementally (which
        would mean ~thousands of random-access upserts per file).
        """
        cur = self._conn.cursor()
        cur.execute("BEGIN")
        try:
            cur.execute("DELETE FROM term_stats")
            cur.execute("INSERT INTO term_stats (term, df) SELECT term, COUNT(*) FROM terms GROUP BY term")
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    def backfill_terms(self, verbose: bool = False) -> int:
        """Index terms for fingerprints cached before this feature existed.

        Re-indexes from the already-cached fingerprint BLOB — never
        recomputes a fingerprint, so this doesn't need --force-refresh.
        Resumable if interrupted; a one-time meta flag skips the detection
        scan on subsequent normal runs once nothing is left to backfill.
        """
        if self._conn.execute("SELECT value FROM meta WHERE key = 'terms_backfilled'").fetchone():
            return 0

        rows = self._conn.execute(
            "SELECT f.path, f.fingerprint FROM fingerprints f "
            "WHERE NOT EXISTS (SELECT 1 FROM files WHERE files.path = f.path)"
        ).fetchall()

        if rows and verbose:
            print(f"  Backfilling term index for {len(rows)} cached fingerprints (one-time)...", flush=True)

        cur = self._conn.cursor()
        for path, blob in rows:
            count = len(blob) // 4
            fingerprint = list(struct.unpack(f"<{count}i", blob))
            terms = extract_terms(fingerprint)
            cur.execute("BEGIN")
            try:
                self._reindex_terms(cur, path, terms)
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

        cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('terms_backfilled', '1')")
        return len(rows)

    def find_candidates(
        self,
        self_path: str,
        term_keys: set[int],
        min_abs: int = 20,
        df_cap: int = 2000,
    ) -> list[tuple[str, int, int]]:
        """Files sharing at least `min_abs` terms with `term_keys`, excluding
        overly common terms (document frequency > df_cap). Only returns
        candidates with path > self_path, so a full pass over every file as
        "self" surfaces each unordered pair exactly once.

        Returns (candidate_path, shared_term_count, candidate_term_count).
        """
        if not term_keys:
            return []
        cur = self._conn.cursor()
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS query_terms (term INTEGER PRIMARY KEY)")
        cur.execute("DELETE FROM query_terms")
        cur.executemany("INSERT INTO query_terms (term) VALUES (?)", [(t,) for t in term_keys])
        return cur.execute(
            """
            SELECT fl.path, COUNT(*) AS shared, fl.term_count
            FROM query_terms q
            JOIN term_stats s ON s.term = q.term
            JOIN terms t ON t.term = q.term
            JOIN files fl ON fl.id = t.file_id
            WHERE s.df <= ? AND fl.path > ?
            GROUP BY t.file_id
            HAVING shared >= ?
            ORDER BY shared DESC
            """,
            (df_cap, self_path, min_abs),
        ).fetchall()

    def __len__(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]

    def close(self) -> None:
        self._conn.close()
