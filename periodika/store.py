"""SQLite krātuve: rāpošanas fronte, resursi, dokumenti un pilnteksta indekss.

Krātuve ir vienīgais stāvokļa avots — rāpošanu var apturēt un atsākt jebkurā
brīdī. Pilntekstam lieto FTS5 (ja pieejams; citādi automātiski atkāpjas uz
``LIKE`` meklēšanu). Indeksē gan oriģinālo, gan mūsdienu rakstībā pārrakstīto
tekstu, gan "salocīto" atslēgu, tāpēc ``sabiedrība`` atrod arī ``ſabeedriba``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .orthography import fold

__all__ = ["Document", "Store"]


@dataclass
class Document:
    """Viens saglabāts objekts: raksts, lappuse, laidiens vai vienkārši lapa."""

    id: str
    kind: str = "article"          # article | page | issue | title | web
    url: str = ""
    viewer_url: str = ""
    issue_id: str = ""
    article_id: str = ""
    page_number: int | None = None
    publication: str = ""
    title: str = ""
    date: str = ""
    language: str = ""
    text_raw: str = ""
    text_modern: str = ""
    orthography: str = ""
    old_score: float = 0.0
    ocr_confidence: float | None = None
    source_type: str = ""          # alto | mets | tei | html | json | text
    metadata: dict[str, Any] = field(default_factory=dict)
    fetched_at: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    url TEXT PRIMARY KEY,
    status INTEGER,
    content_type TEXT,
    content_hash TEXT,
    fetched_at REAL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS frontier (
    url TEXT PRIMARY KEY,
    depth INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'pending',   -- pending | active | done | failed | skipped
    discovered_from TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS frontier_state_idx ON frontier(state, priority DESC, depth);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    kind TEXT,
    url TEXT,
    viewer_url TEXT,
    issue_id TEXT,
    article_id TEXT,
    page_number INTEGER,
    publication TEXT,
    title TEXT,
    date TEXT,
    language TEXT,
    text_raw TEXT,
    text_modern TEXT,
    orthography TEXT,
    old_score REAL,
    ocr_confidence REAL,
    source_type TEXT,
    metadata TEXT,
    fetched_at REAL
);
CREATE INDEX IF NOT EXISTS documents_issue_idx ON documents(issue_id);
CREATE INDEX IF NOT EXISTS documents_date_idx ON documents(date);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title, text_raw, text_modern, text_fold,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS fts_map (rowid INTEGER PRIMARY KEY, doc_id TEXT UNIQUE);
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._lock = threading.Lock()
        con = self._conn()
        con.executescript(_SCHEMA)
        self.fts_enabled = True
        try:
            con.executescript(_FTS_SCHEMA)
        except sqlite3.OperationalError:
            self.fts_enabled = False
        con.commit()

    # -- savienojumi ----------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(self.path, timeout=60, isolation_level=None)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            self._local.con = con
        return con

    def close(self) -> None:
        con = getattr(self._local, "con", None)
        if con is not None:
            con.close()
            self._local.con = None

    # -- fronte ---------------------------------------------------------
    def add_urls(
        self,
        urls: Iterable[str],
        depth: int = 0,
        priority: int = 0,
        discovered_from: str = "",
    ) -> int:
        rows = [(u, depth, priority, discovered_from, time.time()) for u in urls if u]
        if not rows:
            return 0
        with self._lock:
            cur = self._conn().executemany(
                "INSERT OR IGNORE INTO frontier "
                "(url, depth, priority, discovered_from, updated_at) VALUES (?,?,?,?,?)",
                rows,
            )
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def claim(self, limit: int = 1) -> list[sqlite3.Row]:
        """Paņem nākamos URL apstrādei un atzīmē tos kā aktīvus (atomāri)."""
        with self._lock:
            con = self._conn()
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT url, depth, priority FROM frontier WHERE state='pending' "
                "ORDER BY priority DESC, depth ASC, rowid ASC LIMIT ?",
                (limit,),
            ).fetchall()
            if rows:
                con.executemany(
                    "UPDATE frontier SET state='active', attempts=attempts+1, updated_at=? "
                    "WHERE url=?",
                    [(time.time(), r["url"]) for r in rows],
                )
            con.execute("COMMIT")
            return rows

    def finish(self, url: str, state: str = "done") -> None:
        with self._lock:
            self._conn().execute(
                "UPDATE frontier SET state=?, updated_at=? WHERE url=?",
                (state, time.time(), url),
            )

    def requeue_active(self) -> int:
        """Pēc avārijas atgriež "aktīvos" URL rindā (izsauc pirms atsākšanas)."""
        with self._lock:
            cur = self._conn().execute(
                "UPDATE frontier SET state='pending' WHERE state='active'"
            )
            return cur.rowcount or 0

    def frontier_counts(self) -> dict[str, int]:
        rows = self._conn().execute(
            "SELECT state, COUNT(*) AS n FROM frontier GROUP BY state"
        ).fetchall()
        return {r["state"]: r["n"] for r in rows}

    def is_visited(self, url: str) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM frontier WHERE url=? AND state IN ('done','skipped')", (url,)
        ).fetchone()
        return row is not None

    # -- resursi --------------------------------------------------------
    def record_resource(
        self,
        url: str,
        status: int | None,
        content_type: str = "",
        content_hash: str = "",
        error: str = "",
    ) -> None:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO resources "
                "(url, status, content_type, content_hash, fetched_at, error) "
                "VALUES (?,?,?,?,?,?)",
                (url, status, content_type, content_hash, time.time(), error),
            )

    # -- dokumenti ------------------------------------------------------
    def upsert_document(self, doc: Document) -> None:
        payload = doc.to_json()
        payload["metadata"] = json.dumps(doc.metadata, ensure_ascii=False)
        cols = list(payload.keys())
        placeholders = ",".join("?" for _ in cols)
        with self._lock:
            con = self._conn()
            con.execute(
                f"INSERT OR REPLACE INTO documents ({','.join(cols)}) VALUES ({placeholders})",
                [payload[c] for c in cols],
            )
            if self.fts_enabled:
                self._index_document(con, doc)

    def _index_document(self, con: sqlite3.Connection, doc: Document) -> None:
        row = con.execute("SELECT rowid FROM fts_map WHERE doc_id=?", (doc.id,)).fetchone()
        text_fold = fold(f"{doc.title}\n{doc.text_raw}")
        if row:
            rowid = row["rowid"]
            con.execute("DELETE FROM documents_fts WHERE rowid=?", (rowid,))
            con.execute(
                "INSERT INTO documents_fts(rowid, title, text_raw, text_modern, text_fold) "
                "VALUES (?,?,?,?,?)",
                (rowid, doc.title, doc.text_raw, doc.text_modern, text_fold),
            )
        else:
            cur = con.execute(
                "INSERT INTO documents_fts(title, text_raw, text_modern, text_fold) "
                "VALUES (?,?,?,?)",
                (doc.title, doc.text_raw, doc.text_modern, text_fold),
            )
            con.execute(
                "INSERT OR REPLACE INTO fts_map(rowid, doc_id) VALUES (?,?)",
                (cur.lastrowid, doc.id),
            )

    def get_document(self, doc_id: str) -> Document | None:
        row = self._conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        return _row_to_document(row) if row else None

    def find_documents(
        self,
        *,
        issue_id: str = "",
        publication: str = "",
        date_from: str = "",
        date_to: str = "",
        kind: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[Document]:
        where: list[str] = []
        params: list[Any] = []
        if issue_id:
            where.append("issue_id=?")
            params.append(issue_id)
        if publication:
            where.append("publication LIKE ?")
            params.append(f"%{publication}%")
        if date_from:
            where.append("date >= ?")
            params.append(date_from)
        if date_to:
            where.append("date <= ?")
            params.append(date_to)
        if kind:
            where.append("kind=?")
            params.append(kind)
        sql = "SELECT * FROM documents"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY date, page_number LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = self._conn().execute(sql, params).fetchall()
        return [_row_to_document(r) for r in rows]

    # -- pilnteksta meklēšana -------------------------------------------
    def search(
        self, query: str, limit: int = 20, *, fold_query: bool = True
    ) -> list[tuple[Document, str]]:
        """Meklē lokālajā indeksā. Atgriež (dokuments, fragments) pārus.

        Vaicājums tiek meklēts gan tekstā, kā tas ir avotā, gan mūsdienu
        pārrakstījumā, gan "salocītajā" formā — tāpēc mūsdienu vārds atrod
        vecās drukas rakstu un otrādi.
        """
        if not query.strip():
            return []
        con = self._conn()
        if self.fts_enabled:
            terms = [_fts_escape(query)]
            if fold_query:
                folded = fold(query)
                if folded and folded != query.lower():
                    terms.append(_fts_escape(folded))
            match = " OR ".join(terms)
            try:
                rows = con.execute(
                    "SELECT m.doc_id AS doc_id, "
                    "snippet(documents_fts, 1, '«', '»', ' … ', 24) AS snip, "
                    "bm25(documents_fts) AS rank "
                    "FROM documents_fts f JOIN fts_map m ON m.rowid = f.rowid "
                    "WHERE documents_fts MATCH ? ORDER BY rank LIMIT ?",
                    (match, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            out: list[tuple[Document, str]] = []
            for r in rows:
                doc = self.get_document(r["doc_id"])
                if doc:
                    out.append((doc, r["snip"] or ""))
            if out:
                return out
        # atkāpšanās: LIKE pa salocīto tekstu
        needle = f"%{fold(query)}%"
        rows = con.execute(
            "SELECT * FROM documents WHERE "
            "lower(text_modern) LIKE ? OR lower(text_raw) LIKE ? OR lower(title) LIKE ? "
            "LIMIT ?",
            (f"%{query.lower()}%", needle, f"%{query.lower()}%", limit),
        ).fetchall()
        return [(_row_to_document(r), _make_snippet(r["text_modern"] or r["text_raw"], query))
                for r in rows]

    # -- statistika un eksports -----------------------------------------
    def stats(self) -> dict[str, Any]:
        con = self._conn()
        docs = con.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        by_kind = {
            r["kind"]: r["n"]
            for r in con.execute(
                "SELECT kind, COUNT(*) AS n FROM documents GROUP BY kind"
            ).fetchall()
        }
        by_orth = {
            r["orthography"]: r["n"]
            for r in con.execute(
                "SELECT orthography, COUNT(*) AS n FROM documents GROUP BY orthography"
            ).fetchall()
        }
        issues = con.execute(
            "SELECT COUNT(DISTINCT issue_id) AS n FROM documents WHERE issue_id != ''"
        ).fetchone()["n"]
        span = con.execute(
            "SELECT MIN(date) AS a, MAX(date) AS b FROM documents WHERE date != ''"
        ).fetchone()
        return {
            "dokumenti": docs,
            "laidieni": issues,
            "pa_veidiem": by_kind,
            "pa_ortogrāfijām": by_orth,
            "datumu_diapazons": [span["a"], span["b"]],
            "fronte": self.frontier_counts(),
            "fts": self.fts_enabled,
        }

    def iter_documents(self, batch: int = 500) -> Iterator[Document]:
        offset = 0
        while True:
            rows = self._conn().execute(
                "SELECT * FROM documents ORDER BY rowid LIMIT ? OFFSET ?", (batch, offset)
            ).fetchall()
            if not rows:
                return
            for r in rows:
                yield _row_to_document(r)
            offset += batch

    def export_jsonl(self, path: Path | str) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with path.open("w", encoding="utf-8") as fh:
            for doc in self.iter_documents():
                fh.write(json.dumps(doc.to_json(), ensure_ascii=False) + "\n")
                n += 1
        return n

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, value)
            )

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def _row_to_document(row: sqlite3.Row) -> Document:
    data = dict(row)
    data.pop("rowid", None)
    meta = data.get("metadata")
    try:
        data["metadata"] = json.loads(meta) if meta else {}
    except (TypeError, json.JSONDecodeError):
        data["metadata"] = {}
    known = set(Document.__dataclass_fields__)  # type: ignore[attr-defined]
    return Document(**{k: v for k, v in data.items() if k in known})


def _fts_escape(query: str) -> str:
    """Pārvērš brīvu tekstu drošā FTS5 vaicājumā (frāzes pēdiņās)."""
    words = [w for w in query.replace('"', " ").split() if w]
    if not words:
        return '""'
    return " ".join(f'"{w}"' for w in words)


def _make_snippet(text: str, query: str, width: int = 160) -> str:
    if not text:
        return ""
    low = text.lower()
    pos = low.find(query.lower().split()[0]) if query.split() else -1
    if pos < 0:
        return text[:width].strip()
    start = max(0, pos - width // 2)
    return ("… " if start else "") + text[start : start + width].strip() + " …"
