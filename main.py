#!/usr/bin/env python3
"""
WorldQuant BRAIN MCP Server - Python Version
A comprehensive Model Context Protocol (MCP) server for WorldQuant BRAIN platform integration.
"""

import json
import time
import asyncio
import logging
from typing import Dict, List, Optional, Any, Union, Sequence
import re
import base64
from bs4 import BeautifulSoup
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
import os
import sys
from pathlib import Path
from time import sleep
from urllib.parse import urljoin, urlparse
import redis
import sqlite3
import hashlib
import math
import uuid
import random
import fcntl
from collections import deque

import requests
import pandas as pd
import zlib
import msgpack
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, EmailStr, model_validator

# Import the new forum client
from forum_functions import forum_client

# Import the BRAIN Labs client (Playwright sign-in + single-concurrency lock)
from labs_functions import labs_client

# Candidate-pool management (submission queue + pyramid coverage + correlation safety)
import candidate_pool as cpool

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def archive_performance_comparison(
    alpha_id: str, scope: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Atomically archive a non-empty comparison result without credentials."""
    if not payload or payload.get("available") is False or payload.get("error"):
        return payload

    root = Path(
        os.environ.get(
            "BRAIN_PERFORMANCE_COMPARISON_ARCHIVE",
            r"D:\wqb_tuzige\tracking\performance_comparison",
        )
    )
    root.mkdir(parents=True, exist_ok=True)
    saved_at = datetime.now().astimezone().isoformat(timespec="seconds")
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    safe_alpha_id = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in alpha_id
    )
    archive_path = root / f"{safe_alpha_id}-{timestamp}.json"
    temporary_path = archive_path.with_suffix(".json.tmp")
    record = {
        "schema_version": 1,
        "saved_at": saved_at,
        "alpha_id": alpha_id,
        "scope": scope,
        "result": payload,
    }
    temporary_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, archive_path)
    result = dict(payload)
    result["_archive"] = {
        "saved": True,
        "path": str(archive_path),
        "saved_at": saved_at,
    }
    return result

# Pydantic models for type safety
class AuthCredentials(BaseModel):
    email: EmailStr
    password: str

class SimulationSettings(BaseModel):
    instrumentType: str = "EQUITY"
    region: str = "USA"
    universe: str = "TOP3000"
    delay: int = 1
    decay: float = 0.0
    neutralization: str = "NONE"
    truncation: float = 0.0
    pasteurization: str = "ON"
    unitHandling: Optional[str] = "VERIFY"
    nanHandling: Optional[str] = "OFF"
    language: str = "FASTEXPR"
    lookback: Optional[int] = None
    visualization: bool = True
    testPeriod: str = "P0Y0M"
    selectionHandling: str = "POSITIVE"
    selectionLimit: int = 1000
    maxTrade: str = "OFF"
    componentActivation: str = "IS"

class SimulationData(BaseModel):
    type: str = "REGULAR"  # "REGULAR" or "SUPER"
    settings: SimulationSettings
    regular: Optional[str] = None
    combo: Optional[str] = None
    selection: Optional[str] = None

    @model_validator(mode="after")
    def validate_super_selection_rules(self) -> "SimulationData":
        if self.type.upper() != "SUPER":
            return self

        region = self.settings.region.upper()
        if region != "USA":
            return self

        if not self.selection:
            raise ValueError('USA SUPER simulations require selection to include (prod_correlation > 0)')

        if not re.search(r"\(\s*prod_correlation\s*>\s*0(?:\.0+)?\s*\)", self.selection):
            raise ValueError('USA SUPER simulations require selection to include (prod_correlation > 0)')

        return self



# The unfiltered /data-fields window stops here; offset beyond it returns HTTP 400.
_DATAFIELDS_WINDOW_CAP = 10000

_CATALOGUE_TRUNCATED_WARNING = (
    f"TRUNCATED at the {_DATAFIELDS_WINDOW_CAP}-row limit of the unfiltered /data-fields "
    "window. Fields beyond it are NOT here and cannot be reached by paging — for USA/TOP3000 "
    "that hides ~89% of the catalogue and 267 datasets entirely. Query one dataset at a time "
    "(dataset_id=...) for complete coverage, or run build_datafield_catalogue once."
)


def _annotate_catalogue_completeness(payload):
    """Mark a config-level catalogue as truncated, deriving it at read time.

    Entries stored before this flag existed carry no marker, and a catalogue that
    silently looks whole is precisely how the gap went unnoticed — so the state is
    recomputed on every read rather than trusted from storage.

    Completeness is judged on rows FETCHED, not on unique ids: a field can belong
    to two datasets at once (verified in DEU/TOP500, where
    price_momentum_12m_minus_1m is in both analyst94 and model109), so a complete
    catalogue legitimately holds fewer unique ids than the datasets declare.
    """
    if not isinstance(payload, dict) or 'results' not in payload:
        return payload
    declared = payload.get('declared_total')
    unique = len(payload.get('results') or [])
    fetched = payload.get('fetched_rows')
    if fetched is None:
        fetched = unique
    if declared:
        payload['coverage'] = round(fetched / declared, 4)
        payload['capped'] = fetched < declared
        if payload['capped']:
            payload['warning'] = (f"INCOMPLETE: {fetched} of {declared} declared fields stored. "
                                  "Run build_datafield_catalogue to finish it.")
        else:
            payload.pop('warning', None)
    elif unique >= _DATAFIELDS_WINDOW_CAP:
        payload['capped'] = True
        payload['warning'] = _CATALOGUE_TRUNCATED_WARNING
    return payload


def _dataset_field_matches(item: Dict[str, Any], search_term: str) -> bool:
    """Multi-keyword AND match over a datafield's searchable text."""
    keywords = [k.strip().lower() for k in (search_term or '').split() if k.strip()]
    if not keywords:
        return True
    parts = [str(item.get(k) or '') for k in ('name', 'description', 'id')]
    ds = item.get('dataset')
    if isinstance(ds, dict):
        parts += [str(ds.get(k) or '') for k in ('name', 'vendor', 'id')]
    text = ' '.join(parts).lower()
    return all(k in text for k in keywords)


class PersistentStore:
    """Permanent, on-disk store for platform data that never changes.

    A submitted (OS) alpha's record, an alpha's PnL series, the datafield and
    dataset catalogues and the operator list are all immutable for practical
    purposes, so they belong in durable storage rather than a TTL cache. Redis is
    deliberately NOT used for them: this deployment runs it with
    ``maxmemory-policy=noeviction`` and no cap, so parking hundreds of MB of
    permanent data there trades an API-traffic problem for an out-of-memory one.

    Measured on this data, disk is also simply the better tier: a 5.3 MB
    datafield catalogue compresses to 4.3% (226 KB), and reading + inflating it
    from disk costs less than a Redis GET of the raw string because ``json.loads``
    dominates either way.

    Layout: ``<root>/<namespace>/<sha1[:2]>/<sha1>.z``, each holding one
    zlib-compressed JSON envelope. Writes are atomic (temp file + os.replace) so a
    crash or a concurrent writer can never leave a torn entry behind.
    """

    ENVELOPE_VERSION = 1

    def __init__(self, root: Path, log, level: int = 6):
        self.root = Path(root)
        self._log = log
        self._level = level

    @staticmethod
    def _digest(key: str) -> str:
        return hashlib.sha1(key.encode('utf-8')).hexdigest()

    def path_for(self, namespace: str, key: str) -> Path:
        h = self._digest(key)
        return self.root / namespace / h[:2] / f"{h}.z"

    # --- blocking primitives (always called via asyncio.to_thread) ---------- #

    def _read_sync(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as e:
            self._log(f"[store] Read failed for {path}: {e}", "WARNING")
            return None
        try:
            envelope = json.loads(zlib.decompress(blob))
        except Exception as e:
            # A corrupt entry must not poison the caller; drop it and re-fetch.
            self._log(f"[store] Corrupt entry {path} ({e}); discarding", "WARNING")
            try:
                path.unlink()
            except OSError:
                pass
            return None
        if not isinstance(envelope, dict) or 'payload' not in envelope:
            return None
        return envelope

    def _write_sync(self, path: Path, envelope: Dict[str, Any]) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = zlib.compress(
            json.dumps(envelope, ensure_ascii=False, separators=(',', ':')).encode('utf-8'),
            self._level,
        )
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(blob)
            os.replace(tmp, path)  # atomic within the same directory
        except OSError as e:
            self._log(f"[store] Write failed for {path}: {e}", "WARNING")
            try:
                tmp.unlink()
            except OSError:
                pass
            return 0
        return len(blob)

    # --- async API --------------------------------------------------------- #

    async def get(self, namespace: str, key: str) -> Optional[Any]:
        """Return the stored payload, or None when absent."""
        envelope = await asyncio.to_thread(self._read_sync, self.path_for(namespace, key))
        return envelope['payload'] if envelope else None

    async def get_envelope(self, namespace: str, key: str) -> Optional[Dict[str, Any]]:
        """Return the full record including ``fetched_at`` and ``key_params``."""
        return await asyncio.to_thread(self._read_sync, self.path_for(namespace, key))

    async def put(
        self,
        namespace: str,
        key: str,
        payload: Any,
        key_params: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Store ``payload`` permanently. Returns the compressed size in bytes."""
        envelope = {
            'v': self.ENVELOPE_VERSION,
            'ns': namespace,
            'key': key,
            'key_params': key_params,
            'fetched_at': time.time(),
            'payload': payload,
        }
        return await asyncio.to_thread(self._write_sync, self.path_for(namespace, key), envelope)

    async def delete(self, namespace: str, key: str) -> bool:
        def _rm(path: Path) -> bool:
            try:
                path.unlink()
                return True
            except OSError:
                return False
        return await asyncio.to_thread(_rm, self.path_for(namespace, key))

    def iter_files(self, namespace: str):
        base = self.root / namespace
        if not base.exists():
            return
        yield from base.glob('*/*.z')

    async def list_entries(self, namespace: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Metadata for stored entries (payload omitted), newest first."""
        def _scan() -> List[Dict[str, Any]]:
            rows = []
            for f in self.iter_files(namespace):
                envelope = self._read_sync(f)
                if not envelope:
                    continue
                rows.append({
                    'key': envelope.get('key'),
                    'key_params': envelope.get('key_params'),
                    'fetched_at': envelope.get('fetched_at'),
                    'age_days': round((time.time() - (envelope.get('fetched_at') or 0)) / 86400, 2),
                    'bytes': f.stat().st_size,
                })
            rows.sort(key=lambda r: r.get('fetched_at') or 0, reverse=True)
            return rows[:limit] if limit else rows
        return await asyncio.to_thread(_scan)

    async def stats(self, namespaces: Sequence[str]) -> Dict[str, Any]:
        def _scan() -> Dict[str, Any]:
            out: Dict[str, Any] = {}
            total_files = total_bytes = 0
            for ns in namespaces:
                n = b = 0
                oldest = None
                for f in self.iter_files(ns):
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    n += 1
                    b += st.st_size
                    oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
                out[ns] = {
                    'entries': n,
                    'bytes': b,
                    'oldest_age_days': round((time.time() - oldest) / 86400, 2) if oldest else None,
                }
                total_files += n
                total_bytes += b
            out['_total'] = {'entries': total_files, 'bytes': total_bytes,
                             'mb': round(total_bytes / 1048576, 2)}
            return out
        return await asyncio.to_thread(_scan)


class AlphaStore:
    """SQLite corpus of every alpha this account has simulated.

    Why not the file store: this account creates ~2,769 alphas/day and holds
    ~678,000 since 2026-01-01. One file per alpha would mean 678k inodes and turn
    every question into a full decompress-and-scan; the same corpus is ~450 MB in
    SQLite with indexes, and FTS5 (present in the container, no new dependency)
    answers "have I tried this expression" in milliseconds.

    Keeping *every* alpha rather than only the good ones is what buys the
    denominator: "which datafield actually produces high-Sharpe alphas" is a
    ratio, and it is unanswerable from a corpus of winners alone.
    """

    COLUMNS = 27

    SCHEMA = [
        """CREATE TABLE IF NOT EXISTS alphas (
            id TEXT PRIMARY KEY,
            date_created TEXT, date_submitted TEXT,
            stage TEXT, status TEXT, type TEXT, color TEXT,
            instrument_type TEXT, region TEXT, universe TEXT, delay INTEGER,
            neutralization TEXT, decay REAL, truncation REAL, language TEXT,
            expression TEXT,
            sharpe REAL, fitness REAL, turnover REAL, returns REAL,
            margin REAL, drawdown REAL, long_count INTEGER, short_count INTEGER,
            pyramids TEXT, classifications TEXT,
            fetched_at REAL
        )""",
        "CREATE INDEX IF NOT EXISTS ix_alphas_cfg ON alphas(region, universe, delay)",
        "CREATE INDEX IF NOT EXISTS ix_alphas_sharpe ON alphas(sharpe DESC)",
        "CREATE INDEX IF NOT EXISTS ix_alphas_date ON alphas(date_created)",
        "CREATE INDEX IF NOT EXISTS ix_alphas_stage ON alphas(stage)",
        """CREATE VIRTUAL TABLE IF NOT EXISTS alphas_fts
           USING fts5(expression, content='alphas', content_rowid='rowid')""",
        "CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT)",
        # Which identifiers each expression uses. Without this table, "how often
        # does field X produce a good alpha" means tokenising 678k expressions on
        # every question; with it, it is one indexed GROUP BY.
        """CREATE TABLE IF NOT EXISTS alpha_tokens (
            alpha_id TEXT NOT NULL, token TEXT NOT NULL, kind TEXT,
            PRIMARY KEY (alpha_id, token)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_tokens_token ON alpha_tokens(token, kind)",
        # Datafield catalogue, indexed for search. It lives in the same database
        # as the alpha corpus so "which of the fields I actually use are about
        # analyst revisions" is one join rather than two lookups in two stores.
        """CREATE TABLE IF NOT EXISTS datafields (
            id TEXT PRIMARY KEY, description TEXT, type TEXT,
            dataset TEXT, category TEXT, date_created TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS datafield_configs (
            id TEXT NOT NULL, region TEXT, universe TEXT, delay INTEGER,
            coverage REAL, user_count INTEGER, alpha_count INTEGER,
            PRIMARY KEY (id, region, universe, delay)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_dfcfg ON datafield_configs(region, universe, delay)",
        # porter stemming is the whole point: the catalogue says "estimates
        # lowered" where a researcher types "lower", and substring matching also
        # produced false hits like "cutoff" for "cut".
        """CREATE VIRTUAL TABLE IF NOT EXISTS datafields_fts
           USING fts5(id UNINDEXED, description, tokenize='porter unicode61')""",
    ]

    # Expression identifiers: FastExpr fields and operators are both bare words.
    _TOKEN_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')

    def __init__(self, path: Path, log):
        self.path = Path(path)
        self._log = log
        self._lock = asyncio.Lock()
        self._ready = False

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL lets the analysis tools read while a sync is still writing.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_sync(self) -> None:
        conn = self._connect()
        try:
            for stmt in self.SCHEMA:
                conn.execute(stmt)
            conn.commit()
        finally:
            conn.close()
        self._ready = True

    async def ensure_ready(self) -> None:
        if not self._ready:
            await asyncio.to_thread(self._init_sync)

    @staticmethod
    def _utc(value: Optional[str]) -> Optional[str]:
        """Normalise a platform timestamp to UTC.

        The API returns local-offset stamps ("2026-08-14T20:00:21-04:00"), and
        SQLite compares them as text — so a naive range query on '2026-08-15'
        silently misses rows that are inside the UTC day. Everything is stored as
        UTC so lexicographic comparison is the same as chronological.
        """
        if not value:
            return value
        try:
            return datetime.fromisoformat(value).astimezone(timezone.utc)\
                .isoformat().replace('+00:00', 'Z')
        except (ValueError, TypeError):
            return value

    @staticmethod
    def row_from_alpha(a: Dict[str, Any]) -> Optional[tuple]:
        """Flatten one platform alpha record into a corpus row."""
        aid = a.get('id')
        if not aid:
            return None
        s = a.get('settings') or {}
        m = a.get('is') or {}
        # The expression arrives as {"code","description","operatorCount"} for
        # REGULAR alphas and as a bare string elsewhere; SUPER alphas carry it
        # under `combo` in either shape.
        expression = AlphaStore._code_of(a.get('regular')) or AlphaStore._code_of(a.get('combo'))
        pyr = a.get('pyramids')
        return AlphaStore._sanitize((
            aid, AlphaStore._utc(a.get('dateCreated')), AlphaStore._utc(a.get('dateSubmitted')),
            a.get('stage'), a.get('status'), a.get('type'), a.get('color'),
            s.get('instrumentType'), s.get('region'), s.get('universe'), s.get('delay'),
            s.get('neutralization'), s.get('decay'), s.get('truncation'), s.get('language'),
            expression,
            m.get('sharpe'), m.get('fitness'), m.get('turnover'), m.get('returns'),
            m.get('margin'), m.get('drawdown'), m.get('longCount'), m.get('shortCount'),
            json.dumps([p.get('name') for p in pyr if isinstance(p, dict)], ensure_ascii=False)
                if isinstance(pyr, list) else None,
            json.dumps([c.get('name') or c.get('id') for c in (a.get('classifications') or [])
                        if isinstance(c, dict)], ensure_ascii=False),
            time.time(),
        ))

    @staticmethod
    def _code_of(value: Any) -> Optional[str]:
        if isinstance(value, dict):
            code = value.get('code')
            return code if isinstance(code, str) else None
        return value if isinstance(value, str) else None

    @staticmethod
    def _sanitize(row: tuple) -> tuple:
        """Make every value bindable by sqlite3.

        One unexpected shape in 678,000 records would otherwise abort the whole
        sync, so anything that is not a primitive is stored as JSON text rather
        than raising.
        """
        out = []
        for v in row:
            if v is None or isinstance(v, (str, int, float, bytes)):
                out.append(v)
            else:
                try:
                    out.append(json.dumps(v, ensure_ascii=False, default=str))
                except Exception:
                    out.append(str(v))
        return tuple(out)

    def _upsert_sync(self, rows: List[tuple], index: bool,
                     op_set: set, field_set: set) -> int:
        if not rows:
            return 0
        conn = self._connect()
        try:
            if index:
                # alphas_fts is an external-content table, so it does not follow
                # writes to `alphas` on its own. An incremental write has to
                # remove the old term row before adding the new one, or a
                # re-simulated id would match twice.
                ids = [r[0] for r in rows]
                q = ",".join("?" * len(ids))
                for rowid, expr in conn.execute(
                        f"SELECT rowid, expression FROM alphas WHERE id IN ({q})", ids):
                    conn.execute("INSERT INTO alphas_fts(alphas_fts, rowid, expression) "
                                 "VALUES('delete', ?, ?)", (rowid, expr))
                conn.execute(f"DELETE FROM alpha_tokens WHERE alpha_id IN ({q})", ids)

            conn.executemany(
                "INSERT OR REPLACE INTO alphas VALUES (" + ",".join(["?"] * self.COLUMNS) + ")",
                rows)

            if index:
                ids = [r[0] for r in rows]
                q = ",".join("?" * len(ids))
                token_rows = []
                for rowid, aid, expr in conn.execute(
                        f"SELECT rowid, id, expression FROM alphas WHERE id IN ({q})", ids):
                    conn.execute("INSERT INTO alphas_fts(rowid, expression) VALUES (?,?)",
                                 (rowid, expr))
                    seen = set()
                    for tok in AlphaStore._TOKEN_RE.findall(expr or ""):
                        low = tok.lower()
                        if low in seen:
                            continue
                        seen.add(low)
                        kind = ('operator' if low in op_set
                                else 'field' if low in field_set else None)
                        if kind:
                            token_rows.append((aid, low, kind))
                if token_rows:
                    conn.executemany(
                        "INSERT OR IGNORE INTO alpha_tokens VALUES (?,?,?)", token_rows)
                # Keep the coverage marker honest as the corpus grows.
                total = conn.execute("SELECT COUNT(*) n FROM alphas").fetchone()["n"]
                cur = conn.execute(
                    "SELECT value FROM sync_state WHERE key='token_index_alphas'").fetchone()
                if cur is not None:
                    conn.execute("INSERT OR REPLACE INTO sync_state VALUES (?,?)",
                                 ('token_index_alphas', str(total)))
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    async def upsert_many(self, alphas: List[Dict[str, Any]], index: bool = False,
                          op_set: Optional[set] = None,
                          field_set: Optional[set] = None) -> int:
        """Store a batch of alphas.

        ``index`` also maintains the FTS and token indexes for these rows. The
        bulk sync leaves it off and rebuilds both once at the end (cheaper across
        hundreds of thousands of rows); a single alpha arriving from a finished
        backtest turns it on, so it is searchable and counted immediately rather
        than only after the next sync.
        """
        await self.ensure_ready()
        rows = [r for r in (self.row_from_alpha(a) for a in alphas) if r]
        async with self._lock:
            return await asyncio.to_thread(
                self._upsert_sync, rows, index, op_set or set(), field_set or set())

    async def rebuild_fts(self) -> None:
        await self.ensure_ready()

        def _rb():
            conn = self._connect()
            try:
                conn.execute("INSERT INTO alphas_fts(alphas_fts) VALUES('rebuild')")
                conn.commit()
            finally:
                conn.close()
        async with self._lock:
            await asyncio.to_thread(_rb)

    # --- Datafield search index -------------------------------------------- #

    _FTS_OPERATORS = re.compile(r'\b(AND|OR|NOT|NEAR)\b|["()*:^]')

    @staticmethod
    def fts_query(search: str) -> str:
        """Turn a user search string into a safe FTS5 query.

        A query already written in FTS5 syntax is passed through; anything else
        is quoted term-by-term and AND-ed, which both matches the previous
        keyword behaviour and stops punctuation from being read as syntax.
        """
        text = (search or '').strip()
        if not text:
            return ''
        if AlphaStore._FTS_OPERATORS.search(text):
            return text
        terms = [t for t in re.split(r'[^\w]+', text) if t]
        return ' AND '.join('"%s"' % t for t in terms)

    def _index_datafields_sync(self, fields: List[Dict[str, Any]],
                               configs: List[tuple]) -> Dict[str, Any]:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM datafields_fts")
            conn.execute("DELETE FROM datafields")
            conn.execute("DELETE FROM datafield_configs")
            conn.executemany("INSERT OR REPLACE INTO datafields VALUES (?,?,?,?,?,?)", fields)
            conn.executemany(
                "INSERT OR REPLACE INTO datafield_configs VALUES (?,?,?,?,?,?,?)", configs)
            conn.executemany("INSERT INTO datafields_fts(id, description) VALUES (?,?)",
                             [(f[0], f[1]) for f in fields if f[1]])
            conn.commit()
            return {'fields': len(fields), 'config_rows': len(configs)}
        finally:
            conn.close()

    async def index_datafields(self, fields: List[Dict[str, Any]],
                               configs: List[tuple]) -> Dict[str, Any]:
        await self.ensure_ready()
        async with self._lock:
            return await asyncio.to_thread(self._index_datafields_sync, fields, configs)

    async def search_datafields(self, search: str, region: Optional[str] = None,
                                universe: Optional[str] = None, delay: Optional[int] = None,
                                data_type: Optional[str] = None,
                                dataset_id: Optional[str] = None,
                                limit: int = 200) -> Optional[List[Dict[str, Any]]]:
        """Rank fields by relevance to ``search``. None when the index is empty."""
        await self.ensure_ready()
        q = self.fts_query(search)
        if not q:
            return None

        def _q():
            conn = self._connect()
            try:
                if not conn.execute("SELECT COUNT(*) n FROM datafields").fetchone()["n"]:
                    return None
                sql = ["SELECT d.id, d.description, d.type, d.dataset, d.category,",
                       "       d.date_created, c.coverage, c.user_count, c.alpha_count",
                       "FROM datafields_fts f",
                       "JOIN datafields d ON d.id = f.id",
                       "LEFT JOIN datafield_configs c ON c.id = d.id"]
                params: List[Any] = []
                cond = ["f.datafields_fts MATCH ?"]
                params.append(q)
                for col, val in (("c.region", region and region.upper()),
                                 ("c.universe", universe and universe.upper()),
                                 ("c.delay", delay)):
                    if val is not None:
                        cond.append(f"{col} = ?"); params.append(val)
                if data_type and data_type != 'ALL':
                    cond.append("d.type = ?"); params.append(data_type)
                if dataset_id:
                    cond.append("d.dataset = ?"); params.append(dataset_id)
                sql.append("WHERE " + " AND ".join(cond))
                sql.append("ORDER BY rank LIMIT ?")
                params.append(limit)
                try:
                    return [dict(r) for r in conn.execute(" ".join(sql), params).fetchall()]
                except sqlite3.OperationalError:
                    # Malformed FTS expression: fall back to the caller's filter.
                    return None
            finally:
                conn.close()
        return await asyncio.to_thread(_q)

    async def datafield_index_stats(self) -> Dict[str, Any]:
        await self.ensure_ready()

        def _s():
            conn = self._connect()
            try:
                return {
                    'fields': conn.execute("SELECT COUNT(*) n FROM datafields").fetchone()["n"],
                    'config_rows': conn.execute(
                        "SELECT COUNT(*) n FROM datafield_configs").fetchone()["n"],
                }
            finally:
                conn.close()
        return await asyncio.to_thread(_s)

    async def get_state(self, key: str) -> Optional[str]:
        await self.ensure_ready()

        def _g():
            conn = self._connect()
            try:
                r = conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
                return r["value"] if r else None
            finally:
                conn.close()
        return await asyncio.to_thread(_g)

    async def set_state(self, key: str, value: str) -> None:
        await self.ensure_ready()

        def _s():
            conn = self._connect()
            try:
                conn.execute("INSERT OR REPLACE INTO sync_state VALUES (?,?)", (key, value))
                conn.commit()
            finally:
                conn.close()
        async with self._lock:
            await asyncio.to_thread(_s)

    async def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        """Run a read-only query and return dict rows."""
        await self.ensure_ready()

        def _q():
            conn = self._connect()
            try:
                return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
            finally:
                conn.close()
        return await asyncio.to_thread(_q)

    async def build_token_index(self, operators: Sequence[str] = (),
                                fields: Sequence[str] = ()) -> Dict[str, Any]:
        """Index which operators and datafields each expression references.

        Classification is by lookup, not by parsing: the operator catalogue and
        the datafield catalogue are both already on disk, so a token is whatever
        those two sets say it is. Anything in neither is ignored rather than
        guessed at.
        """
        await self.ensure_ready()
        op_set = {o.lower() for o in operators if o}
        field_set = {f.lower() for f in fields if f}

        def _build():
            conn = self._connect()
            try:
                conn.execute("DELETE FROM alpha_tokens")
                rows = []
                seen_pairs = set()
                cur = conn.execute(
                    "SELECT id, expression FROM alphas WHERE expression IS NOT NULL")
                n_alphas = 0
                for aid, expr in cur:
                    n_alphas += 1
                    for tok in set(AlphaStore._TOKEN_RE.findall(expr or "")):
                        low = tok.lower()
                        if low in op_set:
                            kind = 'operator'
                        elif low in field_set:
                            kind = 'field'
                        else:
                            continue
                        pair = (aid, low)
                        if pair in seen_pairs:
                            continue
                        seen_pairs.add(pair)
                        rows.append((aid, low, kind))
                        if len(rows) >= 50000:
                            conn.executemany(
                                "INSERT OR IGNORE INTO alpha_tokens VALUES (?,?,?)", rows)
                            rows.clear()
                            seen_pairs.clear()
                if rows:
                    conn.executemany("INSERT OR IGNORE INTO alpha_tokens VALUES (?,?,?)", rows)
                conn.commit()
                total = conn.execute("SELECT COUNT(*) n FROM alpha_tokens").fetchone()["n"]
                corpus = conn.execute("SELECT COUNT(*) n FROM alphas").fetchone()["n"]
                # Remember the corpus size this index covers. A stale index does
                # not fail — it silently undercounts (measured: 15% low when the
                # corpus had grown from 92k to 116k), so the gap must be visible.
                conn.execute("INSERT OR REPLACE INTO sync_state VALUES (?,?)",
                             ('token_index_alphas', str(corpus)))
                conn.execute("INSERT OR REPLACE INTO sync_state VALUES (?,?)",
                             ('token_index_built_at', str(time.time())))
                conn.commit()
                return {'alphas_scanned': n_alphas, 'token_rows': total,
                        'covers_alphas': corpus,
                        'known_operators': len(op_set), 'known_fields': len(field_set)}
            finally:
                conn.close()
        async with self._lock:
            return await asyncio.to_thread(_build)

    async def stats(self) -> Dict[str, Any]:
        await self.ensure_ready()

        def _s():
            conn = self._connect()
            try:
                size = self.path.stat().st_size if self.path.exists() else 0
                total = conn.execute("SELECT COUNT(*) n FROM alphas").fetchone()["n"]
                if not total:
                    return {"alphas": 0, "bytes": size}
                span = conn.execute(
                    "SELECT MIN(date_created) lo, MAX(date_created) hi FROM alphas").fetchone()
                by_stage = {r["stage"]: r["n"] for r in conn.execute(
                    "SELECT stage, COUNT(*) n FROM alphas GROUP BY stage")}
                graded = conn.execute(
                    "SELECT COUNT(*) n FROM alphas WHERE sharpe IS NOT NULL").fetchone()["n"]
                tokens = conn.execute("SELECT COUNT(*) n FROM alpha_tokens").fetchone()["n"]
                covered = conn.execute(
                    "SELECT value FROM sync_state WHERE key='token_index_alphas'").fetchone()
                covered = int(covered["value"]) if covered else 0
                out = {
                    "alphas": total,
                    "with_metrics": graded,
                    "date_range": [span["lo"], span["hi"]],
                    "by_stage": by_stage,
                    "token_rows": tokens,
                    "token_index_covers": covered,
                    "bytes": size,
                }
                if tokens and covered < total:
                    out["token_index_stale_by"] = total - covered
                return out
            finally:
                conn.close()
        return await asyncio.to_thread(_s)


class EndpointRateLimiter:

    """Adaptive, per-endpoint-family throttle driven by BRAIN's own headers.

    Live probes show the platform publishes hard quotas that differ sharply per
    endpoint family, e.g. ``/data-fields`` allows 1 req/second and 30 req/minute
    while ``/alphas/{id}`` allows 2000 req/hour. Absorbing 429s and retrying is
    pure wasted traffic, so requests are paced *before* they are sent using the
    ``X-RateLimit-Limit-{Second,Minute,Hour}`` values learned from every
    response, plus an explicit cooldown whenever the server answers 429.

    Limits start unknown (no throttling) and are learned on the first response
    from each family, so a quota change on the platform side is picked up
    automatically instead of being baked into the client.
    """

    _WINDOWS = (("second", 1.0), ("minute", 60.0), ("hour", 3600.0))

    # One sliding window per (bucket, window) as a ZSET scored by wall-clock time.
    # Prune-count-admit has to be atomic or two processes both read "under quota"
    # and both send; a Lua body is the only way to get that in one round trip.
    # Returns 0 to admit, otherwise the seconds to wait.
    _ADMIT_LUA = """
    local now    = tonumber(ARGV[1])
    local span   = tonumber(ARGV[2])
    local budget = tonumber(ARGV[3])
    local member = ARGV[4]
    redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - span)
    local used = redis.call('ZCARD', KEYS[1])
    if used >= budget then
        local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
        local wait = span - (now - tonumber(oldest[2]))
        if wait < 0.05 then wait = 0.05 end
        return tostring(wait)
    end
    redis.call('ZADD', KEYS[1], now, member)
    redis.call('EXPIRE', KEYS[1], math.ceil(span) + 5)
    return "0"
    """

    def __init__(self, log, safety: float = 0.9, redis_client: Any = None):
        self._log = log
        self._safety = max(0.1, min(1.0, safety))
        self._state: Dict[str, Dict[str, Any]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        # Shared backend. Without it every process runs its own window and each
        # believes it owns the whole quota — measured: a standalone script and the
        # server together tripped "429 on data-sets" that neither would have hit
        # alone. Redis makes the budget one shared pool.
        self._redis = redis_client
        self._redis_ok = redis_client is not None
        self._admit_sha: Optional[str] = None
        self._degraded_logged = False
        # A transient Redis blip must not strand a long-lived server in
        # per-process mode forever, so degradation is re-probed periodically.
        self._redis_retry_at = 0.0
        try:
            self._redis_retry_seconds = max(5.0, float(
                os.environ.get("BRAIN_RATE_LIMIT_REDIS_RETRY_SECONDS", "30")))
        except Exception:
            self._redis_retry_seconds = 30.0

    @property
    def backend(self) -> str:
        if self._redis is None:
            return 'in-process'
        self._maybe_recover()
        return 'redis' if self._redis_ok else 'in-process'

    def _degrade(self, err: Exception) -> None:
        """Fall back to the in-process window; the server must stay usable."""
        self._redis_ok = False
        self._admit_sha = None
        self._redis_retry_at = time.time() + self._redis_retry_seconds
        if not self._degraded_logged:
            self._degraded_logged = True
            self._log(f"[rate-limit] Redis unavailable ({err}); pacing per-process "
                      "until it returns. Concurrent processes may now exceed quota.", "WARNING")

    def _maybe_recover(self) -> None:
        """Re-probe a degraded Redis so the shared budget comes back on its own."""
        if self._redis is None or self._redis_ok or time.time() < self._redis_retry_at:
            return
        self._redis_retry_at = time.time() + self._redis_retry_seconds
        try:
            self._redis.ping()
        except Exception:
            return
        self._redis_ok = True
        self._degraded_logged = False
        self._log("[rate-limit] Redis is back; sharing the quota again.", "INFO")

    def _redis_call(self, fn, *a, **kw):
        """Run a Redis op, degrading (not raising) on failure."""
        if self._redis is None or not self._redis_ok:
            return None
        try:
            return fn(*a, **kw)
        except Exception as e:
            self._degrade(e)
            return None

    def _budget(self, quota: int) -> int:
        return max(1, int(quota * self._safety)) if quota > 1 else quota

    def _shared_limits(self, bucket: str) -> Dict[str, int]:
        """Quotas learned by any process, so a fresh one need not rediscover them."""
        raw = self._redis_call(self._redis.hgetall, f'rl:limits:{bucket}') if self._redis else None
        out: Dict[str, int] = {}
        for k, v in (raw or {}).items():
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                continue
        return out

    async def _redis_wait(self, bucket: str) -> Optional[float]:
        """Seconds to wait per the shared windows, or None if Redis is unusable.

        0.0 means the request was admitted and already recorded.
        """
        self._maybe_recover()
        if self._redis is None or not self._redis_ok:
            return None
        cooldown = self._redis_call(self._redis.ttl, f'rl:cool:{bucket}')
        if cooldown is None:
            return None
        if cooldown > 0:
            return float(cooldown)

        limits = self._shared_limits(bucket)
        # Merge anything only this process has seen so far.
        for name, quota in (self._bucket_state(bucket)['limits'] or {}).items():
            limits.setdefault(name, quota)
        if not limits:
            return 0.0

        if self._admit_sha is None:
            self._admit_sha = self._redis_call(self._redis.script_load, self._ADMIT_LUA)
            if self._admit_sha is None:
                return None

        now = time.time()
        member = uuid.uuid4().hex
        admitted: List[str] = []
        for name, span in self._WINDOWS:
            quota = limits.get(name)
            if not quota:
                continue
            key = f'rl:hits:{bucket}:{name}'
            try:
                res = await asyncio.to_thread(
                    self._redis.evalsha, self._admit_sha, 1, key,
                    now, span, self._budget(quota), member)
            except Exception as e:
                # A flushed script cache is recoverable; anything else degrades.
                if 'NOSCRIPT' in str(e):
                    self._admit_sha = None
                else:
                    self._degrade(e)
                self._undo(admitted, member)
                return None
            wait = float(res)
            if wait > 0:
                # Roll back the windows already admitted so this attempt costs nothing.
                self._undo(admitted, member)
                return wait
            admitted.append(f'rl:hits:{bucket}:{name}')
        return 0.0

    def _undo(self, keys: List[str], member: str) -> None:
        for key in keys:
            self._redis_call(self._redis.zrem, key, member)

    @staticmethod
    def bucket_for(url: str) -> str:
        """Group URLs into the families the platform actually meters."""
        try:
            path = urlparse(url).path.strip('/')
        except Exception:
            return 'default'
        if not path:
            return 'default'
        parts = path.split('/')
        head = parts[0]
        if head == 'users':
            # /users/self/alphas is metered separately from /users/self/*
            if len(parts) >= 3 and parts[2] == 'alphas':
                return 'users-alphas'
            return 'users'
        if head in ('alphas', 'simulations', 'data-fields', 'data-sets',
                    'operators', 'events', 'competitions', 'consultant',
                    'tutorials', 'tutorial-pages', 'authentication'):
            return head
        return head

    async def _bucket_lock(self, bucket: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(bucket)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[bucket] = lock
            return lock

    def _bucket_state(self, bucket: str) -> Dict[str, Any]:
        state = self._state.get(bucket)
        if state is None:
            state = {
                'limits': {},                 # window name -> int quota
                'hits': deque(),              # monotonic timestamps of sent requests
                'blocked_until': 0.0,         # server-mandated cooldown (monotonic)
            }
            self._state[bucket] = state
        return state

    def _wait_seconds(self, state: Dict[str, Any], now: float) -> float:
        wait = max(0.0, state['blocked_until'] - now)
        hits: deque = state['hits']
        limits: Dict[str, int] = state['limits']
        if not limits:
            return wait
        longest = max(span for name, span in self._WINDOWS if name in limits)
        while hits and now - hits[0] > longest:
            hits.popleft()
        for name, span in self._WINDOWS:
            quota = limits.get(name)
            if not quota:
                continue
            # Keep a safety margin so a concurrent client/tab does not push us over.
            budget = max(1, int(quota * self._safety)) if quota > 1 else quota
            recent = [t for t in hits if now - t < span]
            if len(recent) >= budget:
                wait = max(wait, span - (now - recent[-budget]))
        return wait

    async def acquire(self, url: str) -> str:
        """Block until sending a request to ``url``'s family is within quota.

        Uses the Redis-shared windows when available so every process draws from
        one budget; falls back to the in-process window if Redis is unreachable.
        """
        bucket = self.bucket_for(url)
        lock = await self._bucket_lock(bucket)
        async with lock:
            state = self._bucket_state(bucket)
            while True:
                wait = await self._redis_wait(bucket)
                if wait is None:
                    # Redis unusable — pace locally.
                    now = time.monotonic()
                    wait = self._wait_seconds(state, now)
                    if wait <= 0:
                        state['hits'].append(now)
                        return bucket
                elif wait <= 0:
                    # Admitted and already recorded in the shared window. Mirror it
                    # locally so a later degrade still sees recent history.
                    state['hits'].append(time.monotonic())
                    return bucket
                if wait > 1.0:
                    self._log(
                        f"[rate-limit] Pacing {bucket} ({self.backend}): sleeping "
                        f"{wait:.1f}s to stay within {state['limits'] or self._shared_limits(bucket)}",
                        "INFO",
                    )
                await asyncio.sleep(min(wait, 30.0))

    def observe(self, bucket: str, response: Optional[requests.Response]) -> None:
        """Learn quotas and cooldowns from a response's rate-limit headers."""
        if response is None:
            return
        state = self._bucket_state(bucket)
        headers = response.headers
        for name, _span in self._WINDOWS:
            raw = headers.get(f'X-RateLimit-Limit-{name.capitalize()}')
            if raw is None:
                continue
            try:
                value = int(float(raw))
            except (TypeError, ValueError):
                continue
            if value > 0:
                state['limits'][name] = value
                # Publish it so other processes inherit the quota immediately.
                self._redis_call(self._redis.hset, f'rl:limits:{bucket}', name, value) \
                    if self._redis else None
        # Remaining==0 means the next request is guaranteed to 429; wait out the window.
        for name, span in self._WINDOWS:
            raw = headers.get(f'X-RateLimit-Remaining-{name.capitalize()}')
            if raw is None:
                continue
            try:
                remaining = int(float(raw))
            except (TypeError, ValueError):
                continue
            if remaining <= 0:
                state['blocked_until'] = max(state['blocked_until'], time.monotonic() + span)
                if self._redis:
                    self._redis_call(self._redis.set, f'rl:cool:{bucket}', '1',
                                     ex=max(1, int(span)), nx=True)
        if response.status_code == 429:
            cooldown = 30.0
            raw = headers.get('Retry-After') or headers.get('RateLimit-Reset')
            try:
                if raw is not None:
                    cooldown = max(1.0, min(float(raw), 300.0))
            except (TypeError, ValueError):
                pass
            state['blocked_until'] = max(state['blocked_until'], time.monotonic() + cooldown)
            if self._redis:
                # Share the cooldown: a 429 is account-wide, not process-wide.
                self._redis_call(self._redis.set, f'rl:cool:{bucket}', '1',
                                 ex=max(1, int(cooldown)))
            self._log(f"[rate-limit] 429 on {bucket}; cooling down {cooldown:.0f}s", "WARNING")

    def snapshot(self) -> Dict[str, Any]:
        now = time.monotonic()
        out: Dict[str, Any] = {}
        # Touch Redis before reporting the backend: reading the flag first would
        # report a stale 'redis' for one call after the connection dropped.
        self._maybe_recover()
        if self._redis is not None and self._redis_ok:
            self._redis_call(self._redis.ping)
        buckets = set(self._state)
        if self._redis is not None and self._redis_ok:
            keys = self._redis_call(self._redis.keys, 'rl:limits:*') or []
            buckets |= {k.split(':', 2)[-1] for k in keys}
        for bucket in sorted(buckets):
            state = self._bucket_state(bucket)
            row = {
                'limits': dict(state['limits']) or self._shared_limits(bucket),
                'sent_last_minute_this_process': sum(1 for t in state['hits'] if now - t < 60.0),
                'cooldown_seconds': round(max(0.0, state['blocked_until'] - now), 1),
            }
            if self.backend == 'redis':
                # What every process together has spent — the number that matters.
                shared = self._redis_call(
                    self._redis.zcount, f'rl:hits:{bucket}:minute', time.time() - 60, '+inf')
                if shared is not None:
                    row['sent_last_minute_all_processes'] = shared
                ttl = self._redis_call(self._redis.ttl, f'rl:cool:{bucket}')
                if ttl and ttl > 0:
                    row['cooldown_seconds'] = float(ttl)
            out[bucket] = row
        out['_backend'] = self.backend
        return out


class BrainApiClient:
    """WorldQuant BRAIN API client with comprehensive functionality."""
    
    def __init__(self):
        # Best-effort: load .env early so env overrides are available here
        try:
            from dotenv import load_dotenv, find_dotenv
            env_path = find_dotenv(usecwd=True)
            if env_path:
                load_dotenv(env_path, override=False)
            else:
                candidate = Path(__file__).parent / ".env"
                if candidate.exists():
                    load_dotenv(candidate, override=False)
        except Exception:
            # Fallback: simple parser
            try:
                candidate = Path(__file__).parent / ".env"
                if candidate.exists():
                    for line in candidate.read_text().splitlines():
                        line = line.strip()
                        if not line or line.startswith('#') or '=' not in line:
                            continue
                        k, v = line.split('=', 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        os.environ.setdefault(k, v)
            except Exception:
                pass

        self.base_url = "https://api.worldquantbrain.com"
        self.session = requests.Session()
        self.auth_credentials = None
        self.is_authenticating = False
        self._max_concurrency = int(os.environ.get("BRAIN_MAX_CONCURRENCY", "8"))
        self._request_semaphore = asyncio.Semaphore(self._max_concurrency)
        # Requests are paced per endpoint family from the platform's own
        # RateLimit headers; the semaphore only bounds in-flight sockets.
        try:
            _rl_safety = float(os.environ.get("BRAIN_RATE_LIMIT_SAFETY", "0.9"))
        except Exception:
            _rl_safety = 0.9
        # redis_client is created further down; the limiter is attached to it in
        # _attach_rate_limiter_backend() once that connection exists.
        self.rate_limiter = EndpointRateLimiter(self.log, safety=_rl_safety)
        self._auth_lock = asyncio.Lock()
        self._self_user_id: Optional[str] = None
        self._self_user_id_lock = asyncio.Lock()
        self._auth_validated_until = 0.0
        try:
            self._auth_check_ttl_seconds = max(0.0, float(os.environ.get("BRAIN_AUTH_CHECK_TTL_SECONDS", "300")))
        except Exception:
            self._auth_check_ttl_seconds = 300.0
        # Platform correlation (prod + power-pool) is gated by ONE per-account
        # slot that is both a mutex AND a rate limit: only one check may run at
        # a time, and a finished check leaves a cooldown behind so the next one
        # cannot start until BRAIN_CORRELATION_MIN_INTERVAL_SECONDS have passed.
        # Held cross-process in Redis AND in a host-level flock file.
        try:
            self._brain_correlation_min_interval_seconds = max(
                0,
                int(os.environ.get("BRAIN_CORRELATION_MIN_INTERVAL_SECONDS", "180")),
            )
        except Exception:
            self._brain_correlation_min_interval_seconds = 180
        self._os_pnl_pool_locks: Dict[str, asyncio.Lock] = {}
        self._os_pnl_pool_locks_guard = asyncio.Lock()
        self._os_pnl_pool_last_sync: Dict[str, Any] = {}
        try:
            self._os_pnl_pool_sync_debounce_seconds = max(
                0.0,
                float(os.environ.get("BRAIN_SC_POOL_SYNC_DEBOUNCE_SECONDS", "1")),
            )
        except Exception:
            self._os_pnl_pool_sync_debounce_seconds = 1.0
        # The server-side OS alpha list is the ONLY request a warm self-correlation
        # check still makes. Within this window the cached list is reused with no
        # request at all; past it the list is *verified* with a single 1-row probe
        # (see _os_list_probe) rather than re-paged. Set 0 to verify on every call.
        try:
            self._os_list_ttl_seconds = max(0, int(os.environ.get("BRAIN_OS_LIST_TTL_SECONDS", "300")))
        except Exception:
            self._os_list_ttl_seconds = 300
        # How long a verified list entry is kept around to be re-verified against.
        try:
            self._os_list_retain_seconds = max(60, int(os.environ.get("BRAIN_OS_LIST_RETAIN_SECONDS", "86400")))
        except Exception:
            self._os_list_retain_seconds = 86400
        try:
            self._brain_correlation_busy_retry_after_seconds = max(
                1,
                int(os.environ.get("BRAIN_CORRELATION_BUSY_RETRY_AFTER_SECONDS", "180")),
            )
        except Exception:
            self._brain_correlation_busy_retry_after_seconds = 180
        # Allow timeout override via env (e.g., API_SETTINGS_TIMEOUT)
        try:
            self._default_timeout_seconds = int(os.environ.get("API_SETTINGS_TIMEOUT", "30"))
        except Exception:
            self._default_timeout_seconds = 30
        self._create_simulation_semaphore = asyncio.Semaphore(int(os.environ.get("BRAIN_CREATE_SIMULATION_MAX_CONCURRENCY", "6")))
        try:
            self._forum_rate_limit_seconds = max(0, int(os.environ.get("FORUM_RATE_LIMIT_SECONDS", "0")))
        except Exception:
            self._forum_rate_limit_seconds = 0
        self._forum_rate_limit_lock = asyncio.Lock()
        self._forum_rate_limit_until = 0.0
        # Async submission tracking: platform-side submission checks can take
        # minutes up to ~1 hour, so submissions run as background tasks and
        # their progress is tracked here (keyed by alpha_id).
        self._submission_states: Dict[str, Dict[str, Any]] = {}
        self._submission_tasks: Dict[str, asyncio.Task] = {}
        # Catalogue building runs for hours, so it lives as a background task in
        # THIS process: that way it shares self.rate_limiter with foreground
        # requests. A separate process would run a second limiter that believes
        # it owns the whole 30/min budget, and the two would collide into 429s.
        self._catalogue_state: Dict[str, Any] = {}
        self._catalogue_task: Optional[asyncio.Task] = None
        # The platform processes one submission check at a time per account.
        self._submission_serialize_lock = asyncio.Lock()
        try:
            self._submit_max_seconds = max(300.0, float(os.environ.get("BRAIN_SUBMIT_MAX_SECONDS", "5400")))
        except Exception:
            self._submit_max_seconds = 5400.0
        # Permanent on-disk store for immutable platform data. Redis stays the
        # hot tier for things that actually change (alpha lists, pyramid stats).
        store_root = os.environ.get("BRAIN_CACHE_DIR") or str(Path(__file__).parent / "cache")
        self.store = PersistentStore(Path(store_root), self.log)
        # Structured corpus of this account's own alphas. Separate from the file
        # store because 678k records need indexes, not 678k files.
        self.alpha_store = AlphaStore(Path(store_root) / "alphas.db", self.log)
        self._alpha_sync_state: Dict[str, Any] = {}
        self._alpha_sync_task: Optional[asyncio.Task] = None
        # Operator/datafield name sets used to classify expression tokens.
        # Reading 73k field names off disk on every finished backtest would be
        # absurd, so they are loaded once and refreshed on a long interval.
        self._token_vocab: Optional[Dict[str, set]] = None
        self._token_vocab_at = 0.0
        try:
            self._token_vocab_ttl = max(60, int(
                os.environ.get("BRAIN_TOKEN_VOCAB_TTL_SECONDS", "3600")))
        except Exception:
            self._token_vocab_ttl = 3600
        # An IS alpha is still editable, so it gets a short Redis TTL rather than
        # a permanent record; OS alphas go to the store.
        try:
            self._alpha_details_is_ttl = max(0, int(os.environ.get("BRAIN_ALPHA_DETAILS_IS_TTL", "120")))
        except Exception:
            self._alpha_details_is_ttl = 120
        # A submitted alpha's expression, settings and IS metrics are frozen, but
        # its `os` block is NOT: osISSharpeRatio / preCloseSharpeRatio fill in and
        # keep moving as out-of-sample performance accrues (verified: alphas
        # submitted 2025-03 report osISSharpeRatio while one submitted today
        # reports null). So the stored record is served only while it is younger
        # than this window; 0 disables the check and makes it truly permanent.
        try:
            self._alpha_details_max_age = max(0, int(os.environ.get("BRAIN_ALPHA_DETAILS_MAX_AGE", "604800")))
        except Exception:
            self._alpha_details_max_age = 604800
        
        # Configure session. The default HTTPAdapter pool holds only 10
        # connections; with concurrent requests a smaller pool silently discards
        # and re-establishes sockets, paying a TLS handshake per call.
        _pool = max(10, self._max_concurrency * 2)
        _adapter = requests.adapters.HTTPAdapter(
            pool_connections=_pool, pool_maxsize=_pool, max_retries=0
        )
        self.session.mount('https://', _adapter)
        self.session.mount('http://', _adapter)
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
        # Load OS/IS Sharpe ratio data for datafield quality filtering
        self._isos_data = {}
        try:
            info_data_path = Path(__file__).parent / 'config' / 'info_data.bin'
            if info_data_path.exists():
                with open(info_data_path, 'rb') as f:
                    self._isos_data = msgpack.unpackb(zlib.decompress(f.read()), raw=False)
                self.log(f"Loaded OS/IS Sharpe data: {len(self._isos_data)} region_delay entries", "INFO")
            else:
                self.log(f"OS/IS Sharpe data file not found at {info_data_path}, sharpe filtering disabled", "WARNING")
        except Exception as e:
            self.log(f"Failed to load OS/IS Sharpe data: {str(e)}, sharpe filtering disabled", "WARNING")

        # Initialize Redis connection
        try:
            redis_host = os.environ.get('REDIS_HOST', 'localhost')
            try:
                redis_port = int(os.environ.get('REDIS_PORT', str(6379)))
            except Exception:
                redis_port = 6379

            self.redis_client = redis.Redis(
                host=redis_host,
                port=redis_port,
                db=0,
                decode_responses=True,
                socket_connect_timeout=5
            )
            # Test connection
            self.redis_client.ping()
            self.log("Redis connection established", "INFO")
        except Exception as e:
            self.log(f"Redis connection failed: {str(e)}, caching disabled", "WARNING")
            self.redis_client = None

        # Share the rate-limit budget across processes now that Redis is known.
        if self.redis_client is not None:
            self.rate_limiter._redis = self.redis_client
            self.rate_limiter._redis_ok = True
            self.log(f"Rate limiting backend: {self.rate_limiter.backend}", "INFO")
    
    def log(self, message: str, level: str = "INFO"):
        """Log messages to stderr to avoid MCP protocol interference."""
        print(f"[{level}] {message}", file=sys.stderr)
    
    def _to_absolute_url(self, url: str) -> str:
        if not url:
            return url
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return urljoin(self.base_url, url)

    def _response_payload(self, response: requests.Response) -> Any:
        """Return JSON when possible, otherwise response text for diagnostics."""
        try:
            return response.json()
        except ValueError:
            return response.text

    def _simulation_error_message(self, data: Any) -> str:
        """Extract the most useful error text from a simulation progress payload."""
        if not isinstance(data, dict):
            return str(data) if data is not None else "Unknown error"

        for key in ("error", "message", "detail", "details", "statusMessage", "status"):
            value = data.get(key)
            if value:
                if isinstance(value, (dict, list)):
                    return json.dumps(value, ensure_ascii=False)
                return str(value)

        collected: list[str] = []

        def visit(node: Any) -> None:
            if len(collected) >= 8:
                return
            if isinstance(node, dict):
                for key, value in node.items():
                    lower = str(key).lower()
                    if any(token in lower for token in ("error", "message", "exception", "traceback")) and value:
                        if isinstance(value, (dict, list)):
                            collected.append(json.dumps(value, ensure_ascii=False))
                        else:
                            collected.append(str(value))
                    visit(value)
            elif isinstance(node, list):
                for item in node:
                    visit(item)

        visit(data)
        return " | ".join(collected) if collected else "Unknown error"

    def _generate_cache_key(self, prefix: str, params: dict) -> str:
        """Generate a cache key from prefix and parameters."""
        # Sort params to ensure consistent key generation
        sorted_params = sorted(params.items())
        param_str = json.dumps(sorted_params, sort_keys=True)
        hash_str = hashlib.md5(param_str.encode()).hexdigest()
        return f"{prefix}:{hash_str}"
    
    def _get_cached_data(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Get data from Redis cache."""
        if not self.redis_client:
            return None
        try:
            cached = self.redis_client.get(cache_key)
            if cached:
                self.log(f"Cache hit for key: {cache_key}", "INFO")
                return json.loads(cached)
        except Exception as e:
            self.log(f"Cache read error: {str(e)}", "WARNING")
        return None
    
    def _set_cached_data(self, cache_key: str, data: Dict[str, Any], ttl: int = 604800):
        """Set data in Redis cache with TTL (default 1 week = 604800 seconds)."""
        if not self.redis_client:
            return
        try:
            self.redis_client.setex(cache_key, ttl, json.dumps(data))
            self.log(f"Cached data with key: {cache_key}, TTL: {ttl}s", "INFO")
        except Exception as e:
            self.log(f"Cache write error: {str(e)}", "WARNING")

    # Lua: take the slot only if the key is absent. A present key means either
    # another check is in flight (value 'held:<token>') or the post-check
    # cooldown has not elapsed (value 'cooldown'); both must block.
    _CORR_ACQUIRE_LUA = """
    local cur = redis.call('GET', KEYS[1])
    if cur then return {0, redis.call('TTL', KEYS[1]), cur} end
    redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
    return {1, tonumber(ARGV[2]), ARGV[1]}
    """

    # Lua: releasing does NOT free the slot — it DOWNGRADES the key to the
    # cooldown marker, so the interval between two checks is enforced from the
    # moment the previous one finished. Only the holder may downgrade.
    _CORR_RELEASE_LUA = """
    if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
    local cd = tonumber(ARGV[3])
    if cd > 0 then
      redis.call('SET', KEYS[1], ARGV[2], 'EX', cd)
      return 1
    end
    redis.call('DEL', KEYS[1])
    return 2
    """

    _CORR_COOLDOWN_VALUE = "cooldown"

    def _brain_correlation_lock_key(self) -> str:
        """Per-account lock key. BRAIN's correlation concurrency limit is per
        account, so multi-account deployments sharing one Redis must not block
        each other."""
        email = (self.auth_credentials or {}).get('email') if self.auth_credentials else None
        if not email:
            try:
                email = (load_config().get('credentials') or {}).get('email')
            except Exception:
                email = None
        if email:
            digest = hashlib.md5(email.strip().lower().encode()).hexdigest()[:12]
            return f"lock:brain_correlation:{digest}"
        return "lock:brain_correlation"

    def _brain_correlation_max_wait(self) -> int:
        """Total polling budget for one platform correlation check."""
        try:
            return max(60, int(os.environ.get("BRAIN_CORRELATION_MAX_WAIT_SECONDS", "3600")))
        except Exception:
            return 3600

    def _brain_correlation_lock_ttl(self) -> int:
        """Slot TTL — the crash safety net, not the normal release path.

        It must never expire under a holder that is still polling (that would
        hand the slot to a second process mid-check), so it is always kept
        above the poll budget plus a margin no matter what the env says.
        """
        try:
            ttl = max(60, int(os.environ.get("BRAIN_CORRELATION_LOCK_TTL_SECONDS", "7200")))
        except Exception:
            ttl = 7200
        return max(ttl, self._brain_correlation_max_wait() + 300)

    def _brain_correlation_lock_file(self) -> Path:
        """Host-level slot file. Shared by every process that mounts the cache
        volume — never fall back to a process-local lock, that would let N
        processes hit BRAIN at once."""
        root = os.environ.get("BRAIN_CACHE_DIR") or str(Path(__file__).parent / "cache")
        lock_dir = Path(root) / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        safe = self._brain_correlation_lock_key().replace(':', '_')
        return lock_dir / f"{safe}.json"

    def _file_slot_transact(self, mutate):
        """Run ``mutate(state)`` under an exclusive flock on the slot file.

        ``state`` is the decoded slot ({'value', 'reason', 'expires_at'}) with
        expiry already applied, or {} when free. ``mutate`` returns
        (new_state_or_None, result); a None state leaves the file untouched.
        The critical section is a couple of file ops, so blocking the loop here
        is cheaper than the machinery needed to avoid it.
        """
        path = self._brain_correlation_lock_file()
        with open(path, 'a+', encoding='utf-8') as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                raw = fh.read().strip()
                try:
                    state = json.loads(raw) if raw else {}
                except Exception:
                    state = {}
                if not isinstance(state, dict) or float(state.get('expires_at') or 0) <= time.time():
                    state = {}
                new_state, result = mutate(state)
                if new_state is not None:
                    fh.seek(0)
                    fh.truncate()
                    fh.write(json.dumps(new_state))
                    fh.flush()
                    os.fsync(fh.fileno())
                return result
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    async def _try_acquire_brain_correlation_lock(self, op_name: str) -> Dict[str, Any]:
        """Try once to take the per-account platform correlation slot.

        The slot lives in TWO layers and BOTH must admit the caller:

        * the host-level flock file (``cache/locks/``) — every process sharing
          the cache volume passes through it, so the slot still holds while
          Redis is down or has evicted the key (this deployment runs Redis with
          ``volatile-lru``, and a TTL'd lock key is an eviction candidate);
        * Redis — the only layer that reaches processes on other hosts.

        Either layer reporting busy is enough to refuse, so the slot is never
        handed out twice: at most one platform correlation check (production or
        power-pool) is in flight per account at any instant, and the next one
        may not START until BRAIN_CORRELATION_MIN_INTERVAL_SECONDS after the
        previous one finished. Never queues — always fails fast.
        """
        lock_key = self._brain_correlation_lock_key()
        lock_ttl = self._brain_correlation_lock_ttl()
        cooldown = self._brain_correlation_min_interval_seconds
        held_value = f"held:{uuid.uuid4().hex}"

        def _busy(layer: str, ttl: Any, current: Any) -> Dict[str, Any]:
            reason = 'cooldown' if current == self._CORR_COOLDOWN_VALUE else 'running'
            try:
                ttl = int(ttl)
            except Exception:
                ttl = -1
            self.log(
                f"[corr-lock] Busy {lock_key} for {op_name} "
                f"(reason={reason}, remaining={ttl}s, layer={layer})",
                "INFO",
            )
            return {
                'acquired': False,
                'backend': layer,
                'lock_key': lock_key,
                'reason': reason,
                'retry_after': ttl if ttl > 0 else None,
            }

        # --- layer 1: host-level file slot ---------------------------------- #
        def _take(state):
            if state.get('value'):
                return None, ('busy', state.get('expires_at', 0), state.get('value'))
            return (
                {'value': held_value, 'reason': 'running', 'expires_at': time.time() + lock_ttl},
                ('ok', 0, held_value),
            )

        layers: List[str] = []
        try:
            outcome, expires_at, current = self._file_slot_transact(_take)
            if outcome == 'busy':
                return _busy('file', max(0, int(float(expires_at) - time.time())), current)
            layers.append('file')
        except Exception as e:
            self.log(
                f"[corr-lock] File slot unusable for {op_name}: {e}. Relying on Redis alone.",
                "WARNING",
            )

        # --- layer 2: cross-host Redis slot --------------------------------- #
        if self.redis_client:
            try:
                ok, ttl, current = self.redis_client.eval(
                    self._CORR_ACQUIRE_LUA, 1, lock_key, held_value, lock_ttl
                )
                if int(ok) != 1:
                    # Another host owns the account slot — hand the file layer
                    # straight back; no check ran, so no cooldown is owed.
                    self._abandon_file_slot(held_value, layers)
                    return _busy('redis', ttl, current)
                layers.append('redis')
            except Exception as e:
                self.log(
                    f"[corr-lock] Redis error acquiring lock for {op_name}: {e}. "
                    "Holding the host-level file slot only.",
                    "WARNING",
                )

        if not layers:
            # Neither layer can arbitrate. Refuse rather than let every process
            # think it is alone — a missed check is recoverable, a stampede
            # against BRAIN is not.
            self.log(f"[corr-lock] No usable lock backend for {op_name}", "ERROR")
            return {
                'acquired': False,
                'backend': 'unavailable',
                'lock_key': lock_key,
                'reason': 'lock_backend_unavailable',
                'retry_after': None,
            }

        self.log(
            f"[corr-lock] Acquired {lock_key} for {op_name} "
            f"(layers={'+'.join(layers)}, ttl={lock_ttl}s, cooldown_after={cooldown}s)",
            "INFO",
        )
        return {
            'acquired': True,
            'backend': '+'.join(layers),
            'layers': layers,
            'lock_key': lock_key,
            'lock_token': held_value,
        }

    def _abandon_file_slot(self, token: str, layers: List[str]):
        """Drop the file layer without starting a cooldown (no check ran)."""
        if 'file' not in layers:
            return

        def _clear(state):
            if state.get('value') != token:
                return None, False
            return {}, True

        try:
            self._file_slot_transact(_clear)
            layers.remove('file')
        except Exception as e:
            self.log(f"[corr-lock] Failed to abandon file slot: {e}", "WARNING")

    async def _release_brain_correlation_lock(self, lock_info: Dict[str, Any], op_name: str):
        """Hand every held layer back as a cooldown, not as a free slot."""
        if not lock_info or not lock_info.get('acquired'):
            return
        cooldown = self._brain_correlation_min_interval_seconds
        lock_key = lock_info['lock_key']
        token = lock_info['lock_token']
        layers = lock_info.get('layers') or []

        if 'redis' in layers and self.redis_client:
            try:
                self.redis_client.eval(
                    self._CORR_RELEASE_LUA, 1, lock_key,
                    token, self._CORR_COOLDOWN_VALUE, cooldown,
                )
            except Exception as e:
                # The held key still carries the 2h TTL. Leaving it is the safe
                # failure: it blocks, it does not stampede.
                self.log(f"[corr-lock] Redis lock release failed for {op_name}: {e}", "WARNING")

        if 'file' in layers:
            def _downgrade(state):
                if state.get('value') != token:
                    return None, False
                if cooldown <= 0:
                    return {}, True
                return (
                    {
                        'value': self._CORR_COOLDOWN_VALUE,
                        'reason': 'cooldown',
                        'expires_at': time.time() + cooldown,
                    },
                    True,
                )

            try:
                self._file_slot_transact(_downgrade)
            except Exception as e:
                self.log(f"[corr-lock] File lock release failed for {op_name}: {e}", "WARNING")

        self.log(
            f"[corr-lock] Released {lock_key} for {op_name} (layers={'+'.join(layers)}); "
            f"next platform correlation check allowed in {cooldown}s",
            "INFO",
        )

    async def _rate_limit_forum_op(self, op_name: str) -> Optional[Dict[str, Any]]:
        if self._forum_rate_limit_seconds <= 0:
            return None

        if self.redis_client:
            try:
                lock_key = "rate_limit:forum_ops"
                if not self.redis_client.set(lock_key, "locked", ex=self._forum_rate_limit_seconds, nx=True):
                    ttl = self.redis_client.ttl(lock_key)
                    if not isinstance(ttl, int) or ttl < 0:
                        ttl = self._forum_rate_limit_seconds
                    return {
                        'status': 'rate_limited',
                        'message': f"Rate limit exceeded. Please wait {ttl} seconds before trying again.",
                        'retry_after': ttl,
                    }
            except Exception as e:
                self.log(f"Rate limiting for {op_name} failed, falling back to local limiter: {str(e)}", "WARNING")

        async with self._forum_rate_limit_lock:
            now = time.time()
            until = float(self._forum_rate_limit_until)
            if now < until:
                ttl = int(until - now)
                if ttl < 0:
                    ttl = 0
                return {
                    'status': 'rate_limited',
                    'message': f"Rate limit exceeded. Please wait {ttl} seconds before trying again.",
                    'retry_after': ttl,
                }
            self._forum_rate_limit_until = now + self._forum_rate_limit_seconds
            return None
    
    async def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Run blocking requests I/O in a worker thread to avoid blocking the asyncio event loop.

        Requests are paced by ``self.rate_limiter`` using the platform's own
        RateLimit headers, so concurrent callers stay inside BRAIN's per-family
        quotas instead of generating 429s and retry traffic. ``requests.Session``
        is used concurrently here on purpose: connection pooling and the cookie
        jar are only mutated during authentication, which holds ``_auth_lock``.
        """
        absolute_url = self._to_absolute_url(url)
        timeout = kwargs.pop("timeout", self._default_timeout_seconds)
        # Add extra buffer for asyncio timeout to catch stuck threads
        asyncio_timeout = timeout + 10

        bucket = await self.rate_limiter.acquire(absolute_url)

        async with self._request_semaphore:
            try:
                # Wrap asyncio.to_thread with wait_for to prevent infinite hangs
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.session.request,
                        method,
                        absolute_url,
                        timeout=timeout,
                        **kwargs,
                    ),
                    timeout=asyncio_timeout
                )
                self.rate_limiter.observe(bucket, response)
                return response
            except asyncio.TimeoutError:
                self.log(f"Request asyncio timeout for {method} {absolute_url} after {asyncio_timeout}s", "ERROR")
                raise TimeoutError(f"Request timed out after {asyncio_timeout}s")
            except asyncio.CancelledError:
                self.log(f"Request cancelled for {method} {absolute_url}", "WARNING")
                raise
            except requests.Timeout as e:
                self.log(f"Request timeout for {method} {absolute_url}: {str(e)}", "ERROR")
                raise TimeoutError(f"Request timed out after {timeout}s") from e
            except requests.ConnectionError as e:
                self.log(f"Connection error for {method} {absolute_url}: {str(e)}", "ERROR")
                raise ConnectionError(f"Failed to connect to {absolute_url}") from e
            except requests.HTTPError as e:
                self.log(f"HTTP error for {method} {absolute_url}: {str(e)}", "ERROR")
                raise
            except Exception as e:
                # Catch other unexpected errors (e.g., RemoteDisconnected wrapped in other exceptions)
                error_str = str(e)
                if "RemoteDisconnected" in error_str or "Connection aborted" in error_str:
                    self.log(f"Remote disconnected for {method} {absolute_url}: {error_str}", "ERROR")
                    raise ConnectionError(f"Remote server disconnected: {absolute_url}") from e
                raise

    @staticmethod
    def _recordset_retry_after(response: Optional[requests.Response], fallback: float) -> float:
        """Wait BRAIN asks for while it materialises a recordset.

        The /alphas/{id}/recordsets/* endpoints answer 200 with an EMPTY body and
        a Retry-After header (observed: 1.0) until the record is built. Obeying
        the header instead of a fixed backoff halves the wait on every cold read.
        """
        try:
            value = float(response.headers.get('Retry-After'))
        except (TypeError, ValueError, AttributeError):
            return fallback
        return max(0.25, min(value, 30.0))

    def _retry_wait_seconds(self, response: Optional[requests.Response], attempt: int, base_delay: float = 2.0, max_delay: float = 60.0) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(max(float(retry_after), 0.0), max_delay)
                except (TypeError, ValueError):
                    pass
        backoff = min(base_delay * (1.6 ** attempt), max_delay)
        return backoff + random.uniform(0, min(1.0, backoff * 0.1))

    async def _request_json_with_retries(
        self,
        method: str,
        url: str,
        *,
        op_name: str,
        max_retries: int = 6,
        retry_statuses: Optional[set] = None,
        allow_empty: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """Request JSON with bounded retries for bulk/paginated endpoints."""
        retry_statuses = retry_statuses or {429, 500, 502, 503, 504}
        last_error: Optional[Exception] = None

        for attempt in range(max_retries):
            response: Optional[requests.Response] = None
            try:
                response = await self._request(method, url, **kwargs)
                if response.status_code == 401:
                    self._auth_validated_until = 0.0
                    if attempt < max_retries - 1:
                        self.log(
                            f"{op_name}: HTTP 401, refreshing authentication "
                            f"(attempt {attempt + 1}/{max_retries})",
                            "WARNING",
                        )
                        await self.ensure_authenticated()
                        continue
                    response.raise_for_status()
                if response.status_code in retry_statuses:
                    wait = self._retry_wait_seconds(response, attempt)
                    self.log(
                        f"{op_name}: HTTP {response.status_code}, retrying in {wait:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries})",
                        "WARNING",
                    )
                    await asyncio.sleep(wait)
                    continue

                response.raise_for_status()
                text = (response.text or "").strip()
                if not text:
                    if allow_empty:
                        return {}
                    wait = self._retry_wait_seconds(response, attempt)
                    self.log(
                        f"{op_name}: empty response, retrying in {wait:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries})",
                        "WARNING",
                    )
                    await asyncio.sleep(wait)
                    continue
                try:
                    return response.json() or {}
                except json.JSONDecodeError as e:
                    last_error = e
                    wait = self._retry_wait_seconds(response, attempt)
                    self.log(
                        f"{op_name}: JSON parse failed, retrying in {wait:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries})",
                        "WARNING",
                    )
                    await asyncio.sleep(wait)
                    continue
            except requests.HTTPError:
                raise
            except (ConnectionError, TimeoutError, requests.RequestException) as e:
                last_error = e
                wait = self._retry_wait_seconds(response, attempt)
                self.log(
                    f"{op_name}: transient request failure ({e}), retrying in {wait:.1f}s "
                    f"(attempt {attempt + 1}/{max_retries})",
                    "WARNING",
                )
                await asyncio.sleep(wait)

        if last_error:
            raise last_error
        raise RuntimeError(f"{op_name}: failed after {max_retries} attempts")
    
    async def authenticate(self, email: str, password: str) -> Dict[str, Any]:
        """Authenticate with WorldQuant BRAIN platform with biometric support."""
        async with self._auth_lock:
            return await self._authenticate_unlocked(email, password)

    async def _authenticate_unlocked(self, email: str, password: str) -> Dict[str, Any]:
        """Authenticate while ``_auth_lock`` is already held."""
        self.log("🔐 Starting Authentication process...", "INFO")
        auth_timeout = self._default_timeout_seconds + 10  # Extra buffer for asyncio timeout
        
        try:
            # Store credentials for potential re-authentication
            self.auth_credentials = {'email': email, 'password': password}
            self._auth_validated_until = 0.0
            
            # Clear any existing session data (quick operation, no lock needed for this)
            self.session.cookies.clear()
            self.session.auth = None
            self._self_user_id = None
            
            # Create Basic Authentication header (base64 encoded credentials)
            import base64
            credentials = f"{email}:{password}"
            encoded_credentials = base64.b64encode(credentials.encode()).decode()
            
            # Send POST request with Basic Authentication header
            headers = {
                'Authorization': f'Basic {encoded_credentials}'
            }
            
            # Use a direct thread call with timeout, no nested locks
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.session.request,
                        'POST',
                        'https://api.worldquantbrain.com/authentication',
                        headers=headers,
                        timeout=self._default_timeout_seconds,
                    ),
                    timeout=auth_timeout
                )
            except asyncio.TimeoutError:
                self.log(f"❌ Authentication request timed out after {auth_timeout}s", "ERROR")
                raise TimeoutError(f"Authentication timed out after {auth_timeout}s")

            # Check for successful authentication (status code 201)
            if response.status_code == 201:
                self.log("Authentication successful", "SUCCESS")
                
                # Check if JWT token was automatically stored by session
                jwt_token = self.session.cookies.get('t')
                if jwt_token:
                    self._auth_validated_until = time.time() + self._auth_check_ttl_seconds
                    self.log("JWT token automatically stored by session", "SUCCESS")
                
                # Return success response
                return {
                    'user': {'email': email},
                    'status': 'authenticated',
                    'permissions': ['read', 'write'],
                    'message': 'Authentication successful',
                    'status_code': response.status_code,
                    'has_jwt': jwt_token is not None
                }
            
            # Check if biometric authentication is required (401 with persona)
            elif response.status_code == 401:
                www_auth = response.headers.get("WWW-Authenticate")
                location = response.headers.get("Location")
                
                if www_auth == "persona" and location:
                    self.log("🔴 Biometric authentication required", "INFO")
                    
                    # Handle biometric authentication
                    from urllib.parse import urljoin
                    biometric_url = urljoin(response.url, location)
                    return await self._handle_biometric_auth(biometric_url, email)
                else:
                    raise Exception("Incorrect email or password")
            else:
                raise Exception(f"Authentication failed with status code: {response.status_code}")
                    
        except asyncio.TimeoutError:
            self.log(f"❌ Authentication timed out", "ERROR")
            raise TimeoutError("Authentication request timed out")
        except requests.HTTPError as e:
            self.log(f"❌ HTTP error during authentication: {e}", "ERROR")
            raise
        except Exception as e:
            self.log(f"❌ Authentication failed: {str(e)}", "ERROR")
            raise
    
    async def _handle_biometric_auth(self, biometric_url: str, email: str) -> Dict[str, Any]:
        """Handle biometric authentication using browser automation."""
        self.log("🌐 Starting biometric authentication...", "INFO")
        
        try:
            # Import playwright for browser automation
            from playwright.async_api import async_playwright
            import time
            
            # 尝试导入browser_setup模块来获取浏览器路径
            browser_path = None
            try:
                from browser_setup import ensure_browser_available
                browser_path = ensure_browser_available()
            except ImportError:
                # 如果导入失败，尝试从当前目录导入
                try:
                    import sys
                    from pathlib import Path
                    current_dir = Path(__file__).parent
                    sys.path.insert(0, str(current_dir))
                    from browser_setup import ensure_browser_available
                    browser_path = ensure_browser_available()
                except:
                    pass
            
            async with async_playwright() as p:
                # 设置浏览器启动参数
                browser_args = ['--headless=new', '--no-sandbox', '--disable-dev-shm-usage']
                
                if browser_path and os.path.exists(browser_path):
                    self.log(f"使用自定义浏览器路径: {browser_path}", "INFO")
                    browser = await p.chromium.launch(executable_path=browser_path, args=browser_args)
                else:
                    self.log("使用默认Playwright浏览器", "INFO")
                    browser = await p.chromium.launch(headless=True, args=browser_args)
                    
                page = await browser.new_page()

                self.log("🌐 Opening browser for biometric authentication...", "INFO")
                await page.goto(biometric_url)
                self.log("Browser page loaded successfully", "SUCCESS")

                # Print instructions
                print("\n" + "="*60, file=sys.stderr)
                print("BIOMETRIC AUTHENTICATION REQUIRED", file=sys.stderr)
                print("="*60, file=sys.stderr)
                print("Browser window is open with biometric authentication page", file=sys.stderr)
                print("Complete the biometric authentication in the browser", file=sys.stderr)
                print("The system will automatically check when you're done...", file=sys.stderr)
                print("="*60, file=sys.stderr)

                # Keep checking until authentication is complete
                max_attempts = 60  # 5 minutes maximum (60 * 5 seconds)
                attempt = 0

                while attempt < max_attempts:
                    await asyncio.sleep(5)  # Check every 5 seconds
                    attempt += 1

                    # Check if authentication completed
                    check_response = await self._request('POST', biometric_url)
                    self.log(f"🔄 Checking authentication status (attempt {attempt}/{max_attempts}): {check_response.status_code}", "INFO")

                    if check_response.status_code == 201:
                        self.log("Biometric authentication successful!", "SUCCESS")

                        await browser.close()
                        
                        # Check JWT token
                        jwt_token = self.session.cookies.get('t')
                        if jwt_token:
                            self.log("JWT token received", "SUCCESS")
                        
                        # Return success response
                        return {
                            'user': {'email': email},
                            'status': 'authenticated',
                            'permissions': ['read', 'write'],
                            'message': 'Biometric authentication successful',
                            'status_code': check_response.status_code,
                            'has_jwt': jwt_token is not None
                        }
                
                await browser.close()
                raise Exception("Biometric authentication timed out")

        except Exception as e:
            self.log(f"❌ Biometric authentication failed: {str(e)}", "ERROR")
            raise
    
    async def is_authenticated(self) -> bool:
        """Check if currently authenticated using JWT token."""
        try:
            # Check if we have a JWT token in cookies
            jwt_token = self.session.cookies.get('t')
            if not jwt_token:
                self.log("❌ No JWT token found", "INFO")
                self._auth_validated_until = 0.0
                return False

            if time.time() < self._auth_validated_until:
                return True
            
            # Test authentication with a simple API call
            response = await self._request('GET', f"{self.base_url}/authentication")
            if response.status_code == 200:
                self._auth_validated_until = time.time() + self._auth_check_ttl_seconds
                return True
            elif response.status_code == 401:
                self.log("❌ JWT token expired or invalid (401)", "INFO")
                self._auth_validated_until = 0.0
                return False
            else:
                self.log(f"⚠️ Unexpected status code during auth check: {response.status_code}", "WARNING")
                self._auth_validated_until = 0.0
                return False
        except (TimeoutError, ConnectionError) as e:
            self.log(f"❌ Network error checking authentication: {str(e)}", "ERROR")
            return False
        except Exception as e:
            self.log(f"❌ Unexpected error checking authentication: {str(e)}", "ERROR")
            return False
    
    async def ensure_authenticated(self):
        """Ensure authentication is valid, re-authenticate if needed."""
        jwt_token = self.session.cookies.get('t')
        if jwt_token and time.time() < self._auth_validated_until:
            return

        async with self._auth_lock:
            # Double-check after waiting for another coroutine's auth refresh.
            jwt_token = self.session.cookies.get('t')
            if jwt_token and time.time() < self._auth_validated_until:
                return

            if jwt_token:
                try:
                    response = await self._request('GET', f"{self.base_url}/authentication")
                    if response.status_code == 200:
                        self._auth_validated_until = time.time() + self._auth_check_ttl_seconds
                        return
                    if response.status_code == 401:
                        self.log("❌ JWT token expired or invalid (401)", "INFO")
                    else:
                        self.log(f"⚠️ Unexpected status code during auth check: {response.status_code}", "WARNING")
                except (TimeoutError, ConnectionError) as e:
                    self.log(f"❌ Network error checking authentication: {str(e)}", "ERROR")

            self._auth_validated_until = 0.0
            if not self.auth_credentials:
                self.log("No credentials in memory, loading from config...", "INFO")
                config = load_config()
                creds = config.get("credentials", {})
                email = creds.get("email")
                password = creds.get("password")
                if not email or not password:
                    raise Exception("Authentication credentials not found in config. Please authenticate first.")
                self.auth_credentials = {'email': email, 'password': password}

            self.log("🔄 Re-authenticating...", "INFO")
            await self._authenticate_unlocked(self.auth_credentials['email'], self.auth_credentials['password'])
    
    async def get_self_user_id(self) -> Optional[str]:
        """The account's own user id, fetched once per process.

        Three tools each spent a whole /users/self round trip purely to learn an
        id that cannot change while the session is authenticated.
        """
        if self._self_user_id:
            return self._self_user_id
        async with self._self_user_id_lock:
            if self._self_user_id:
                return self._self_user_id
            try:
                data = await self._request_json_with_retries(
                    'GET', f"{self.base_url}/users/self", op_name='get_self_user_id')
                self._self_user_id = data.get('id')
            except Exception as e:
                self.log(f"Failed to resolve own user id: {e}", "WARNING")
                return None
            return self._self_user_id

    async def get_authentication_status(self) -> Optional[Dict[str, Any]]:
        """Get current authentication status and user info."""
        try:
            response = await self._request('GET', f"{self.base_url}/users/self")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get auth status: {str(e)}", "ERROR")
            return None
    
    async def create_simulation(self, simulation_data: SimulationData,
                                reuse_existing: bool = True) -> Dict[str, str]:
        """Create a new simulation on BRAIN platform.

        Every completed simulation is recorded in the local ledger; an identical
        request is served from it unless ``reuse_existing`` is False.
        """
        await self._create_simulation_semaphore.acquire()
        try:
            await self.ensure_authenticated()
        
            self.log("🚀 Creating simulation...", "INFO")
            
            # Prepare settings based on simulation type
            settings_dict = simulation_data.settings.model_dump()
            
            # Remove fields based on simulation type
            if simulation_data.type == "REGULAR":
                # Remove SUPER-specific fields for REGULAR
                settings_dict.pop('selectionHandling', None)
                settings_dict.pop('selectionLimit', None)
                settings_dict.pop('componentActivation', None)
            
            # Filter out None values from settings
            settings_dict = {k: v for k, v in settings_dict.items() if v is not None}
            
            # Prepare simulation payload
            payload = {
                'type': simulation_data.type,
                'settings': settings_dict
            }
            
            # Add type-specific fields
            if simulation_data.type == "REGULAR":
                if simulation_data.regular:
                    payload['regular'] = simulation_data.regular
            elif simulation_data.type == "SUPER":
                if simulation_data.combo:
                    payload['combo'] = simulation_data.combo
                if simulation_data.selection:
                    payload['selection'] = simulation_data.selection
            
            # Filter out None values from entire payload
            payload = {k: v for k, v in payload.items() if v is not None}

            # An identical request was already paid for once: the platform is
            # deterministic on the same expression, settings and data vintage, so
            # replay the recorded alpha instead of burning several minutes and a
            # simulation slot. The caller always sees that this happened.
            fingerprint = self._simulation_fingerprint(payload)
            if reuse_existing:
                prior = await self._ledger_lookup(fingerprint)
                if prior and prior.get('alpha_id'):
                    try:
                        alpha = await self.get_alpha_details(prior['alpha_id'])
                    except Exception:
                        alpha = None
                    if alpha:
                        self.log(
                            f"[ledger] Reusing alpha {prior['alpha_id']} for an identical "
                            f"simulation request (first run "
                            f"{datetime.fromtimestamp(prior.get('simulated_at') or 0):%Y-%m-%d %H:%M})",
                            "INFO",
                        )
                        return {
                            **alpha,
                            'from_local_ledger': True,
                            'previously_simulated_at': datetime.fromtimestamp(
                                prior.get('simulated_at') or 0).isoformat(),
                            'ledger_note': ('Identical expression+settings were simulated before; '
                                            'no platform simulation was run. Pass '
                                            'reuse_existing=false to force a fresh backtest '
                                            '(e.g. after a monthly data release).'),
                        }

            response = await self._request('POST', f"{self.base_url}/simulations", json=payload)
            if response.status_code >= 400:
                return {
                    "error": "Failed to create simulation",
                    "status_code": response.status_code,
                    "response": self._response_payload(response),
                    "request": {
                        "type": simulation_data.type,
                        "settings": settings_dict,
                        "has_regular": bool(simulation_data.regular),
                        "has_combo": bool(simulation_data.combo),
                        "has_selection": bool(simulation_data.selection),
                    },
                }
            
            location = response.headers.get('Location', '')
            location_url = self._to_absolute_url(location)
            simulation_id = location_url.split('/')[-1] if location_url else None
            
            self.log(f"Simulation created with ID: {simulation_id}", "SUCCESS")

            start_time = time.time()
            timeout_seconds = 1800  # 10 minutes
            max_poll_retries = 5  # Max retries for transient connection errors during polling
            poll_retry_delay = 3  # Initial delay between poll retries

            simulation_progress = None
            while True:
                # Check for timeout
                if time.time() - start_time > timeout_seconds:
                    raise TimeoutError(f"Simulation {simulation_id} timed out after {timeout_seconds} seconds")

                # Poll with retry logic for transient network errors
                poll_error = None
                for poll_attempt in range(max_poll_retries):
                    try:
                        simulation_progress = await self._request('GET', location_url)
                        poll_error = None
                        break  # Success, exit retry loop
                    except (ConnectionError, TimeoutError) as e:
                        poll_error = e
                        if poll_attempt < max_poll_retries - 1:
                            retry_wait = poll_retry_delay * (1.5 ** poll_attempt)
                            self.log(f"⚠️ Polling connection error for {simulation_id} (attempt {poll_attempt + 1}/{max_poll_retries}), retrying in {retry_wait:.1f}s: {str(e)}", "WARNING")
                            await asyncio.sleep(retry_wait)
                        else:
                            self.log(f"❌ Polling failed after {max_poll_retries} attempts for {simulation_id}: {str(e)}", "ERROR")
                
                if poll_error:
                    raise poll_error
                
                # Check if we need to wait
                retry_after = simulation_progress.headers.get("Retry-After")
                
                if not retry_after or float(retry_after) == 0:
                    break
                
                wait_time = float(retry_after)
                # Use asyncio.sleep instead of time.sleep to avoid blocking
                await asyncio.sleep(wait_time)

            self.log("Alpha done simulating, getting alpha details", "INFO")
            
            progress_data = simulation_progress.json()
            if not progress_data.get("alpha"):
                return {
                    "error": "Simulation failed or returned no alpha ID",
                    "message": self._simulation_error_message(progress_data),
                    "simulation_id": simulation_id,
                    "location": location,
                    "location_url": location_url,
                    "status": progress_data.get("status"),
                    "progress": progress_data,
                    "request": {
                        "type": simulation_data.type,
                        "settings": settings_dict,
                        "has_regular": bool(simulation_data.regular),
                        "has_combo": bool(simulation_data.combo),
                        "has_selection": bool(simulation_data.selection),
                    },
                }
                
            alpha_id = progress_data["alpha"]
            
            # Fetch alpha details with retry logic
            for alpha_attempt in range(max_poll_retries):
                try:
                    # Through get_alpha_details so the fresh record lands in the
                    # cache and the follow-up reads this workflow always makes
                    # (correlation setup, pool admission) cost nothing.
                    alpha = await self.get_alpha_details(alpha_id, force_refresh=True)
                    await self._ledger_record(fingerprint, payload, alpha)
                    await self.record_alpha_locally(alpha)
                    return alpha
                except (ConnectionError, TimeoutError) as e:
                    if alpha_attempt < max_poll_retries - 1:
                        retry_wait = poll_retry_delay * (1.5 ** alpha_attempt)
                        self.log(f"⚠️ Failed to fetch alpha details (attempt {alpha_attempt + 1}/{max_poll_retries}), retrying in {retry_wait:.1f}s: {str(e)}", "WARNING")
                        await asyncio.sleep(retry_wait)
                    else:
                        self.log(f"❌ Failed to fetch alpha details after {max_poll_retries} attempts: {str(e)}", "ERROR")
                        raise

            raise RuntimeError(f"Could not fetch alpha {alpha_id} after {max_poll_retries} attempts")
            
        except Exception as e:
            self.log(f"❌ Failed to create simulation: {str(e)}", "ERROR")
            raise
        finally:
            self._create_simulation_semaphore.release()
    
    # --- Simulation ledger -------------------------------------------------- #
    #
    # Every backtest this server runs is recorded locally. Two things fall out:
    # an identical request never has to be paid for twice (a simulation costs
    # minutes and platform quota, and the same expression on the same settings
    # produces the same alpha), and the account's own research history becomes
    # queryable without paging /users/self/alphas.

    @staticmethod
    def _simulation_fingerprint(payload: Dict[str, Any]) -> str:
        """Stable hash of a simulation request (type + settings + expression)."""
        return hashlib.sha1(
            json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
        ).hexdigest()

    def _ledger_path(self) -> Path:
        return self.store.root / 'simulations' / 'ledger.jsonl'

    async def _ledger_lookup(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        entry = await self.store.get('simulation', fingerprint)
        return entry if isinstance(entry, dict) else None

    async def _ledger_record(self, fingerprint: str, payload: Dict[str, Any],
                             alpha: Dict[str, Any]) -> None:
        """Persist one simulation, keyed by request and appended to the ledger."""
        settings = payload.get('settings') or {}
        is_block = alpha.get('is') or {}
        row = {
            'fingerprint': fingerprint,
            'alpha_id': alpha.get('id'),
            'type': payload.get('type'),
            'expression': payload.get('regular') or payload.get('combo'),
            'selection': payload.get('selection'),
            'settings': settings,
            'region': settings.get('region'),
            'universe': settings.get('universe'),
            'delay': settings.get('delay'),
            'neutralization': settings.get('neutralization'),
            'metrics': {k: is_block.get(k) for k in
                        ('sharpe', 'fitness', 'turnover', 'returns', 'margin', 'drawdown',
                         'longCount', 'shortCount')},
            'simulated_at': time.time(),
        }
        try:
            await self.store.put('simulation', fingerprint, row)
            path = self._ledger_path()

            def _append():
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, 'a', encoding='utf-8') as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + '\n')

            await asyncio.to_thread(_append)
        except Exception as e:
            self.log(f"[ledger] Failed to record simulation {alpha.get('id')}: {e}", "WARNING")

    async def read_simulation_ledger(self) -> List[Dict[str, Any]]:
        """All recorded simulations, newest first (append-only JSONL)."""
        path = self._ledger_path()

        def _read() -> List[Dict[str, Any]]:
            if not path.exists():
                return []
            rows = []
            with open(path, encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            rows.sort(key=lambda r: r.get('simulated_at') or 0, reverse=True)
            return rows

        return await asyncio.to_thread(_read)

    async def _cached_get(
        self,
        url: str,
        *,
        namespace: str,
        key: str,
        params: Optional[Dict[str, Any]] = None,
        permanent: bool = False,
        max_age: Optional[float] = None,
        redis_ttl: Optional[int] = None,
        force_refresh: bool = False,
        op_name: Optional[str] = None,
    ) -> Any:
        """GET a resource through the cache tiers, with retries and 429 handling.

        ``permanent`` routes the payload to the on-disk store (optionally with a
        ``max_age`` freshness window); otherwise it lands in Redis under
        ``redis_ttl``. Empty payloads are never stored — several BRAIN endpoints
        answer 200 with an empty body while they materialise a resource, and
        caching that would make the emptiness stick.
        """
        if not force_refresh:
            if permanent:
                envelope = await self.store.get_envelope(namespace, key)
                if envelope and envelope.get('payload'):
                    age = time.time() - (envelope.get('fetched_at') or 0)
                    if max_age is None or age <= max_age:
                        return envelope['payload']
            else:
                cached = self._get_cached_data(f"{namespace}:{key}")
                if cached:
                    return cached.get('payload') if isinstance(cached, dict) and set(cached) == {'payload'} else cached

        data = await self._request_json_with_retries(
            'GET', url, params=params, op_name=op_name or f"{namespace}({key})",
        )
        if data:
            if permanent:
                await self.store.put(namespace, key, data)
            elif redis_ttl:
                payload = data if isinstance(data, dict) else {'payload': data}
                self._set_cached_data(f"{namespace}:{key}", payload, ttl=redis_ttl)
        return data

    async def _store_get_with_params(
        self, namespace: str, key: str, key_params: Dict[str, Any]
    ) -> Optional[Any]:
        """Read a stored catalogue, backfilling its parameter metadata if missing.

        Entries migrated out of Redis carry only the hashed key, so the refresh
        tool cannot tell what to refetch for them. The parameters are known here,
        so the first read after a migration rewrites the entry with them and the
        entry becomes refreshable from then on.
        """
        envelope = await self.store.get_envelope(namespace, key)
        if not envelope:
            return None
        payload = envelope.get('payload')
        if not envelope.get('key_params') and payload:
            await self.store.put(namespace, key, payload, key_params=key_params)
        return payload

    @staticmethod
    def _is_frozen_alpha(details: Dict[str, Any]) -> bool:
        """True once an alpha is submitted: its record no longer changes."""
        if not isinstance(details, dict):
            return False
        return details.get('stage') == 'OS' or bool(details.get('dateSubmitted'))

    async def get_alpha_details(self, alpha_id: str, force_refresh: bool = False) -> Dict[str, Any]:
        """Get detailed information about an alpha.

        /alphas/{id} is metered at 2000 requests/hour and the bulk paths
        (diversity scoring, candidate-pool sync, correlation setup) re-read the
        same records repeatedly.

        A submitted (OS) alpha goes to the permanent on-disk store, but is served
        from it only while younger than ``BRAIN_ALPHA_DETAILS_MAX_AGE`` (default
        7 days): the expression, settings and IS metrics are frozen, yet the `os`
        block keeps accruing out-of-sample performance. An IS alpha is still
        editable, so it only gets a short Redis TTL that de-duplicates reads
        inside one workflow. Pass ``force_refresh`` to bypass both.
        """
        await self.ensure_authenticated()

        redis_key = f"alpha_details:{alpha_id}"
        if not force_refresh:
            envelope = await self.store.get_envelope('alpha_details', alpha_id)
            if envelope and envelope.get('payload'):
                age = time.time() - (envelope.get('fetched_at') or 0)
                if not self._alpha_details_max_age or age <= self._alpha_details_max_age:
                    return envelope['payload']
            else:
                cached = self._get_cached_data(redis_key)
                if cached:
                    return cached

        try:
            response = await self._request_json_with_retries(
                'GET',
                f"{self.base_url}/alphas/{alpha_id}",
                op_name=f"get_alpha_details({alpha_id})",
            )
            if response:
                if self._is_frozen_alpha(response):
                    await self.store.put('alpha_details', alpha_id, response)
                    # Drop any short-lived Redis copy so there is one source of truth.
                    if self.redis_client:
                        try:
                            self.redis_client.delete(redis_key)
                        except Exception:
                            pass
                elif self._alpha_details_is_ttl:
                    self._set_cached_data(redis_key, response, ttl=self._alpha_details_is_ttl)
            return response
        except Exception as e:
            self.log(f"Failed to get alpha details: {str(e)}", "ERROR")
            raise
    
    async def get_datasets(self, category: Optional[str] = None, region: str = "USA",
                          delay: int = 1, universe: str = "TOP3000", theme: str = "false",
                          search: Optional[str] = None, force_refresh: bool = False) -> Dict[str, Any]:
        """Get available datasets, stored permanently on disk after the first fetch."""
        await self.ensure_authenticated()
        
        try:
            # Generate cache key from parameters (excluding search for cache key)
            cache_params = {
                'category': category,
                'region': region,
                'delay': delay,
                'universe': universe,
                'theme': theme
            }
            cache_key = self._generate_cache_key('datasets', cache_params)

            # Permanent on-disk record; refreshed on demand via sync_platform_cache.
            cached_data = None if force_refresh else await self._store_get_with_params(
                'datasets', cache_key, cache_params)
            if cached_data:
                # Apply search filter if needed
                if search:
                    filtered_results = [
                        item for item in cached_data.get('results', [])
                        if search.lower() in json.dumps(item).lower()
                    ]
                    return {
                        **cached_data,
                        'results': filtered_results,
                        'count': len(filtered_results),
                        'from_cache': True
                    }
                return {**cached_data, 'from_cache': True}
            
            # Fetch all data from API (pagination loop)
            all_results = []
            offset = 0
            limit = 50
            total_count = None
            
            while True:
                params = {
                    'category': category,
                    'region': region,
                    'delay': delay,
                    'universe': universe,
                    'theme': theme,
                    'limit': limit,
                    'offset': offset
                }
                
                data = await self._request_json_with_retries(
                    'GET',
                    f"{self.base_url}/data-sets",
                    params=params,
                    op_name=f"get_datasets(offset={offset})",
                )
                
                results = data.get('results', [])
                all_results.extend(results)
                
                if total_count is None:
                    total_count = data.get('count', 0)
                
                # Break if we've fetched all data
                if len(results) < limit or len(all_results) >= total_count:
                    break
                
                offset += limit
            
            # Prepare complete response
            complete_data = {
                'results': all_results,
                'count': len(all_results),
                'extraNote': "if your returned result is 0, you may want to check your parameter by using get_platform_setting_options tool to got correct parameter",
                'from_cache': False
            }
            
            # Never store empty result sets: newly launched regions (e.g. GBR) can
            # transiently return count=0 from /data-sets while the platform index
            # catches up, and a permanent record of that would be sticky forever.
            if all_results:
                await self.store.put('datasets', cache_key, complete_data, key_params=cache_params)

            # Apply search filter if needed
            if search:
                filtered_results = [
                    item for item in all_results
                    if search.lower() in json.dumps(item).lower()
                ]
                complete_data['results'] = filtered_results
                complete_data['count'] = len(filtered_results)
            
            return complete_data
            
        except Exception as e:
            self.log(f"Failed to get datasets: {str(e)}", "ERROR")
            raise
    
    async def get_datafields(self, instrument_type: str = "EQUITY", region: str = "USA",
                            delay: int = 1, universe: str = "TOP3000", theme: str = "false",
                            dataset_id: Optional[str] = None, data_type: str = "",
                            search: Optional[str] = None,
                            filter_sharpe: bool = True,
                            force_refresh: bool = False) -> Dict[str, Any]:
        """Get available data fields, stored permanently on disk after the first fetch.
        
        Search supports fuzzy matching across multiple fields:
        - Searches in: name, description, dataset.name, dataset.vendor, id
        - Multiple keywords (space-separated) use AND logic
        - Case-insensitive matching
        
        OS/IS Sharpe filtering (filter_sharpe=True by default):
        - Filters out datafields whose OS/IS Sharpe ratio < 0 to improve field quality
        - Uses pre-aggregated statistics from WebDataScope info_data.bin
        - Matching is done at datafield level first, then dataset level as fallback
        
        Examples:
        - search="price" -> matches any field containing "price"
        - search="stock volume" -> matches fields containing both "stock" AND "volume"
        """
        await self.ensure_authenticated()

        # Cache key is derived from the request parameters only (``search`` and
        # ``filter_sharpe`` are applied to the cached rows), so it can be
        # computed before anything else and used to skip the lock entirely.
        cache_params = {
            'instrumentType': instrument_type,
            'region': region,
            'delay': delay,
            'universe': universe,
            'theme': theme,
            'dataset_id': dataset_id,
            'data_type': data_type,
        }
        # A dataset-scoped query is the one shape the API serves completely, so
        # it gets its own complete, permanently stored catalogue.
        if dataset_id:
            block = await self.get_dataset_fields(
                dataset_id, instrument_type, region, delay, universe, force_refresh=force_refresh)
            rows = block.get('results') or []
            if data_type and data_type != 'ALL':
                rows = [f for f in rows if f.get('type') == data_type]
            out = {'results': rows, 'count': len(rows), 'from_cache': not force_refresh,
                   'dataset_id': dataset_id, 'declared_count': block.get('declared_count'),
                   'complete': block.get('complete')}
            if search:
                ranked = await self._fts_filter(rows, search, region, universe, delay)
                out['results'] = ranked if ranked is not None else [
                    f for f in rows if _dataset_field_matches(f, search)]
                out['count'] = len(out['results'])
                out['search_mode'] = 'fts5' if ranked is not None else 'substring'
            if filter_sharpe:
                filtered, removed, applied = self._sharpe_filter_rows(out['results'], region, delay)
                out['results'] = filtered
                out['count'] = len(filtered)
                out['sharpe_filter_applied'] = applied
                out['sharpe_filter_removed'] = removed
            return out

        cache_key = self._generate_cache_key('datafields', cache_params)
        # Permanent on-disk record. A full USA/TOP3000 catalogue is 10 000 rows /
        # 5.3 MB of JSON that compresses to 226 KB (4.3%), and reading it back
        # from disk is cheaper than a Redis GET of the raw string — so it is kept
        # forever and refreshed only on demand via sync_platform_cache.
        precheck = None if force_refresh else await self._store_get_with_params(
            'datafields', cache_key, cache_params)

        # Redis-based distributed lock, taken ONLY on a store miss: /data-fields
        # is metered at 1 req/s and a full sweep is ~200 pages, so one sweep must
        # not fan out — but a hit has no business queueing behind it. The key is
        # per parameter set so unrelated regions never block each other.
        lock_key = f"lock:get_datafields:{cache_key.split(':', 1)[-1]}"
        lock_acquired = False
        lock_timeout = 900  # Lock expires to prevent deadlock
        max_wait_time = 600  # Maximum wait time for acquiring lock (10 minutes)
        wait_interval = 2  # Check every 2 seconds

        if self.redis_client and precheck is None:
            start_wait = time.time()
            while time.time() - start_wait < max_wait_time:
                try:
                    # Try to acquire lock with NX (only set if not exists) and EX (expiration)
                    lock_acquired = self.redis_client.set(lock_key, "locked", ex=lock_timeout, nx=True)
                    if lock_acquired:
                        self.log(f"Acquired Redis lock for get_datafields", "INFO")
                        break
                    else:
                        # Lock is held by another process, wait and retry
                        ttl = self.redis_client.ttl(lock_key)
                        self.log(f"Waiting for get_datafields lock (TTL: {ttl}s)...", "INFO")
                        await asyncio.sleep(wait_interval)
                except Exception as e:
                    self.log(f"Redis lock acquisition failed: {str(e)}, proceeding without lock", "WARNING")
                    break
            
            if not lock_acquired and self.redis_client:
                self.log(f"Could not acquire get_datafields lock after {max_wait_time}s, proceeding anyway", "WARNING")
        
        try:
            def fuzzy_search_filter(item: Dict[str, Any], search_term: str) -> bool:
                """Enhanced fuzzy search across key fields with multi-keyword support."""
                if not search_term:
                    return True
                
                # Split search term into keywords (space-separated) for AND logic
                keywords = [kw.strip().lower() for kw in search_term.split() if kw.strip()]
                if not keywords:
                    return True
                
                # Extract searchable fields
                searchable_text_parts = []
                
                # Add field name
                if item.get('name'):
                    searchable_text_parts.append(str(item['name']))
                
                # Add field description
                if item.get('description'):
                    searchable_text_parts.append(str(item['description']))
                
                # Add field ID
                if item.get('id'):
                    searchable_text_parts.append(str(item['id']))
                
                # Add dataset information
                dataset = item.get('dataset', {})
                if isinstance(dataset, dict):
                    if dataset.get('name'):
                        searchable_text_parts.append(str(dataset['name']))
                    if dataset.get('vendor'):
                        searchable_text_parts.append(str(dataset['vendor']))
                    if dataset.get('id'):
                        searchable_text_parts.append(str(dataset['id']))
                
                # Combine all searchable text
                combined_text = ' '.join(searchable_text_parts).lower()
                
                # Check if ALL keywords match (AND logic)
                return all(keyword in combined_text for keyword in keywords)
            
            def sharpe_filter(items: list, rgn: str, dly: int) -> tuple:
                """Filter out datafields with OS/IS sharpe < 0. Returns (filtered_items, removed_count, applied)."""
                if not self._isos_data:
                    return items, 0, False
                region_key = f"{rgn}_{dly}"
                isos_info = self._isos_data.get(region_key, {})
                isos_section = isos_info.get('isos', {})
                datafield_sharpe = isos_section.get('datafield', {})
                dataset_sharpe_map = isos_section.get('dataset', {})
                if not datafield_sharpe and not dataset_sharpe_map:
                    return items, 0, False
                filtered = []
                for item in items:
                    field_name = item.get('id', '') or item.get('name', '')
                    dataset_info = item.get('dataset', {})
                    ds_id = dataset_info.get('id', '') if isinstance(dataset_info, dict) else ''
                    df_stats = datafield_sharpe.get(field_name)
                    if df_stats is not None:
                        sr = df_stats.get('sharpe_ratio')
                        if sr is not None and sr < 0:
                            continue
                    if df_stats is None and ds_id:
                        ds_stats = dataset_sharpe_map.get(ds_id)
                        if ds_stats is not None:
                            sr = ds_stats.get('sharpe_ratio')
                            if sr is not None and sr < 0:
                                continue
                    filtered.append(item)
                return filtered, len(items) - len(filtered), True

            # Double-checked: another waiter may have filled the store while we
            # queued for the lock, which turns a 200-page sweep into zero calls.
            cached_data = precheck if precheck is not None else (
                None if force_refresh
                else await self._store_get_with_params('datafields', cache_key, cache_params)
            )
            if cached_data:
                _annotate_catalogue_completeness(cached_data)
                result = {**cached_data, 'from_cache': True}
                results = result.get('results', [])
                # Full-text first: stemming and ranking beat substring matching,
                # which matched "cutoff" for "cut" and missed "estimates lowered".
                if search:
                    ranked = await self._fts_filter(results, search, region, universe, delay)
                    results = ranked if ranked is not None else [
                        item for item in results if fuzzy_search_filter(item, search)]
                    result['search_mode'] = 'fts5' if ranked is not None else 'substring'
                # Apply OS/IS Sharpe filtering
                if filter_sharpe:
                    results, removed, applied = sharpe_filter(results, region, delay)
                    result['sharpe_filter_applied'] = applied
                    result['sharpe_filter_removed'] = removed
                result['results'] = results
                result['count'] = len(results)
                return result
            
            # Fetch all data from API (pagination loop)
            all_results = []
            offset = 0
            limit = 50
            total_count = None
            
            while True:
                params = {
                    'instrumentType': instrument_type,
                    'region': region,
                    'delay': delay,
                    'universe': universe,
                    'limit': limit,
                    'offset': offset
                }
                
                if data_type != 'ALL' and data_type:
                    params['type'] = data_type
                
                if dataset_id:
                    params['dataset.id'] = dataset_id
                
                data = await self._request_json_with_retries(
                    'GET',
                    f"{self.base_url}/data-fields",
                    params=params,
                    op_name=f"get_datafields(offset={offset})",
                )
                
                results = data.get('results', [])
                # Pacing is handled by self.rate_limiter, which learns the real
                # quota (currently 1/s + 30/min on this endpoint) from response
                # headers instead of guessing with a fixed sleep.
                all_results.extend(results)
                
                if total_count is None:
                    total_count = data.get('count', 0)
                
                # Break if we've fetched all data
                if len(results) < limit or len(all_results) >= total_count:
                    break
                
                offset += limit
            
            # Prepare complete response
            complete_data = {
                'results': all_results,
                'count': len(all_results),
                'extraNote': "if your returned result is 0, you may want to check your parameter by using get_platform_setting_options tool to got correct parameter. Search supports fuzzy matching with multiple keywords (space-separated, AND logic).",
                'from_cache': False
            }
            # The unfiltered window is hard-capped: `count` saturates at 10000 and
            # offset=10000 is rejected with "Invalid offset. Please use filters to
            # narrow down the result." For USA/TOP3000 the datasets declare 91076
            # fields, so a sweep reaches 11% of them and 267 datasets come back
            # empty -- with nothing in the payload to say so. Say so.
            _annotate_catalogue_completeness(complete_data)
            
            # Never store empty result sets: newly launched regions can transiently
            # return count=0 while the platform index catches up, and a permanent
            # record of that emptiness would be far worse than a TTL'd one.
            if all_results:
                await self.store.put('datafields', cache_key, complete_data, key_params=cache_params)

            if search:
                ranked = await self._fts_filter(all_results, search, region, universe, delay)
                filtered_results = ranked if ranked is not None else [
                    item for item in all_results if fuzzy_search_filter(item, search)]
                complete_data['results'] = filtered_results
                complete_data['count'] = len(filtered_results)
                complete_data['search_mode'] = 'fts5' if ranked is not None else 'substring'
            
            # Apply OS/IS Sharpe ratio filtering
            if filter_sharpe:
                results, removed, applied = sharpe_filter(complete_data['results'], region, delay)
                complete_data['results'] = results
                complete_data['count'] = len(results)
                complete_data['sharpe_filter_applied'] = applied
                complete_data['sharpe_filter_removed'] = removed
                if applied:
                    self.log(f"Sharpe filter ({region}_{delay}): removed {removed}/{removed + len(results)} fields with OS/IS sharpe < 0", "INFO")
            
            return complete_data
            
        except Exception as e:
            self.log(f"Failed to get datafields: {str(e)}", "ERROR")
            raise
        finally:
            # Release Redis lock if acquired
            if lock_acquired and self.redis_client:
                try:
                    self.redis_client.delete(lock_key)
                    self.log(f"Released Redis lock for get_datafields", "INFO")
                except Exception as e:
                    self.log(f"Failed to release Redis lock: {str(e)}", "WARNING")
    
    def _sharpe_filter_rows(self, items: list, region: str, delay: int) -> tuple:
        """Drop datafields whose OS/IS Sharpe is negative. Returns
        (kept, removed_count, applied)."""
        if not self._isos_data:
            return items, 0, False
        isos = (self._isos_data.get(f"{region}_{delay}") or {}).get('isos', {})
        field_sharpe = isos.get('datafield', {})
        dataset_sharpe = isos.get('dataset', {})
        if not field_sharpe and not dataset_sharpe:
            return items, 0, False
        kept = []
        for item in items:
            name = item.get('id', '') or item.get('name', '')
            ds = item.get('dataset', {})
            ds_id = ds.get('id', '') if isinstance(ds, dict) else ''
            stats = field_sharpe.get(name)
            if stats is not None:
                sr = stats.get('sharpe_ratio')
                if sr is not None and sr < 0:
                    continue
            if stats is None and ds_id:
                ds_stats = dataset_sharpe.get(ds_id)
                if ds_stats is not None:
                    sr = ds_stats.get('sharpe_ratio')
                    if sr is not None and sr < 0:
                        continue
            kept.append(item)
        return kept, len(items) - len(kept), True

    def _df_config_key(self, instrument_type: str, region: str, delay: Union[int, str],
                       universe: str) -> str:
        return f"{instrument_type}:{region}:{universe}:{delay}"

    async def get_dataset_fields(
        self,
        dataset_id: str,
        instrument_type: str = "EQUITY",
        region: str = "USA",
        delay: int = 1,
        universe: str = "TOP3000",
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """Every datafield of ONE dataset, paged to completion and stored forever.

        This is the only way to see the whole catalogue. A global /data-fields
        sweep is hard-capped: `count` saturates at 10000 and `offset=10000` is
        rejected with ["Invalid offset. Please use filters to narrow down the
        result."]. For USA/TOP3000 the datasets declare 91076 fields in total, so
        an unfiltered sweep reaches 11% of them and 267 datasets come back with
        nothing at all — silently. Filtering by dataset.id lifts the cap
        (dataset.id=pv87 returns its full 6666), which makes the dataset the
        right unit to fetch and cache.
        """
        await self.ensure_authenticated()
        cfg = self._df_config_key(instrument_type, region, delay, universe)
        key = f"{cfg}:{dataset_id}"

        if not force_refresh:
            stored = await self.store.get('datafields_ds', key)
            if stored:
                return stored

        all_results: List[Dict[str, Any]] = []
        offset, page_size, total = 0, 50, None
        while True:
            data = await self._request_json_with_retries(
                'GET', f"{self.base_url}/data-fields",
                params={'instrumentType': instrument_type, 'region': region, 'delay': delay,
                        'universe': universe, 'dataset.id': dataset_id,
                        'limit': page_size, 'offset': offset},
                op_name=f"get_dataset_fields({dataset_id}@{offset})",
            )
            results = data.get('results') or []
            if total is None:
                total = data.get('count')
            all_results.extend(results)
            if len(results) < page_size or (total and len(all_results) >= total):
                break
            offset += page_size
            if offset >= _DATAFIELDS_WINDOW_CAP:
                # Same cap applies within a dataset; nothing more is reachable.
                self.log(f"[catalogue] {dataset_id} exceeds the 10000-row window at "
                         f"{len(all_results)} fields", "WARNING")
                break

        payload = {
            'dataset_id': dataset_id,
            'results': all_results,
            'count': len(all_results),
            'declared_count': total,
            'complete': total is None or len(all_results) >= total,
        }
        # Store a definitive empty answer too (the server said count=0), otherwise
        # every resume re-asks about datasets that have no fields in this
        # configuration. A blank body with no count is NOT definitive and is left
        # unstored so it gets retried.
        if all_results or total == 0:
            await self.store.put('datafields_ds', key, payload,
                                 key_params={'instrumentType': instrument_type, 'region': region,
                                             'delay': delay, 'universe': universe,
                                             'dataset_id': dataset_id})
        return payload

    async def build_full_datafield_catalogue(
        self,
        instrument_type: str = "EQUITY",
        region: str = "USA",
        delay: int = 1,
        universe: str = "TOP3000",
        force_refresh: bool = False,
        progress: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Assemble the COMPLETE datafield catalogue, dataset by dataset.

        Resumable: datasets already stored are skipped unless force_refresh.
        """
        datasets = await self.get_datasets(None, region, delay, universe, 'false', None)
        drows = [x for x in (datasets.get('results') or []) if isinstance(x, dict)]
        cfg = self._df_config_key(instrument_type, region, delay, universe)

        merged: List[Dict[str, Any]] = []
        seen: set = set()
        per_dataset: Dict[str, Any] = {}
        incomplete: List[str] = []
        fetched_rows = 0
        shared_ids: List[str] = []

        for i, ds in enumerate(drows, 1):
            dsid = ds.get('id')
            if not dsid:
                continue
            try:
                block = await self.get_dataset_fields(
                    dsid, instrument_type, region, delay, universe, force_refresh=force_refresh)
            except Exception as e:
                self.log(f"[catalogue] {dsid} failed: {e}", "WARNING")
                per_dataset[dsid] = {'error': str(e)}
                continue
            rows = block.get('results') or []
            fetched_rows += len(rows)
            for f in rows:
                fid = f.get('id')
                if not fid:
                    continue
                if fid in seen:
                    shared_ids.append(fid)
                    continue
                seen.add(fid)
                merged.append(f)
            per_dataset[dsid] = {'fields': len(rows), 'declared': ds.get('fieldCount'),
                                 'complete': block.get('complete')}
            if (ds.get('fieldCount') or 0) > len(rows):
                incomplete.append(dsid)
            if progress and i % 10 == 0:
                self.log(f"[catalogue] {i}/{len(drows)} datasets, {len(merged)} fields", "INFO")

        declared_total = sum(x.get('fieldCount') or 0 for x in drows)
        complete_payload = {
            'results': merged,
            'count': len(merged),
            'fetched_rows': fetched_rows,
            'declared_total': declared_total,
            'coverage': round(fetched_rows / declared_total, 4) if declared_total else None,
            'datasets': len(drows),
            'incomplete_datasets': incomplete,
            'fields_in_two_datasets': sorted(set(shared_ids)),
            'built_via': 'per-dataset sweep (bypasses the 10000-row global cap)',
            'from_cache': False,
        }
        # Write it to the same key the normal reads use, so every existing caller
        # transparently gets the complete catalogue from here on.
        cache_params = {'instrumentType': instrument_type, 'region': region, 'delay': delay,
                        'universe': universe, 'theme': 'false', 'dataset_id': None, 'data_type': ''}
        await self.store.put('datafields', self._generate_cache_key('datafields', cache_params),
                             complete_payload, key_params=cache_params)
        return complete_payload

    # --- Alpha corpus sync (background) ------------------------------------- #

    ALPHA_SYNC_CURSOR = 'alpha_sync_cursor'

    def _alpha_sync_snapshot(self) -> Dict[str, Any]:
        snap = dict(self._alpha_sync_state)
        task = self._alpha_sync_task
        snap['running'] = bool(task and not task.done())
        # Progress lives in the process; the corpus and cursor live on disk. After
        # a restart there is no in-flight sync, which is 'idle' — not an absent
        # status that reads like something went wrong.
        snap.setdefault('status', 'idle')
        started = snap.get('started_at')
        if started:
            elapsed = time.time() - started
            snap['elapsed_seconds'] = round(elapsed, 1)
            fetched = snap.get('fetched') or 0
            if fetched and elapsed > 0:
                snap['alphas_per_minute'] = round(fetched / elapsed * 60, 1)
        return snap

    async def start_alpha_sync(self, since: str = '2026-01-01',
                               restart: bool = False) -> Dict[str, Any]:
        """Begin mirroring the account's alphas into the local corpus."""
        if self._alpha_sync_task and not self._alpha_sync_task.done():
            return {'started': False, 'reason': 'already running', **self._alpha_sync_snapshot()}
        await self.alpha_store.ensure_ready()
        if restart:
            await self.alpha_store.set_state(self.ALPHA_SYNC_CURSOR, '')
        self._alpha_sync_state = {
            'started_at': time.time(), 'since': since, 'fetched': 0, 'stored': 0,
            'pages': 0, 'cursor': None, 'status': 'starting',
        }
        self._alpha_sync_task = asyncio.create_task(self._run_alpha_sync(since))
        return {'started': True, **self._alpha_sync_snapshot()}

    async def _run_alpha_sync(self, since: str) -> None:
        """Cursor-paginate the whole alpha history into SQLite.

        offset cannot be used: the platform rejects offset>=1000 outright with
        "Cannot display more than the first 1,000 alphas. Apply filters to narrow
        results and see more." So the sweep walks forward on dateCreated instead,
        taking up to 1000 rows per cursor position.

        dateCreated has one-second resolution and up to 8 alphas can share a
        second, so each new cursor is set one second BEFORE the batch maximum and
        the overlap is absorbed by the primary key. Without that rewind the rows
        sharing the boundary second would be skipped silently.
        """
        st = self._alpha_sync_state
        store = self.alpha_store
        try:
            st['status'] = 'running'
            cursor = await store.get_state(self.ALPHA_SYNC_CURSOR)
            if not cursor:
                cursor = AlphaStore._utc(self._normalize_brain_datetime(since)) \
                    or self._normalize_brain_datetime(since)
            st['cursor'] = cursor

            page_size = 100
            max_offset = 1000  # platform hard limit for this endpoint
            stall_guard = 0

            async def page(off: int) -> List[Dict[str, Any]]:
                data = await self._request_json_with_retries(
                    'GET', f"{self.base_url}/users/self/alphas",
                    params={'dateCreated>': cursor, 'order': 'dateCreated',
                            'limit': page_size, 'offset': off},
                    op_name=f"alpha_sync(cursor={cursor[:19]},offset={off})",
                )
                st['pages'] += 1
                return data.get('results') or []

            while True:
                # Each page costs ~5s of server time but the family allows 27
                # requests/minute, so fetching one page at a time leaves most of
                # the budget unused (measured: 9 req/min sequential). The offsets
                # inside one cursor window are independent, so they go out
                # together and the rate limiter — not latency — sets the pace.
                first = await page(0)
                batch: List[Dict[str, Any]] = list(first)
                if len(first) == page_size:
                    rest = await asyncio.gather(
                        *[page(off) for off in range(page_size, max_offset, page_size)])
                    for rows in rest:
                        batch.extend(rows)

                if not batch:
                    st['status'] = 'complete'
                    break

                stored = await store.upsert_many(batch)
                st['fetched'] += len(batch)
                st['stored'] += stored

                # Compare in UTC: raw values carry local offsets and text order
                # would not match chronological order.
                stamps = [AlphaStore._utc(a.get('dateCreated')) for a in batch]
                stamps = [s for s in stamps if s]
                if not stamps:
                    st['status'] = 'error'
                    st['error'] = 'batch had no dateCreated values'
                    break
                newest = max(stamps)
                next_cursor = self._rewind_one_second(newest)
                if next_cursor <= cursor:
                    # Guard against a cursor that cannot advance (would spin).
                    stall_guard += 1
                    if stall_guard > 3:
                        st['status'] = 'error'
                        st['error'] = f'cursor stalled at {cursor}'
                        break
                    next_cursor = self._advance_one_second(cursor)
                else:
                    stall_guard = 0

                cursor = next_cursor
                st['cursor'] = cursor
                await store.set_state(self.ALPHA_SYNC_CURSOR, cursor)

                if st['fetched'] % 5000 < len(batch):
                    self.log(f"[alpha-sync] {st['fetched']} alphas, cursor {cursor[:19]}", "INFO")

                if len(batch) < max_offset:
                    # The window was not saturated, so we have caught up to now.
                    st['status'] = 'complete'
                    break

            if st['status'] == 'complete':
                self.log("[alpha-sync] Rebuilding expression index...", "INFO")
                await store.rebuild_fts()
                st['status'] = 'indexing'
                try:
                    operators = await self.get_operators()
                    op_names = [o.get('name') or o.get('id') for o in (operators or [])
                                if isinstance(o, dict)]
                    field_names = await self._known_field_names()
                    st['token_index'] = await store.build_token_index(op_names, field_names)
                except Exception as e:
                    self.log(f"[alpha-sync] Token index failed: {e}", "WARNING")
                    st['token_index'] = {'error': str(e)}
                st['status'] = 'complete'
                st['stats'] = await store.stats()
                self.log(f"[alpha-sync] Done: {st['fetched']} alphas", "INFO")
        except asyncio.CancelledError:
            st['status'] = 'stopped'
            self.log("[alpha-sync] Stopped; cursor is saved and will resume.", "INFO")
            raise
        except Exception as e:
            st['status'] = 'error'
            st['error'] = str(e)
            self.log(f"[alpha-sync] Failed: {e}", "ERROR")

    async def _token_vocabulary(self) -> Dict[str, set]:
        """Cached {operators, fields} name sets for expression tokenisation."""
        if self._token_vocab and time.time() - self._token_vocab_at < self._token_vocab_ttl:
            return self._token_vocab
        try:
            ops = await self.get_operators()
            op_set = {(o.get('name') or o.get('id') or '').lower()
                      for o in (ops or []) if isinstance(o, dict)}
            op_set.discard('')
            field_set = {n.lower() for n in await self._known_field_names()}
        except Exception as e:
            self.log(f"[corpus] Could not load token vocabulary: {e}", "WARNING")
            return self._token_vocab or {'operators': set(), 'fields': set()}
        self._token_vocab = {'operators': op_set, 'fields': field_set}
        self._token_vocab_at = time.time()
        return self._token_vocab

    async def record_alpha_locally(self, alpha: Dict[str, Any]) -> None:
        """Put a freshly produced alpha into the searchable corpus immediately.

        A backtest run through this server already holds the full record, so
        writing it locally costs no platform request — and without it the alpha
        stays invisible to analyze_my_research until the next sync, which is
        exactly when you most want to ask "have I tried this".
        """
        if not isinstance(alpha, dict) or not alpha.get('id'):
            return
        try:
            vocab = await self._token_vocabulary()
            await self.alpha_store.upsert_many(
                [alpha], index=True,
                op_set=vocab['operators'], field_set=vocab['fields'])
        except Exception as e:
            self.log(f"[corpus] Failed to record alpha {alpha.get('id')}: {e}", "WARNING")

    async def _fts_filter(self, rows: List[Dict[str, Any]], search: str,
                          region: Optional[str], universe: Optional[str],
                          delay: Optional[Any]) -> Optional[List[Dict[str, Any]]]:
        """Order ``rows`` by full-text relevance. None when the index cannot serve it."""
        try:
            # No configuration filter here: the caller's rows already belong to
            # the right configuration, so this only needs relevance ordering.
            hits = await self.alpha_store.search_datafields(search, limit=20000)
        except Exception as e:
            self.log(f"[field-index] FTS search failed ({e}); using substring match", "WARNING")
            return None
        if hits is None:
            return None
        order = {h['id']: i for i, h in enumerate(hits)}
        # Keep the caller's own rows (they carry this configuration's metrics)
        # but in relevance order.
        matched = [r for r in rows if r.get('id') in order]
        matched.sort(key=lambda r: order[r['id']])
        return matched

    async def build_datafield_search_index(self) -> Dict[str, Any]:
        """Index every stored datafield description for full-text search.

        Reads only the local catalogues — zero platform requests. Worth doing
        because the previous search was a Python substring scan over the cached
        rows: it took ~550 ms, matched "cutoff" for "cut", and missed the 69
        fields describing a dividend cut as "DPS estimates lowered". FTS5 with
        porter stemming builds in well under a second and answers in ~1 ms.
        """
        fields: Dict[str, tuple] = {}
        configs: List[tuple] = []
        try:
            # The CONFIG-level catalogues, not the per-dataset ones: a
            # configuration built by reuse (dedup mode) has no per-dataset
            # entries at all, so indexing from those silently skipped it.
            entries = await self.store.list_entries('datafields')
        except Exception as e:
            return {'error': f'could not list catalogues: {e}'}

        for entry in entries:
            kp = entry.get('key_params') or {}
            if kp.get('dataset_id') or (kp.get('data_type') or ''):
                continue  # subsets, not whole-configuration catalogues
            payload = await self.store.get('datafields', entry.get('key'))
            region, universe, delay = kp.get('region'), kp.get('universe'), kp.get('delay')
            for f in ((payload or {}).get('results') or []):
                fid = f.get('id')
                if not fid:
                    continue
                if fid not in fields:
                    ds = f.get('dataset')
                    cat = f.get('category')
                    fields[fid] = (
                        fid, (f.get('description') or '').strip(), f.get('type'),
                        ds.get('id') if isinstance(ds, dict) else ds,
                        cat.get('id') if isinstance(cat, dict) else cat,
                        f.get('dateCreated'),
                    )
                if region:
                    configs.append((fid, region, universe, delay, f.get('coverage'),
                                    f.get('userCount'), f.get('alphaCount')))

        if not fields:
            return {'error': 'No stored datafield catalogues to index.',
                    'hint': 'Run build_datafield_catalogue(action="start") first.'}
        result = await self.alpha_store.index_datafields(list(fields.values()), configs)
        self.log(f"[field-index] Indexed {result['fields']} fields, "
                 f"{result['config_rows']} config rows", "INFO")
        return result

    async def _known_field_names(self) -> List[str]:
        """Every datafield id in the local catalogues, across all configurations.

        Used to classify expression tokens. Reading the stored catalogues costs
        nothing; there is no need to ask the platform what its fields are called.
        """
        names: set = set()
        try:
            for entry in await self.store.list_entries('datafields_ds'):
                payload = await self.store.get('datafields_ds', entry.get('key'))
                for f in ((payload or {}).get('results') or []):
                    fid = f.get('id')
                    if fid:
                        names.add(fid)
            if not names:
                for entry in await self.store.list_entries('datafields'):
                    payload = await self.store.get('datafields', entry.get('key'))
                    for f in ((payload or {}).get('results') or []):
                        fid = f.get('id')
                        if fid:
                            names.add(fid)
        except Exception as e:
            self.log(f"[alpha-sync] Could not read field catalogues: {e}", "WARNING")
        return sorted(names)

    @staticmethod
    def _rewind_one_second(value: str) -> str:
        return BrainApiClient._shift_seconds(value, -1)

    @staticmethod
    def _advance_one_second(value: str) -> str:
        return BrainApiClient._shift_seconds(value, +1)

    @staticmethod
    def _shift_seconds(value: str, delta: int) -> str:
        """Shift an ISO 8601 timestamp and return it normalised to UTC.

        Normalising is not cosmetic. The cursor is compared as a string to decide
        whether the sweep advanced, and the platform answers with local offsets
        ("2026-08-14T22:49:42-04:00") while the cursor starts as UTC
        ("2026-08-15T00:00:00Z"). Comparing those as text says 08-14 < 08-15 and
        the sweep concludes it went backwards — which silently degraded it into
        advancing one second per batch, i.e. 86400 batches for a single day.
        """
        try:
            dt = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        shifted = (dt + timedelta(seconds=delta)).astimezone(timezone.utc)
        return shifted.isoformat().replace('+00:00', 'Z')

    async def stop_alpha_sync(self) -> Dict[str, Any]:
        task = self._alpha_sync_task
        if not task or task.done():
            return {'stopped': False, 'reason': 'not running', **self._alpha_sync_snapshot()}
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return {'stopped': True, **self._alpha_sync_snapshot()}

    # --- Full catalogue build (background) ---------------------------------- #

    @staticmethod
    def _configs_in_use() -> List[Dict[str, Any]]:
        """Market configurations this account actually researches.

        Derived from the OS PnL pool filenames, which are written per
        instrument/region/universe/delay as self-correlation runs — a far better
        signal of what matters than the full cross product of platform options.
        """
        configs = []
        seen = set()
        for path in sorted(Path(__file__).parent.joinpath('downloads').glob('os_pnl_pool_*.pkl')):
            stem = path.stem  # os_pnl_pool_equity_usa_top3000_delay1
            parts = stem.split('_')
            if len(parts) < 6 or parts[:3] != ['os', 'pnl', 'pool']:
                continue
            if not parts[-1].startswith('delay'):
                continue
            try:
                delay = int(parts[-1][len('delay'):])
            except ValueError:
                continue
            instrument = parts[3].upper()
            region = parts[4].upper()
            universe = '_'.join(parts[5:-1]).upper()
            if not universe:
                continue
            key = (instrument, region, universe, delay)
            if key in seen:
                continue
            seen.add(key)
            configs.append({'instrumentType': instrument, 'region': region,
                            'universe': universe, 'delay': delay})
        return configs

    def _catalogue_snapshot(self) -> Dict[str, Any]:
        snap = dict(self._catalogue_state)
        task = self._catalogue_task
        snap['running'] = bool(task and not task.done())
        started = snap.get('started_at')
        if started:
            snap['elapsed_seconds'] = round(time.time() - started, 1)
        done = snap.get('fields_done') or 0
        declared = snap.get('fields_declared') or 0
        if declared:
            snap['coverage'] = round(done / declared, 4)
            rate = done / max(1.0, snap.get('elapsed_seconds') or 1.0)
            if rate > 0:
                snap['eta_minutes'] = round((declared - done) / rate / 60.0, 1)
        return snap

    async def start_catalogue_build(self, configs: Optional[List[Dict[str, Any]]] = None,
                                    mode: str = 'dedup') -> Dict[str, Any]:
        """Kick off the complete per-dataset catalogue build in the background."""
        if self._catalogue_task and not self._catalogue_task.done():
            return {'started': False, 'reason': 'already running', **self._catalogue_snapshot()}

        targets = configs or self._configs_in_use()
        if not targets:
            return {'started': False, 'reason': 'no market configurations found'}

        self._catalogue_state = {
            'started_at': time.time(),
            'configs_total': len(targets),
            'configs_done': 0,
            'current': None,
            'fields_done': 0,
            'fields_declared': 0,
            'datasets_done': 0,
            'datasets_total': 0,
            'per_config': {},
            'status': 'starting',
        }
        self._catalogue_state['mode'] = mode
        self._catalogue_task = asyncio.create_task(self._run_catalogue_build(targets, mode))
        return {'started': True, 'mode': mode, 'configs': targets, **self._catalogue_snapshot()}

    async def _run_catalogue_build(self, targets: List[Dict[str, Any]], mode: str = 'dedup') -> None:
        st = self._catalogue_state
        # Which field ids exist is a property of (region, delay) -- verified live:
        # option4 returns the same 1298 ids for USA/TOP500 and USA/TOP3000, and
        # every USA universe declares the same 345 datasets / 91076 fields. Only
        # the per-field usage metrics (userCount, alphaCount, coverage) differ.
        # In 'dedup' mode a configuration whose dataset list is identical to one
        # already built reuses that catalogue instead of re-downloading it, which
        # halves the work; the payload records which universe the metrics came from.
        built_by_signature: Dict[Any, Dict[str, Any]] = {}
        try:
            st['status'] = 'running'
            for cfg in targets:
                label = f"{cfg['region']}/{cfg['universe']}/delay{cfg['delay']}"
                st['current'] = label
                try:
                    datasets = await self.get_datasets(
                        None, cfg['region'], cfg['delay'], cfg['universe'], 'false', None)
                except Exception as e:
                    st['per_config'][label] = {'error': f'dataset list failed: {e}'}
                    st['configs_done'] += 1
                    continue
                drows = [x for x in (datasets.get('results') or []) if isinstance(x, dict) and x.get('id')]
                declared = sum(x.get('fieldCount') or 0 for x in drows)

                if not drows:
                    # The platform serves no datasets for this configuration.
                    st['per_config'][label] = {'datasets': 0, 'declared': 0, 'fields': 0,
                                               'status': 'empty',
                                               'note': 'platform returns no datasets for this configuration'}
                    st['configs_done'] += 1
                    continue

                signature = (cfg['instrumentType'], cfg['region'], cfg['delay'],
                             len(drows), declared, tuple(sorted(x['id'] for x in drows)))
                prior = built_by_signature.get(signature)
                if mode == 'dedup' and prior is not None:
                    payload = dict(prior['payload'])
                    payload['metrics_from_universe'] = prior['universe']
                    payload['note'] = (
                        f"Field set is identical to {cfg['region']}/{prior['universe']}; reused to "
                        "avoid re-downloading it. The per-field userCount / alphaCount / coverage "
                        f"in this payload are {prior['universe']}'s. Rebuild this configuration on "
                        "its own if you need its real usage metrics."
                    )
                    cache_params = {'instrumentType': cfg['instrumentType'], 'region': cfg['region'],
                                    'delay': cfg['delay'], 'universe': cfg['universe'],
                                    'theme': 'false', 'dataset_id': None, 'data_type': ''}
                    await self.store.put('datafields',
                                         self._generate_cache_key('datafields', cache_params),
                                         payload, key_params=cache_params)
                    st['per_config'][label] = {
                        'datasets': len(drows), 'declared': declared,
                        'fields': payload.get('count'), 'status': 'reused',
                        'reused_from': f"{cfg['region']}/{prior['universe']}",
                        'coverage': payload.get('coverage'),
                    }
                    st['configs_done'] += 1
                    self.log(f"[catalogue] {label} reused from {prior['universe']} "
                             f"({payload.get('count')} fields, no download)", "INFO")
                    continue

                st['fields_declared'] += declared
                st['datasets_total'] += len(drows)
                st['per_config'][label] = {'datasets': len(drows), 'declared': declared,
                                           'fields': 0, 'status': 'running'}

                merged: List[Dict[str, Any]] = []
                seen: set = set()
                incomplete: List[str] = []
                fetched_rows = 0
                shared_ids: List[str] = []
                for ds in drows:
                    dsid = ds['id']
                    try:
                        block = await self.get_dataset_fields(
                            dsid, cfg['instrumentType'], cfg['region'], cfg['delay'], cfg['universe'])
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self.log(f"[catalogue] {label} {dsid} failed: {e}", "WARNING")
                        incomplete.append(dsid)
                        st['datasets_done'] += 1
                        continue
                    rows = block.get('results') or []
                    fetched_rows += len(rows)
                    for f in rows:
                        fid = f.get('id')
                        if not fid:
                            continue
                        if fid in seen:
                            # The same field id can be published under two
                            # datasets; that is not a duplicate download.
                            shared_ids.append(fid)
                            continue
                        seen.add(fid)
                        merged.append(f)
                    if (ds.get('fieldCount') or 0) > len(rows):
                        incomplete.append(dsid)
                    st['datasets_done'] += 1
                    st['fields_done'] += len(rows)
                    st['per_config'][label]['fields'] = len(merged)

                payload = {
                    'results': merged,
                    'count': len(merged),
                    'fetched_rows': fetched_rows,
                    'declared_total': declared,
                    'coverage': round(fetched_rows / declared, 4) if declared else None,
                    'datasets': len(drows),
                    'incomplete_datasets': incomplete,
                    'fields_in_two_datasets': sorted(set(shared_ids)),
                    'built_via': 'per-dataset sweep (bypasses the 10000-row global cap)',
                    'from_cache': False,
                }
                cache_params = {'instrumentType': cfg['instrumentType'], 'region': cfg['region'],
                                'delay': cfg['delay'], 'universe': cfg['universe'],
                                'theme': 'false', 'dataset_id': None, 'data_type': ''}
                await self.store.put('datafields',
                                     self._generate_cache_key('datafields', cache_params),
                                     payload, key_params=cache_params)
                built_by_signature[signature] = {'universe': cfg['universe'], 'payload': payload}
                st['per_config'][label].update(
                    {'status': 'done', 'fields': len(merged),
                     'coverage': payload['coverage'], 'incomplete': len(incomplete)})
                st['configs_done'] += 1
                self.log(f"[catalogue] {label} complete: {len(merged)}/{declared} fields", "INFO")
            st['status'] = 'complete'
            st['current'] = None
        except asyncio.CancelledError:
            st['status'] = 'stopped'
            self.log("[catalogue] Build stopped; progress is on disk and will resume.", "INFO")
            raise
        except Exception as e:
            st['status'] = 'error'
            st['error'] = str(e)
            self.log(f"[catalogue] Build failed: {e}", "ERROR")

    async def stop_catalogue_build(self) -> Dict[str, Any]:
        task = self._catalogue_task
        if not task or task.done():
            return {'stopped': False, 'reason': 'not running', **self._catalogue_snapshot()}
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return {'stopped': True, **self._catalogue_snapshot()}

    async def get_alpha_pnl(self, alpha_id: str, force_refresh: bool = False) -> Dict[str, Any]:
        """Get PnL data for an alpha with retry logic.

        A given alpha id's PnL never changes (a re-simulation produces a new id),
        so the payload is stored permanently on disk. Mutual-correlation and
        self-correlation runs re-request the same series for every candidate
        comparison, which is the single largest repeated download against the
        platform. ~100 KB of JSON compresses to ~30 KB per alpha.
        """
        await self.ensure_authenticated()

        if not force_refresh:
            stored = await self.store.get('alpha_pnl', alpha_id)
            if stored:
                return stored

        max_retries = 5
        retry_delay = 2  # seconds

        for attempt in range(max_retries):
            try:
                self.log(f"Attempting to get PnL for alpha {alpha_id} (attempt {attempt + 1}/{max_retries})", "INFO")
                
                response = await self._request('GET', f"{self.base_url}/alphas/{alpha_id}/recordsets/pnl")
                if response.status_code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                    wait = self._retry_wait_seconds(response, attempt)
                    self.log(f"PnL HTTP {response.status_code} for {alpha_id}, retrying in {wait:.1f}s", "WARNING")
                    await asyncio.sleep(wait)
                    continue
                response.raise_for_status()
                
                text = (response.text or "").strip()
                if not text:
                    if attempt < max_retries - 1:
                        # BRAIN answers 200 + EMPTY BODY while it materialises the
                        # recordset and states the wait in Retry-After (observed:
                        # 1.0s). Honour it instead of a blind 2s that grows 1.5x —
                        # every uncached PnL pays this wait exactly once.
                        wait = self._recordset_retry_after(response, retry_delay)
                        self.log(f"Empty PnL response for {alpha_id}, retrying in {wait:.1f}s...", "WARNING")
                        await asyncio.sleep(wait)
                        retry_delay *= 1.5
                        continue
                    else:
                        self.log(f"Empty PnL response after {max_retries} attempts for {alpha_id}", "WARNING")
                        return {}
                
                try:
                    pnl_data = response.json()
                    if pnl_data:
                        self.log(f"Successfully retrieved PnL data for alpha {alpha_id}", "SUCCESS")
                        if isinstance(pnl_data, dict):
                            await self.store.put('alpha_pnl', alpha_id, pnl_data)
                        return pnl_data
                    else:
                        if attempt < max_retries - 1:
                            self.log(f"Empty PnL JSON for {alpha_id}, retrying in {retry_delay} seconds...", "WARNING")
                            await asyncio.sleep(retry_delay)
                            retry_delay *= 1.5
                            continue
                        else:
                            self.log(f"Empty PnL JSON after {max_retries} attempts for {alpha_id}", "WARNING")
                            return {}
                            
                except json.JSONDecodeError as parse_err:
                    if attempt < max_retries - 1:
                        self.log(f"PnL JSON parse failed for {alpha_id} (attempt {attempt + 1}), retrying in {retry_delay} seconds...", "WARNING")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 1.5
                        continue
                    else:
                        self.log(f"PnL JSON parse failed for {alpha_id} after {max_retries} attempts: {parse_err}", "WARNING")
                        return {}
                        
            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    self.log(f"Failed to get alpha PnL for {alpha_id} (attempt {attempt + 1}), retrying in {retry_delay} seconds: {str(e)}", "WARNING")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 1.5
                    continue
                else:
                    self.log(f"Failed to get alpha PnL for {alpha_id} after {max_retries} attempts: {str(e)}", "ERROR")
                    raise
        
        return {}
    
    @staticmethod
    def _normalize_brain_datetime(value: str) -> str:
        """BRAIN date filters require ISO 8601 datetimes WITH timezone.

        The API returns 400 ['Expected ISO 8601 datetime with timezone'] for
        naive values, so date-only strings get 'T00:00:00' appended and
        timezone-less datetimes get 'Z' appended.
        """
        v = (value or '').strip()
        if not v:
            return v
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            v += "T00:00:00"
        if re.search(r"(Z|[+-]\d{2}:?\d{2})$", v):
            return v
        return v + "Z"

    async def get_user_alphas(
        self,
        stage: str = "OS",
        limit: int = 30,
        offset: int = 0,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        submission_start_date: Optional[str] = None,
        submission_end_date: Optional[str] = None,
        order: Optional[str] = None,
        hidden: Optional[bool] = None,
        region: Optional[str] = None,
        status: Optional[str] = None,
        alpha_type: Optional[str] = None,
        is_super: Optional[bool] = None,
        color: Optional[str] = None,
        name: Optional[str] = None,
        tag: Optional[str] = None,
        language: Optional[str] = None,
        min_sharpe: Optional[float] = None,
        min_fitness: Optional[float] = None,
        max_turnover: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Get user's alphas with server-side filtering and Redis caching (1 day TTL).

        All filters are applied server-side by the BRAIN API (verified by live
        probes against /users/self/alphas):
        - region      -> settings.region=<REGION>  (a bare 'region' param is silently ignored)
        - status      -> status=<STATUS>
        - alpha_type  -> type=<TYPE>
        - is_super    -> type=SUPER (True) / type!=SUPER (False)
        - color       -> color=<COLOR>  (case-sensitive uppercase: RED/GREEN/BLUE/YELLOW/PURPLE)
        - name        -> name=<NAME>    (exact match only; substring operators don't exist)
        - tag         -> tag=<TAG>      (singular 'tag'; a 'tags' param is silently ignored)
        - dates       -> dateCreated>/dateCreated</dateSubmitted>/dateSubmitted<
                         (values MUST be ISO 8601 with timezone; naive values are
                         normalized via _normalize_brain_datetime; '>=' does not exist)
        - order supports nested fields too (e.g. '-is.sharpe'), hidden is true/false
        - IS metrics -> is.sharpe>/is.fitness>/is.turnover< (verified live: on one
          day's 6300 alphas, is.sharpe>1.0 returned 2411 and is.fitness>1.0 1016)
        """
        await self.ensure_authenticated()

        try:
            api_params: Dict[str, Any] = {"stage": stage}
            if start_date:
                api_params["dateCreated>"] = self._normalize_brain_datetime(start_date)
            if end_date:
                api_params["dateCreated<"] = self._normalize_brain_datetime(end_date)
            if submission_start_date:
                api_params["dateSubmitted>"] = self._normalize_brain_datetime(submission_start_date)
            if submission_end_date:
                api_params["dateSubmitted<"] = self._normalize_brain_datetime(submission_end_date)
            if order:
                api_params["order"] = order
            if hidden is not None:
                api_params["hidden"] = str(hidden).lower()
            if region:
                api_params["settings.region"] = region.upper()
            if status:
                api_params["status"] = status.upper()
            if color:
                api_params["color"] = color.upper()
            if name:
                api_params["name"] = name
            if tag:
                api_params["tag"] = tag
            if language:
                api_params["settings.language"] = language.upper()
            # IS metric filters are applied server-side, which is by far the
            # cheapest way to narrow a result set: measured on one day's alphas,
            # is.sharpe>1.0 cut 6300 rows to 2411 and is.fitness>1.0 to 1016.
            if min_sharpe is not None:
                api_params["is.sharpe>"] = min_sharpe
            if min_fitness is not None:
                api_params["is.fitness>"] = min_fitness
            if max_turnover is not None:
                api_params["is.turnover<"] = max_turnover
            if alpha_type:
                api_params["type"] = alpha_type.upper()
            if is_super is not None:
                if alpha_type and (alpha_type.upper() == 'SUPER') != is_super:
                    # Contradictory filters can never match anything
                    return {
                        'count': 0, 'next': None, 'previous': None, 'results': [],
                        'from_cache': False,
                        'note': f"type={alpha_type} contradicts is_super={is_super}",
                    }
                if is_super:
                    api_params["type"] = "SUPER"
                elif not alpha_type:
                    api_params["type!"] = "SUPER"

            api_params["limit"] = limit
            api_params["offset"] = offset

            cache_key = self._generate_cache_key('user_alphas', api_params)
            cached_data = self._get_cached_data(cache_key)
            if cached_data:
                return {**cached_data, 'from_cache': True}

            data = await self._request_json_with_retries(
                'GET',
                f"{self.base_url}/users/self/alphas",
                params=api_params,
                op_name=f"get_user_alphas(stage={stage}, offset={offset})",
            )

            data['from_cache'] = False
            self._set_cached_data(cache_key, data, ttl=86400)
            return data

        except requests.HTTPError as e:
            # Surface the API's error body (e.g. ['Expected ISO 8601 datetime with timezone'])
            body = ''
            try:
                body = (e.response.text or '')[:300]
            except Exception:
                pass
            self.log(f"Failed to get user alphas: {e} body={body}", "ERROR")
            raise requests.HTTPError(f"{e} — {body}", response=e.response) from e
        except Exception as e:
            self.log(f"Failed to get user alphas: {str(e)}", "ERROR")
            raise
    
    def pre_submit_check(self, alpha_details: Dict[str, Any]) -> Dict[str, Any]:
        """Check IS metrics against submission thresholds before submitting.

        Criteria:
        - Sharpe > 1.3 and Fitness > 0.75 (relaxed thresholds for pre-submission check)
        - Margin > 0.05% for USA, otherwise > 0.15% (hard floor 0.08%)
        - Turnover between 4% and 40%
        - Returns > 4%
        - All other IS checks must PASS (no FAIL)
        """
        is_data = alpha_details.get('is')
        if not is_data:
            return {'passed': False, 'reason': 'No IS data available for this alpha. Simulation may not be complete.', 'details': []}

        failures = []
        warnings = []

        sharpe = is_data.get('sharpe', 0)
        fitness = is_data.get('fitness', 0)
        margin = is_data.get('margin', 0)
        turnover = is_data.get('turnover', 0)
        returns = is_data.get('returns', 0)
        drawdown = is_data.get('drawdown', 0)
        settings = alpha_details.get('settings') or {}
        region = (settings.get('region') or alpha_details.get('region') or '').upper()

        # Sharpe > 1.3
        if sharpe <= 1.3:
            failures.append(f'Sharpe {sharpe} <= 1.3 (required > 1.3)')

        # Fitness > 0.75
        if fitness <= 0.75:
            failures.append(f'Fitness {fitness} <= 0.75 (required > 0.75)')

        # USA margin rule is relaxed to >5bp. Other regions keep the >15bp target with a 8bp hard floor.
        if region == 'USA':
            if margin <= 0.0005:
                failures.append(f'Margin {margin*100:.4f}% <= 5bp (required > 5bp for USA)')
        else:
            if margin <= 0.0008:
                failures.append(f'Margin {margin*100:.4f}% <= 8bp (hard floor, required > 15bp)')
            elif margin <= 0.0015:
                warnings.append(f'Margin {margin*100:.4f}% <= 15bp (recommended > 15bp, current above 8bp hard floor)')

        # Turnover between 4% and 40%
        if turnover < 0.04:
            failures.append(f'Turnover {turnover*100:.2f}% < 4% (required 4%-40%)')
        elif turnover > 0.40:
            failures.append(f'Turnover {turnover*100:.2f}% > 40% (required 4%-40%)')

        # Returns > 4%
        if returns <= 0.04:
            failures.append(f'Returns {returns*100:.2f}% <= 4% (required > 4%)')

        # # Returns > drawdown
        # if returns <= drawdown:
        #     failures.append(f'Returns {returns*100:.2f}% <= Drawdown {drawdown*100:.2f}% (required Returns > Drawdown)')

        # All other IS checks must not be FAIL
        checks = is_data.get('checks', [])
        for chk in checks:
            result = chk.get('result', '')
            name = chk.get('name', 'UNKNOWN')
            if result == 'FAIL':
                value = chk.get('value', 'N/A')
                limit = chk.get('limit', 'N/A')
                failures.append(f'IS check {name} FAILED (value={value}, limit={limit})')

        passed = len(failures) == 0
        return {
            'passed': passed,
            'failures': failures,
            'warnings': warnings,
            'metrics': {
                'region': region or None,
                'sharpe': sharpe,
                'fitness': fitness,
                'margin': margin,
                'margin_bp': round(margin * 10000, 2),
                'turnover': turnover,
                'returns': returns,
                'drawdown': drawdown,
            },
            'is_checks_summary': [
                {'name': c.get('name'), 'result': c.get('result'), 'value': c.get('value'), 'limit': c.get('limit')}
                for c in checks
            ],
        }

    async def submit_alpha(self, alpha_id: str, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Submit an alpha for production and wait for the platform-side check to finish.

        Implements the correct submit flow from submit.py:
        1. POST to /alphas/{alpha_id}/submit
        2. If response has Retry-After header, switch to GET polling until no more retry-after
        3. Non-200/403 responses retry after 2 minutes
        4. Parses response JSON to check IS checks for ALREADY_SUBMITTED and FAILs

        The whole flow can take from a few minutes up to ~1 hour, bounded by
        BRAIN_SUBMIT_MAX_SECONDS (default 5400s). Progress is written into the
        optional `state` dict so callers can observe it concurrently.

        Returns a dict with at least {'status': 'SUCCESS' | 'ALREADY_SUBMITTED' |
        'FAILED' | 'TIMEOUT', 'message': str}.
        """
        await self.ensure_authenticated()

        submit_url = f"{self.base_url}/alphas/{alpha_id}/submit"
        attempt = 0
        deadline = time.monotonic() + self._submit_max_seconds

        def _update(phase: str, **extra: Any) -> None:
            if state is not None:
                state['phase'] = phase
                state['updated_at'] = time.time()
                state.update(extra)

        while True:
            if time.monotonic() > deadline:
                msg = f"Submission for {alpha_id} exceeded {self._submit_max_seconds:.0f}s without a final result"
                self.log(msg, "ERROR")
                return {'status': 'TIMEOUT', 'message': msg, 'attempts': attempt}

            attempt += 1
            self.log(f"Submit attempt {attempt} for alpha {alpha_id}", "INFO")
            _update('posting', attempts=attempt)

            try:
                response = await self._request('POST', submit_url)
            except Exception as e:
                self.log(f"Submit POST failed for {alpha_id}: {e}", "ERROR")
                raise

            self.log(f"Alpha submit, alpha_id={alpha_id}, status_code={response.status_code}", "INFO")
            _update('posted', last_status_code=response.status_code)

            # Handle Retry-After header: switch to GET polling
            polls = 0
            while 'retry-after' in {k.lower() for k in response.headers}:
                if time.monotonic() > deadline:
                    msg = f"Submission polling for {alpha_id} exceeded {self._submit_max_seconds:.0f}s"
                    self.log(msg, "ERROR")
                    return {'status': 'TIMEOUT', 'message': msg, 'attempts': attempt}
                retry_after_raw = response.headers.get('Retry-After') or response.headers.get('retry-after', '5')
                try:
                    wait_time = float(retry_after_raw)
                except ValueError:
                    wait_time = 5.0
                # Match reference: 5x multiplier for short waits
                actual_wait = 5 * wait_time if wait_time < 60 else wait_time
                polls += 1
                self.log(f"Submission processing (Retry-After={retry_after_raw}s), waiting {actual_wait:.0f}s then GET polling...", "INFO")
                _update('polling', polls=polls, retry_after_seconds=actual_wait)
                await asyncio.sleep(actual_wait)
                try:
                    response = await self._request('GET', submit_url)
                    self.log(f"GET poll response, alpha_id={alpha_id}, status_code={response.status_code}", "INFO")
                    _update('polling', last_status_code=response.status_code)
                except Exception as e:
                    self.log(f"Submit GET poll failed for {alpha_id}: {e}", "ERROR")
                    raise

            if response.status_code == 200:
                # Parse response JSON to validate IS checks
                try:
                    res_json = response.json()
                except (json.JSONDecodeError, ValueError):
                    msg = f"Submit response for {alpha_id} is not valid JSON: {(response.text or '')[:200]}"
                    self.log(msg, "ERROR")
                    return {'status': 'FAILED', 'message': msg, 'attempts': attempt}

                if not res_json:
                    return {'status': 'FAILED', 'message': 'Empty submit response body', 'attempts': attempt}

                if 'detail' in res_json and res_json['detail'] == 'Not found.':
                    msg = f"Submit failed: alpha {alpha_id} not found"
                    self.log(msg, "ERROR")
                    return {'status': 'FAILED', 'message': msg, 'attempts': attempt}

                # Check IS checks in response
                if 'is' in res_json and 'checks' in res_json['is']:
                    failed_checks = []
                    for item in res_json['is']['checks']:
                        if item.get('name') == 'ALREADY_SUBMITTED':
                            self.log(f"Alpha {alpha_id} already submitted", "WARNING")
                            return {'status': 'ALREADY_SUBMITTED',
                                    'message': f'Alpha {alpha_id} was already submitted',
                                    'attempts': attempt}
                        if item.get('result') == 'FAIL':
                            self.log(f"Alpha {alpha_id} IS check failed: {item.get('name')} limit={item.get('limit')} value={item.get('value')}", "ERROR")
                            failed_checks.append({'name': item.get('name'), 'limit': item.get('limit'), 'value': item.get('value')})
                    if failed_checks:
                        return {'status': 'FAILED',
                                'message': f"Submission checks failed for {alpha_id}",
                                'failed_checks': failed_checks,
                                'attempts': attempt}

                self.log(f"Alpha {alpha_id} submission successful!", "INFO")
                # The OS pool just gained a member: drop the cached lists so the
                # next self-correlation check sees it immediately.
                self._invalidate_os_list_cache(f"alpha {alpha_id} submitted")
                return {'status': 'SUCCESS',
                        'message': f'Alpha {alpha_id} submitted successfully',
                        'attempts': attempt}

            elif response.status_code == 403:
                self.log(f"Submit forbidden (403) for alpha {alpha_id}", "ERROR")
                return {'status': 'FAILED',
                        'message': f'Submit forbidden (403) for alpha {alpha_id}',
                        'attempts': attempt}

            else:
                self.log(f"Submit failed status={response.status_code} for {alpha_id}, waiting 2 minutes before retry...", "WARNING")
                _update('retry_wait', last_status_code=response.status_code)
                await asyncio.sleep(120)

    # --- Asynchronous submission management ---

    _SUBMISSION_TERMINAL_STATUSES = {'SUCCESS', 'ALREADY_SUBMITTED', 'FAILED', 'TIMEOUT', 'ERROR'}

    def _submission_state_snapshot(self, alpha_id: str) -> Optional[Dict[str, Any]]:
        state = self._submission_states.get(alpha_id)
        if state is None:
            return None
        snap = dict(state)
        started = snap.get('started_at')
        if started:
            snap['elapsed_seconds'] = round(time.time() - started, 1)
        return snap

    def start_submission(self, alpha_id: str, pre_check: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Start a background submission task for an alpha; returns immediately.

        Submissions are serialized (the platform runs one submission check at a
        time), so a second submission started while another is running waits in
        QUEUED state.
        """
        existing = self._submission_tasks.get(alpha_id)
        if existing is not None and not existing.done():
            return {
                'status': 'ALREADY_RUNNING',
                'message': f'A submission for {alpha_id} is already in progress',
                'state': self._submission_state_snapshot(alpha_id),
            }

        self._submission_states[alpha_id] = {
            'alpha_id': alpha_id,
            'status': 'QUEUED',
            'phase': 'queued',
            'attempts': 0,
            'polls': 0,
            'started_at': time.time(),
            'updated_at': time.time(),
            'finished_at': None,
            'result': None,
            'error': None,
            'pre_check': pre_check,
        }
        task = asyncio.create_task(self._run_submission(alpha_id))
        self._submission_tasks[alpha_id] = task
        return {'status': 'STARTED', 'state': self._submission_state_snapshot(alpha_id)}

    async def _run_submission(self, alpha_id: str) -> None:
        state = self._submission_states[alpha_id]
        try:
            async with self._submission_serialize_lock:
                state['status'] = 'RUNNING'
                state['updated_at'] = time.time()
                result = await self.submit_alpha(alpha_id, state=state)
            state['result'] = result
            state['status'] = result.get('status', 'ERROR')
        except Exception as e:
            state['status'] = 'ERROR'
            state['error'] = str(e)
            self.log(f"Background submission for {alpha_id} crashed: {e}", "ERROR")
        finally:
            state['finished_at'] = time.time()
            state['updated_at'] = time.time()

    async def get_submission_status(self, alpha_id: str) -> Dict[str, Any]:
        """Report the status of a submission started via start_submission.

        Falls back to a live platform probe (alpha stage / dateSubmitted) when
        there is no in-memory record, e.g. after a server restart.
        """
        snap = self._submission_state_snapshot(alpha_id)
        if snap is not None:
            done = snap.get('status') in self._SUBMISSION_TERMINAL_STATUSES
            return {'source': 'tracker', 'done': done, **snap}

        # No in-memory record — probe the platform directly (bypass the cache:
        # the whole point of this call is to observe a stage transition)
        details = await self.get_alpha_details(alpha_id, force_refresh=True)
        stage = details.get('stage')
        date_submitted = details.get('dateSubmitted')
        submitted = stage == 'OS' or bool(date_submitted)
        return {
            'source': 'live',
            'done': submitted,
            'alpha_id': alpha_id,
            'status': 'SUCCESS' if submitted else 'UNKNOWN',
            'stage': stage,
            'date_submitted': date_submitted,
            'message': (
                f'Alpha {alpha_id} is submitted (stage={stage})' if submitted
                else f'No tracked submission for {alpha_id} in this server session and the alpha is not submitted (stage={stage}). '
                     f'Call submit_alpha to start a submission.'
            ),
        }
    
    async def get_events(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Get available events and competitions (announcement-driven; cached 1h)."""
        await self.ensure_authenticated()
        try:
            return await self._cached_get(
                f"{self.base_url}/events", namespace='events', key='all',
                redis_ttl=3600, force_refresh=force_refresh)
        except Exception as e:
            self.log(f"Failed to get events: {str(e)}", "ERROR")
            raise

    async def get_leaderboard(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Get leaderboard data."""
        await self.ensure_authenticated()
        
        try:
            params = {}
            
            if user_id:
                params['user'] = user_id
            else:
                resolved = await self.get_self_user_id()
                if resolved:
                    params['user'] = resolved
            
            response = await self._request('GET', f"{self.base_url}/consultant/boards/leader", params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get leaderboard: {str(e)}", "ERROR")
            raise

    # --- SPC (Systematic Predictions Challenge) ---

    SPC_MODELS = ("gpt", "claude", "gemini", "deepseek", "kimi", "qwen", "glm", "llama", "minimax", "mistral")
    SPC_FREQUENCIES = ("daily", "weekly", "monthly", "quarterly")
    _SPC_ISIN_MIC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]\|[A-Z0-9]{4}$")

    @staticmethod
    def _spc_isin_checksum_valid(isin: str) -> bool:
        expanded = ""
        for char in isin:
            if char.isdigit():
                expanded += char
            elif "A" <= char <= "Z":
                expanded += str(ord(char) - ord("A") + 10)
            else:
                return False
        total = 0
        double = False
        for digit_char in reversed(expanded):
            digit = int(digit_char)
            if double:
                digit *= 2
            total += digit // 10 + digit % 10
            double = not double
        return total % 10 == 0

    def _validate_spc_sample_output(self, sample_output: str) -> List[str]:
        """Validate an SPC sample output string against the competition contract.

        Checks: parseable JSON object, ISIN|MIC key format, ISIN checksum,
        numeric confidence scores within [-1, 1]. Returns a list of error strings.
        """
        errors: List[str] = []
        stripped = (sample_output or "").strip()
        if not stripped:
            return ["sample_output is empty"]
        if stripped.startswith("```") or stripped.endswith("```"):
            errors.append("sample_output contains markdown code fences; must be pure JSON")
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            errors.append(f"sample_output is not valid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}")
            return errors
        if not isinstance(data, dict):
            errors.append("sample_output top-level value must be a JSON object")
            return errors
        if not data:
            errors.append("sample_output object must not be empty")
        for key, value in data.items():
            if not self._SPC_ISIN_MIC_RE.match(str(key)):
                errors.append(f"invalid key format, expected ISIN|MIC: {key!r}")
                continue
            isin = str(key).split("|", 1)[0]
            if not self._spc_isin_checksum_valid(isin):
                errors.append(f"invalid ISIN checksum: {isin}")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"confidence score must be numeric for {key!r}")
            elif not math.isfinite(value):
                errors.append(f"confidence score must be finite for {key!r}")
            elif value < -1.0 or value > 1.0:
                errors.append(f"confidence score out of [-1, 1] for {key!r}: {value}")
        return errors

    def _validate_spc_fields(
        self,
        name: Optional[str] = None,
        prompt: Optional[str] = None,
        model: Optional[str] = None,
        weight: Optional[float] = None,
        update_frequency: Optional[str] = None,
    ) -> List[str]:
        """Validate SPC submission metadata fields. Only non-None fields are checked."""
        errors: List[str] = []
        if name is not None and len(name) > 200:
            errors.append(f"name exceeds 200 characters ({len(name)})")
        if prompt is not None:
            if not prompt.strip():
                errors.append("prompt is empty")
            if len(prompt) > 10000:
                errors.append(f"prompt exceeds 10000 characters ({len(prompt)})")
        if model is not None and model not in self.SPC_MODELS:
            errors.append(f"model must be one of {list(self.SPC_MODELS)}, got {model!r}")
        if update_frequency is not None and update_frequency not in self.SPC_FREQUENCIES:
            errors.append(f"update_frequency must be one of {list(self.SPC_FREQUENCIES)}, got {update_frequency!r}")
        if weight is not None and not (0.0 <= float(weight) <= 1.0):
            errors.append(f"weight must be between 0 and 1, got {weight}")
        return errors

    async def get_spc_submissions(self, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """List the current user's SPC prompt submissions."""
        await self.ensure_authenticated()
        try:
            response = await self._request(
                'GET',
                f"{self.base_url}/competitions/spc/submissions",
                params={"limit": limit, "offset": offset},
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get SPC submissions: {str(e)}", "ERROR")
            raise

    async def create_spc_submission(
        self,
        name: str,
        prompt: str,
        sample_output: str,
        model: str,
        model_version: str,
        weight: float,
        update_frequency: str,
        skip_validation: bool = False,
    ) -> Dict[str, Any]:
        """Create a new SPC prompt submission."""
        await self.ensure_authenticated()
        if not skip_validation:
            errors = self._validate_spc_fields(name, prompt, model, weight, update_frequency)
            errors += self._validate_spc_sample_output(sample_output)
            if errors:
                return {
                    "error": "Local validation failed; nothing was submitted",
                    "validation_errors": errors,
                    "hint": "Fix the errors or pass skip_validation=true to submit anyway",
                }
        payload = {
            "name": name,
            "prompt": prompt,
            "sampleOutput": sample_output,
            "model": model,
            "modelVersion": model_version,
            "weight": round(float(weight), 2),
            "updateFrequency": update_frequency,
        }
        try:
            response = await self._request(
                'POST', f"{self.base_url}/competitions/spc/submissions", json=payload
            )
            if response.status_code == 400:
                return {"error": "Server rejected the submission", "details": self._response_payload(response)}
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to create SPC submission: {str(e)}", "ERROR")
            raise

    async def set_spc_submission_weight(self, submission_id: str, weight: float) -> Dict[str, Any]:
        """Set the weight of an existing SPC submission. weight=0 withdraws the prompt.

        The API only allows changing weight after creation; all other fields are
        immutable. To change a prompt's content, create a new submission and set
        the old one's weight to 0.
        """
        await self.ensure_authenticated()
        errors = self._validate_spc_fields(weight=weight)
        if errors:
            return {"error": "Local validation failed; nothing was updated", "validation_errors": errors}
        try:
            response = await self._request(
                'PATCH',
                f"{self.base_url}/competitions/spc/submissions/{submission_id}",
                json={"weight": round(float(weight), 2)},
            )
            if response.status_code == 400:
                return {"error": "Server rejected the update", "details": self._response_payload(response)}
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to update SPC submission {submission_id}: {str(e)}", "ERROR")
            raise

    async def get_spc_leaderboard(
        self,
        board: Optional[str] = None,
        limit: int = 30,
        offset: int = 0,
        aggregate: str = "user",
    ) -> Dict[str, Any]:
        """Get the SPC leaderboard. board is a month key like '202607' (defaults to current month server-side)."""
        await self.ensure_authenticated()
        params: Dict[str, Any] = {"limit": limit, "offset": offset, "aggregate": aggregate}
        if board:
            params["board"] = board
        try:
            response = await self._request(
                'GET', f"{self.base_url}/consultant/boards/spc", params=params
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get SPC leaderboard: {str(e)}", "ERROR")
            raise

    def _is_atom(self, detail: Optional[Dict[str, Any]]) -> bool:
        """Match atom detection used in extract_regular_alphas.py:
        - Primary signal: 'classifications' entries containing 'SINGLE_DATA_SET'
        - Fallbacks: tags list contains 'atom' or classification id/name contains 'ATOM'
        """
        if not detail or not isinstance(detail, dict):
            return False

        classifications = detail.get('classifications') or []
        for c in classifications:
            cid = (c.get('id') or c.get('name') or '')
            if isinstance(cid, str) and 'SINGLE_DATA_SET' in cid:
                return True

        # Fallbacks
        tags = detail.get('tags') or []
        if isinstance(tags, list):
            for t in tags:
                if isinstance(t, str) and t.strip().lower() == 'atom':
                    return True

        for c in classifications:
            cid = (c.get('id') or c.get('name') or '')
            if isinstance(cid, str) and 'ATOM' in cid.upper():
                return True

        return False

    async def value_factor_trendScore(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """Compute diversity score for regular alphas in a date range.

        Description:
        This function calculate the diversity of the users' submission, by checking the diversity, we can have a good understanding on the valuefactor's trend.
        value factor of a user is defiend by This diversity score, which measures three key aspects of work output: the proportion of works
        with the "Atom" tag (S_A), atom proportion, the breadth of pyramids covered (S_P), and how evenly works
        are distributed across those pyramids (S_H). Calculated as their product, it rewards
        strong performance across all three dimensions—encouraging more Atom-tagged works,
        wider pyramid coverage, and balanced distribution—with weaknesses in any area lowering
        the total score significantly.

        Inputs (hints for AI callers):
        - start_date (str): ISO UTC start datetime, e.g. '2025-08-14T00:00:00Z'
        - end_date (str): ISO UTC end datetime, e.g. '2025-08-18T23:59:59Z'
        - Note: this tool always uses 'OS' (submission dates) to define the window; callers do not need to supply a stage.
                - Note: P_max (total number of possible pyramids) is derived from the platform
                    pyramid-multipliers endpoint and not supplied by callers.

        Returns (compact JSON): {
            'diversity_score': float,
            'N': int,  # total regular alphas in window
            'A': int,  # number of Atom-tagged works (is_single_data_set)
            'P': int,  # pyramid coverage count in the sample
            'P_max': int, # used max for normalization
            'S_A': float, 'S_P': float, 'S_H': float,
            'per_pyramid_counts': {pyramid_name: count}
        }
        """
        # Fetch user alphas (always use OS / submission dates per product policy)
        await self.ensure_authenticated()
        # The API caps `limit` at 100 (limit=101+ is rejected, and a larger value
        # was silently truncated to 100 pages worth of rows), so the window is
        # paged explicitly instead of asking for 500 and quietly scoring only
        # the first 100 alphas.
        alphas: List[Dict[str, Any]] = []
        page_size = 100
        offset = 0
        while True:
            alphas_resp = await self.get_user_alphas(
                stage='OS', limit=page_size, offset=offset,
                submission_start_date=start_date, submission_end_date=end_date,
            )
            if not isinstance(alphas_resp, dict) or 'results' not in alphas_resp:
                return {'error': 'Unexpected response from get_user_alphas', 'raw': alphas_resp}
            page = alphas_resp['results'] or []
            alphas.extend(page)
            total = alphas_resp.get('count')
            if len(page) < page_size or (isinstance(total, int) and len(alphas) >= total):
                break
            offset += page_size
        regular = [a for a in alphas if a.get('type') == 'REGULAR']

        # Fetch details for each regular alpha. These are OS records, so they are
        # served from the long-lived cache after the first sweep; the remaining
        # misses go out concurrently under the rate limiter rather than one
        # blocking round trip at a time.
        pyramid_list = []
        atom_count = 0
        per_pyramid = {}

        async def _detail_or_none(aid: Optional[str]):
            if not aid:
                return None
            try:
                return await self.get_alpha_details(aid)
            except Exception:
                return None

        details_list = await asyncio.gather(
            *[_detail_or_none(a.get('id')) for a in regular]
        )

        for detail in details_list:
            if not detail:
                continue

            is_atom = self._is_atom(detail)
            if is_atom:
                atom_count += 1

            # Extract pyramids
            ps = []
            if isinstance(detail.get('pyramids'), list):
                ps = [p.get('name') for p in detail.get('pyramids') if p.get('name')]
            else:
                pt = detail.get('pyramidThemes') or {}
                pss = pt.get('pyramids') if isinstance(pt, dict) else None
                if pss and isinstance(pss, list):
                    ps = [p.get('name') for p in pss if p.get('name')]

            for p in ps:
                pyramid_list.append(p)
                per_pyramid[p] = per_pyramid.get(p, 0) + 1

        N = len(regular)
        A = atom_count
        P = len(per_pyramid)

        # Determine P_max similarly to the script: use pyramid multipliers if available
        P_max = None
        try:
            pm = await self.get_pyramid_multipliers()
            if isinstance(pm, dict) and 'pyramids' in pm:
                pyramids_list = pm.get('pyramids') or []
                P_max = len(pyramids_list)
        except Exception:
            P_max = None

        if not P_max or P_max <= 0:
            P_max = max(P, 1)

        # Component scores
        S_A = (A / N) if N > 0 else 0.0
        S_P = (P / P_max) if P_max > 0 else 0.0

        # Entropy
        S_H = 0.0
        if P <= 1 or not per_pyramid:
            S_H = 0.0
        else:
            total_occ = sum(per_pyramid.values())
            H = 0.0
            for cnt in per_pyramid.values():
                q = cnt / total_occ if total_occ > 0 else 0
                if q > 0:
                    H -= q * math.log2(q)
            max_H = math.log2(P) if P > 0 else 1
            S_H = (H / max_H) if max_H > 0 else 0.0

        diversity_score = S_A * S_P * S_H

        return {
            'diversity_score': diversity_score,
            'N': N,
            'A': A,
            'P': P,
            'P_max': P_max,
            'S_A': S_A,
            'S_P': S_P,
            'S_H': S_H,
            'per_pyramid_counts': per_pyramid
        }

    async def get_operators(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Get available operators for alpha creation.

        The operator catalogue is a static, sizeable document that changes only
        when the platform ships a release, so it is stored permanently and
        refreshed on demand via ``sync_platform_cache`` rather than re-downloaded
        on every expression-authoring call.
        """
        await self.ensure_authenticated()

        if not force_refresh:
            stored = await self.store.get('operators', 'all')
            if stored:
                return stored

        try:
            response = await self._request('GET', f"{self.base_url}/operators")
            response.raise_for_status()
            data = response.json()
            if data:
                await self.store.put('operators', 'all', data)
            return data
        except Exception as e:
            self.log(f"Failed to get operators: {str(e)}", "ERROR")
            raise

    async def recommend_datasets(self, region: str = "USA", delay: int = 1,
                                  universe: str = "TOP3000", top_n: int = 20) -> Dict[str, Any]:
        """Recommend datasets with unlit pyramid priority and in-pyramid quality ranking.

        The ranking favors datasets from unlit pyramids first. Within those
        pyramids it prefers high OS/IS Sharpe, high dataset userCount, and high
        dataset alphaCount, with a small random component to avoid returning the
        exact same list on every call.
        """
        await self.ensure_authenticated()

        def _category_id(value: Any) -> str:
            if isinstance(value, dict):
                return str(value.get('id') or '')
            return str(value or '')

        def _category_name(value: Any) -> str:
            if isinstance(value, dict):
                return str(value.get('name') or value.get('id') or '')
            return str(value or '')

        def _as_float(value: Any, default: float = 0.0) -> float:
            try:
                if value is None:
                    return default
                return float(value)
            except (TypeError, ValueError):
                return default

        def _as_int(value: Any, default: int = 0) -> int:
            try:
                if value is None:
                    return default
                return int(float(value))
            except (TypeError, ValueError):
                return default

        def _rank_score(value: Optional[float], values: List[float], points: float) -> float:
            """Return 0..points based on value's rank in the supplied sample."""
            if value is None or not values:
                return 0.0
            sorted_values = sorted(values)
            if len(sorted_values) == 1:
                return points
            below_or_equal = sum(1 for item in sorted_values if item <= value)
            percentile = (below_or_equal - 1) / (len(sorted_values) - 1)
            return points * max(0.0, min(1.0, percentile))

        def _log_score(value: int, values: List[int], points: float) -> float:
            if not values:
                return 0.0
            log_values = [math.log1p(max(0, item)) for item in values]
            return _rank_score(math.log1p(max(0, value)), log_values, points)

        def _region_delay_match(item: Dict[str, Any]) -> bool:
            return item.get('region') == region and _as_int(item.get('delay'), -1) == delay

        def _platform_row_match(item: Dict[str, Any], require_universe: bool = True) -> bool:
            if item.get('InstrumentType') != 'EQUITY':
                return False
            if item.get('Region') != region:
                return False
            if _as_int(item.get('Delay'), -1) != delay:
                return False
            if not require_universe:
                return True
            return universe in (item.get('Universe') or [])

        # ---- 1. Fetch pyramid status from the platform's pyramid endpoints ----
        pyramid_alphas: Dict[str, Any] = {}
        pyramid_multipliers: Dict[str, Any] = {}
        try:
            pyramid_alphas, pyramid_multipliers = await asyncio.gather(
                self.get_pyramid_alphas(),
                self.get_pyramid_multipliers(),
            )
        except Exception as e:
            self.log(f"Failed to fetch pyramid status: {e}", "WARNING")

        pyramid_summary: Dict[str, Dict[str, Any]] = {}
        for item in pyramid_multipliers.get('pyramids', []):
            if not isinstance(item, dict) or not _region_delay_match(item):
                continue
            cat_obj = item.get('category', {})
            cat_id = _category_id(cat_obj)
            if not cat_id:
                continue
            pyramid_summary[cat_id] = {
                'category_id': cat_id,
                'category_name': _category_name(cat_obj),
                'alpha_count': 0,
                'need_to_light': 3,
                'lit': False,
                'multiplier': _as_float(item.get('multiplier'), 1.0),
            }

        for item in pyramid_alphas.get('pyramids', []):
            if not isinstance(item, dict) or not _region_delay_match(item):
                continue
            cat_obj = item.get('category', {})
            cat_id = _category_id(cat_obj)
            if not cat_id:
                continue
            alpha_count = _as_int(item.get('alphaCount'), 0)
            pyramid_summary.setdefault(cat_id, {
                'category_id': cat_id,
                'category_name': _category_name(cat_obj),
                'multiplier': 1.0,
            })
            pyramid_summary[cat_id].update({
                'category_name': pyramid_summary[cat_id].get('category_name') or _category_name(cat_obj),
                'alpha_count': alpha_count,
                'need_to_light': max(0, 3 - alpha_count),
                'lit': alpha_count >= 3,
            })

        # ---- 2. Fetch available datasets for this region/delay ----
        datasets_resp = await self.get_datasets(region=region, delay=delay, universe=universe)
        all_datasets = datasets_resp.get('results', [])
        if not all_datasets:
            return {'error': 'No datasets available for the given region/delay/universe'}

        # ---- 2.1. Fetch neutralization options for the same simulation settings ----
        neutralization_options: List[str] = []
        neutralization_info: Dict[str, Any] = {
            'instrument_type': 'EQUITY',
            'region': region,
            'delay': delay,
            'universe': universe,
            'options': neutralization_options,
            'available': False,
            'source': 'platform_setting_options',
        }
        try:
            platform_options = await self.get_platform_setting_options()
            setting_rows = platform_options.get('instrument_options', [])
            matching_rows = [
                item for item in setting_rows
                if isinstance(item, dict) and _platform_row_match(item)
            ]
            universe_matched = True
            if not matching_rows:
                matching_rows = [
                    item for item in setting_rows
                    if isinstance(item, dict) and _platform_row_match(item, require_universe=False)
                ]
                universe_matched = False

            neutralization_options = sorted({
                str(option)
                for row in matching_rows
                for option in (row.get('Neutralization') or [])
                if option
            })
            available_universes = sorted({
                str(option)
                for row in matching_rows
                for option in (row.get('Universe') or [])
                if option
            })
            neutralization_info.update({
                'options': neutralization_options,
                'available': bool(neutralization_options),
                'universe_matched': universe_matched,
                'available_universes': available_universes,
            })
        except Exception as e:
            self.log(f"Failed to fetch neutralization options for dataset recommendations: {e}", "WARNING")
            neutralization_info.update({
                'error': str(e),
                'available': False,
            })

        for ds in all_datasets:
            cat_obj = ds.get('category', {})
            cat_id = _category_id(cat_obj)
            if not cat_id:
                continue
            pyramid_summary.setdefault(cat_id, {
                'category_id': cat_id,
                'category_name': _category_name(cat_obj),
                'alpha_count': 0,
                'need_to_light': 3,
                'lit': False,
                'multiplier': _as_float(ds.get('pyramidMultiplier'), 1.0),
            })

        # ---- 3. Load dataset quality from OS/IS Sharpe (info_data.bin) ----
        region_key = f"{region}_{delay}"
        isos_info = self._isos_data.get(region_key, {}).get('isos', {}) if self._isos_data else {}
        dataset_sharpe_map = isos_info.get('dataset', {})

        datasets_by_category: Dict[str, List[Dict[str, Any]]] = {}
        for ds in all_datasets:
            cat_id = _category_id(ds.get('category', {}))
            datasets_by_category.setdefault(cat_id, []).append(ds)

        sharpe_values_by_category: Dict[str, List[float]] = {}
        user_counts_by_category: Dict[str, List[int]] = {}
        alpha_counts_by_category: Dict[str, List[int]] = {}
        for cat_id, datasets in datasets_by_category.items():
            for ds in datasets:
                ds_id = ds.get('id', '')
                ds_sharpe_info = dataset_sharpe_map.get(ds_id, {})
                ds_sharpe = ds_sharpe_info.get('sharpe_ratio') if isinstance(ds_sharpe_info, dict) else None
                if ds_sharpe is not None:
                    sharpe_values_by_category.setdefault(cat_id, []).append(_as_float(ds_sharpe))
                user_counts_by_category.setdefault(cat_id, []).append(_as_int(ds.get('userCount'), 0))
                alpha_counts_by_category.setdefault(cat_id, []).append(_as_int(ds.get('alphaCount'), 0))

        unlit_categories = {cat_id for cat_id, item in pyramid_summary.items() if not item.get('lit')}
        restrict_to_unlit = any(
            _category_id(ds.get('category', {})) in unlit_categories
            for ds in all_datasets
        )
        candidate_datasets = [
            ds for ds in all_datasets
            if not restrict_to_unlit or _category_id(ds.get('category', {})) in unlit_categories
        ]

        # ---- 4. Score each dataset ----
        scored_datasets = []
        max_multiplier = max(
            [_as_float(item.get('multiplier'), 1.0) for item in pyramid_summary.values()] or [1.0]
        )
        for ds in candidate_datasets:
            ds_id = ds.get('id', '')
            ds_name = ds.get('name', ds_id)
            cat_obj = ds.get('category', {})
            ds_category = _category_id(cat_obj)
            ds_category_name = _category_name(cat_obj)
            sub_obj = ds.get('subcategory', {})
            ds_subcategory = _category_id(sub_obj)
            pyramid = pyramid_summary.get(ds_category, {})

            # --- Pyramid lighting score (0~40 points) ---
            cat_lit = bool(pyramid.get('lit', False))
            cat_count = _as_int(pyramid.get('alpha_count'), 0)
            need = _as_int(pyramid.get('need_to_light'), max(0, 3 - cat_count))
            multiplier = _as_float(pyramid.get('multiplier'), _as_float(ds.get('pyramidMultiplier'), 1.0))
            if not cat_lit:
                need_score = 24.0 * (need / 3.0)
                multiplier_score = 16.0 * (multiplier / max(max_multiplier, 1.0))
                pyramid_score = min(40.0, need_score + multiplier_score)
            else:
                pyramid_score = 5.0 * (multiplier / max(max_multiplier, 1.0))

            # --- Quality score from OS/IS Sharpe (0~30 points) ---
            ds_sharpe_info = dataset_sharpe_map.get(ds_id, {})
            ds_sharpe = ds_sharpe_info.get('sharpe_ratio') if isinstance(ds_sharpe_info, dict) else None
            ds_sharpe_float = _as_float(ds_sharpe) if ds_sharpe is not None else None
            ds_os_count = _as_int(ds_sharpe_info.get('count'), 0) if isinstance(ds_sharpe_info, dict) else 0
            quality_score = _rank_score(
                ds_sharpe_float,
                sharpe_values_by_category.get(ds_category, []),
                30.0,
            )

            # --- Dataset popularity: prefer more users and more submitted alphas (0~20 points) ---
            ds_user_count = _as_int(ds.get('userCount'), 0)
            ds_alpha_count = _as_int(ds.get('alphaCount'), 0)
            usage_score = _log_score(
                ds_user_count,
                user_counts_by_category.get(ds_category, []),
                10.0,
            )
            submission_score = _log_score(
                ds_alpha_count,
                alpha_counts_by_category.get(ds_category, []),
                10.0,
            )

            # --- Controlled randomness (0~5 points) ---
            random_score = random.uniform(0.0, 5.0)
            total_score = pyramid_score + quality_score + usage_score + submission_score + random_score

            scored_datasets.append({
                'dataset_id': ds_id,
                'dataset_name': ds_name,
                'category': ds_category,
                'category_name': ds_category_name,
                'subcategory': ds_subcategory,
                'total_score': round(total_score, 2),
                'pyramid_score': round(pyramid_score, 2),
                'quality_score': round(quality_score, 2),
                'usage_score': round(usage_score, 2),
                'submission_score': round(submission_score, 2),
                'random_score': round(random_score, 2),
                'distribution_score': round(usage_score + submission_score, 2),
                'category_lit': cat_lit,
                'category_alpha_count': cat_count,
                'category_need_to_light': need,
                'pyramid_multiplier': multiplier,
                'dataset_user_count': ds_user_count,
                'dataset_alpha_count': ds_alpha_count,
                'dataset_submissions_this_quarter': None,
                'os_is_sharpe': round(ds_sharpe_float, 4) if ds_sharpe_float is not None else None,
                'os_is_count': ds_os_count,
                'neutralization_options': neutralization_options,
                'neutralization_info': neutralization_info,
            })

        # Sort by total_score descending
        scored_datasets.sort(key=lambda x: x['total_score'], reverse=True)

        lit_count = sum(1 for item in pyramid_summary.values() if item.get('lit'))
        unlit_count = sum(1 for item in pyramid_summary.values() if not item.get('lit'))
        unlit_category_ids = sorted([cat_id for cat_id, item in pyramid_summary.items() if not item.get('lit')])

        return {
            'region': region,
            'delay': delay,
            'universe': universe,
            'neutralization_options': neutralization_options,
            'neutralization_info': neutralization_info,
            'recommendations': scored_datasets[:top_n],
            'total_datasets_scored': len(scored_datasets),
            'total_available_datasets': len(all_datasets),
            'total_candidate_datasets': len(candidate_datasets),
            'restricted_to_unlit_categories': restrict_to_unlit,
            'category_summary': pyramid_summary,
            'pyramid_summary': pyramid_summary,
            'pyramid_status': {
                'lit_categories': lit_count,
                'unlit_categories': unlit_count,
                'total_categories': lit_count + unlit_count,
                'unlit_category_ids': unlit_category_ids,
            },
            'scoring_weights': {
                'pyramid_lighting': '0~40 pts (unlit pyramids get priority; higher multiplier helps)',
                'dataset_quality': '0~30 pts (higher OS/IS Sharpe rank within the same pyramid)',
                'dataset_usage': '0~10 pts (higher dataset userCount rank within the same pyramid)',
                'dataset_submissions': '0~10 pts (higher dataset alphaCount rank within the same pyramid)',
                'randomness': '0~5 pts (small random jitter for exploration)',
            }
        }
            
    async def run_selection(
        self,
        selection: str,
        instrument_type: str = "EQUITY",
        region: str = "USA",
        delay: int = 1,
        selection_limit: int = 1000,
        selection_handling: str = "POSITIVE"
    ) -> Dict[str, Any]:
        """Run a selection query to filter instruments."""
        await self.ensure_authenticated()
        
        try:
            selection_data = {
                "selection": selection,
                "instrumentType": instrument_type,
                "region": region,
                "delay": delay,
                "selectionLimit": selection_limit,
                "selectionHandling": selection_handling
            }
            
            response = await self._request('GET', f"{self.base_url}/simulations/super-selection", params=selection_data)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to run selection: {str(e)}", "ERROR")
            raise

    async def get_user_profile(self, user_id: str = "self", force_refresh: bool = False) -> Dict[str, Any]:
        """Get user profile information (cached briefly; it changes rarely)."""
        await self.ensure_authenticated()
        try:
            return await self._cached_get(
                f"{self.base_url}/users/{user_id}",
                namespace='user_profile', key=str(user_id),
                redis_ttl=3600, force_refresh=force_refresh)
        except Exception as e:
            self.log(f"Failed to get user profile: {str(e)}", "ERROR")
            raise
            
    async def get_documentations(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Get available documentations and learning materials (stored permanently)."""
        await self.ensure_authenticated()
        try:
            return await self._cached_get(
                f"{self.base_url}/tutorials", namespace='tutorials', key='index',
                permanent=True, force_refresh=force_refresh)
        except Exception as e:
            self.log(f"Failed to get documentations: {str(e)}", "ERROR")
            raise
            
    async def get_messages(self, limit: Optional[int] = None, offset: int = 0) -> Dict[str, Any]:
        """Get messages for the current user with optional pagination.
        
        This function retrieves messages, processes their descriptions to extract
        and format embedded JSON, and handles file attachments by saving them locally.
        """
        from typing import Tuple
        
        def process_description(desc: str, message_id: str) -> Tuple[str, List[str]]:
            """
            Processes message description to handle HTML, embedded images, and JSON.
            """
            attachments = []
            
            # Handle embedded images
            soup = BeautifulSoup(desc, 'html.parser')
            for idx, img_tag in enumerate(soup.find_all('img')):
                src = img_tag.get('src', '')
                if src.startswith('data:image'):
                    try:
                        # Extract image data
                        header, encoded = src.split(',', 1)
                        ext = header.split(';')[0].split('/')[1]
                        safe_ext = re.sub(r'[^a-zA-Z0-9]', '', ext)
                        
                        # Decode and save image
                        content = base64.b64decode(encoded)
                        file_name = f"{message_id}_img_{idx}.{safe_ext}"
                        with open(file_name, "wb") as f:
                            f.write(content)
                        
                        # Update HTML and add attachment info
                        img_tag['src'] = file_name
                        attachments.append(f"Saved embedded image to ./{file_name}")
                        
                    except Exception as e:
                        attachments.append(f"Could not process embedded image: {e}")
            
            desc = str(soup)

            # Handle JSON content
            try:
                json_part_match = re.search(r'```json\n({.*?})\n```', desc, re.DOTALL)
                if json_part_match:
                    json_str = json_part_match.group(1)
                    desc = desc.replace(json_part_match.group(0), "").strip()
                    
                    try:
                        data = json.loads(json_str)
                        formatted_json = json.dumps(data, indent=2)
                        desc += f"\n\n---\n**Details**\n```json\n{formatted_json}\n```"
                    except json.JSONDecodeError:
                        desc += f"\n\n---\n**Details (raw)**\n{json_str}"
            except Exception:
                pass
                
            return desc, attachments

        await self.ensure_authenticated()
        
        try:
            params = {"limit": limit, "offset": offset}
            params = {k: v for k, v in params.items() if v is not None}
            
            response = await self._request('GET', f"{self.base_url}/users/self/messages", params=params)
            response.raise_for_status()
            messages_data = response.json()
            
            # Process descriptions and attachments
            for msg in messages_data.get("results", []):
                try:
                    msg_id = msg.get("id", "unknown_id")
                    new_desc, attachments = process_description(msg.get("description", ""), msg_id)
                    msg["description"] = new_desc
                    if attachments:
                        msg["attachments_info"] = attachments
                except Exception as e:
                    self.log(f"Error processing message {msg.get('id')}: {e}", "ERROR")

            return messages_data
            
        except Exception as e:
            self.log(f"Failed to get messages: {str(e)}", "ERROR")
            raise

    async def get_glossary_terms(self, email: str, password: str) -> List[Dict[str, str]]:
        """Get glossary terms from forum."""
        try:
            return await forum_client.get_glossary_terms(email, password)
        except Exception as e:
            self.log(f"Failed to get glossary terms: {str(e)}", "ERROR")
            raise

    async def search_forum_posts(self, email: str, password: str, search_query: str, 
                                 max_results: int = 50) -> Dict[str, Any]:
        """Search forum posts."""
        try:
            rate_limited = await self._rate_limit_forum_op("search_forum_posts")
            if rate_limited:
                return {
                    **rate_limited,
                    'operation': 'search_forum_posts',
                    'search_query': search_query,
                    'max_results': max_results,
                }
            return await forum_client.search_forum_posts(email, password, search_query, max_results)
        except Exception as e:
            self.log(f"Failed to search forum posts: {str(e)}", "ERROR")
            raise


    async def read_forum_post(self, email: str, password: str, article_id: str, 
                              include_comments: bool = True) -> Dict[str, Any]:
        """Get forum post."""
        try:
            rate_limited = await self._rate_limit_forum_op("read_forum_post")
            if rate_limited:
                return {
                    **rate_limited,
                    'operation': 'read_forum_post',
                    'article_id': article_id,
                    'include_comments': include_comments,
                }
            return await forum_client.read_full_forum_post(email, password, article_id, include_comments)
        except Exception as e:
            self.log(f"Failed to read forum post: {str(e)}", "ERROR")
            raise
    
    async def get_alpha_yearly_stats(self, alpha_id: str, force_refresh: bool = False) -> Dict[str, Any]:
        """Get yearly statistics for an alpha.

        Frozen like the PnL recordset: an alpha submitted 2025-03 still reports
        rows ending 2023, the same as one submitted today. Stored permanently.
        """
        await self.ensure_authenticated()

        if not force_refresh:
            stored = await self.store.get('yearly_stats', alpha_id)
            if stored:
                return stored

        max_retries = 5
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                self.log(f"Attempting to get yearly stats for alpha {alpha_id} (attempt {attempt + 1}/{max_retries})", "INFO")
                
                response = await self._request('GET', f"{self.base_url}/alphas/{alpha_id}/recordsets/yearly-stats")
                response.raise_for_status()
                
                text = (response.text or "").strip()
                if not text:
                    if attempt < max_retries - 1:
                        wait = self._recordset_retry_after(response, retry_delay)
                        self.log(f"Empty yearly stats response for {alpha_id}, retrying in {wait:.1f}s...", "WARNING")
                        await asyncio.sleep(wait)
                        retry_delay *= 1.5
                        continue
                    else:
                        return {}
                
                try:
                    stats_data = response.json()
                    if stats_data:
                        if isinstance(stats_data, dict):
                            await self.store.put('yearly_stats', alpha_id, stats_data)
                        return stats_data
                    else:
                        if attempt < max_retries - 1:
                            self.log(f"Empty yearly stats JSON for {alpha_id}, retrying...", "WARNING")
                            await asyncio.sleep(retry_delay)
                            retry_delay *= 1.5
                            continue
                        else:
                            return {}
                            
                except json.JSONDecodeError as parse_err:
                    if attempt < max_retries - 1:
                        self.log(f"Yearly stats JSON parse failed for {alpha_id}, retrying...", "WARNING")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 1.5
                        continue
                    else:
                        raise
                        
            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    self.log(f"Failed to get yearly stats for {alpha_id}, retrying: {e}", "WARNING")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 1.5
                    continue
                else:
                    raise
        
        return {}
        
    async def get_production_correlation(self, alpha_id: str) -> Dict[str, Any]:
        """Get production correlation data for an alpha.

        Polls every 30 seconds for up to 1 hour to handle platform rate-limiting.
        For super alphas, the platform may return an empty body (HTTP 200) for a few
        minutes after simulation completes while it computes the correlation data.
        The polling loop handles this by retrying until data is available.
        Returns {'status': 'pending', ...} after max_wait_seconds if data never arrives.

        Rate limited per account: at most ONE platform correlation check
        (production or power-pool) may START every
        BRAIN_CORRELATION_MIN_INTERVAL_SECONDS (default 180). If another check
        is running, or finished less than that interval ago, this returns
        ``correlation_busy`` immediately instead of queueing behind it. The gate
        is held in Redis AND in a host-level file lock, so it holds across
        processes and survives a Redis outage.
        """
        return await self._get_platform_correlation(alpha_id, 'prod', 'production')

    async def get_power_pool_correlation(self, alpha_id: str) -> Dict[str, Any]:
        """Get Power Pool (PPA) correlation from the platform endpoint.

        Hits ``GET /alphas/{id}/correlations/power-pool`` — the same
        compute-then-poll endpoint family as ``/correlations/prod`` (HTTP 200 +
        empty body + Retry-After while the platform computes). Unlike the prod
        histogram, the payload is self-correlation-shaped: per-alpha records
        (schema columns include id/name/correlation/...) plus top-level scalar
        ``min``/``max``.

        Shares the per-account correlation slot with the production check: the
        two together are limited to one start per
        BRAIN_CORRELATION_MIN_INTERVAL_SECONDS (default 180), enforced across
        processes, so a concurrent — or merely too-soon — prod OR power-pool
        check returns ``correlation_busy`` with a retry_after instead of
        queueing.
        """
        return await self._get_platform_correlation(alpha_id, 'power-pool', 'power pool')

    async def _get_platform_correlation(self, alpha_id: str, endpoint: str, label: str) -> Dict[str, Any]:
        """Shared lock-acquire/poll/release skeleton for the platform correlation
        endpoints (``prod`` and ``power-pool``). Both share one per-account lock."""
        await self.ensure_authenticated()

        op_name = f"get_{endpoint}_correlation({alpha_id})"
        lock_info = await self._try_acquire_brain_correlation_lock(op_name)
        if not lock_info.get('acquired'):
            interval = self._brain_correlation_min_interval_seconds
            remaining = lock_info.get('retry_after')
            # A running holder may keep the slot for up to the 2h lock TTL, but
            # it usually finishes in seconds — tell the caller to come back on
            # the interval, not at the TTL. A cooldown remainder is exact.
            default_retry = self._brain_correlation_busy_retry_after_seconds
            if isinstance(remaining, int) and remaining > 0:
                retry_after = max(1, min(remaining, max(interval, default_retry)))
            else:
                retry_after = default_retry
            reason = lock_info.get('reason')
            if reason == 'cooldown':
                why = (
                    f'The previous platform correlation check finished less than {interval}s ago. '
                    f'At most one platform correlation check (production or power-pool) may be '
                    f'started per {interval} seconds for this account.'
                )
            elif reason == 'lock_backend_unavailable':
                why = (
                    'The per-account correlation slot cannot be arbitrated right now '
                    '(neither Redis nor the file lock is usable), so the check is refused '
                    'rather than risking concurrent requests from multiple processes.'
                )
            else:
                why = (
                    'Another platform correlation check (production or power-pool) is already '
                    'running for this account. The BRAIN platform supports only one in-flight '
                    'correlation computation.'
                )
            return {
                'status': 'correlation_busy',
                'message': (
                    f'{why} The {label} correlation check cannot start. '
                    f'Please retry in {retry_after} seconds. '
                    '(Self-correlation is computed locally and is never gated — use '
                    'check_self_correlation freely.)'
                ),
                'reason': reason,
                'retry_after': retry_after,
                'lock_retry_after': remaining,
                'max': None,
                'records': [],
            }

        try:
            return await self._poll_platform_correlation(alpha_id, endpoint, label)
        finally:
            await self._release_brain_correlation_lock(lock_info, op_name)

    @staticmethod
    def _correlation_retry_after(response, default: float) -> float:
        """Seconds to wait before re-polling, from the Retry-After header when present."""
        try:
            value = float(response.headers.get('Retry-After'))
        except (TypeError, ValueError):
            return default
        # Clamp: the header is authoritative but must not stall or busy-spin.
        # BRAIN answers Retry-After: 1 while it computes, which would fire one
        # GET per second for the whole wait; floor it so a single check costs
        # tens of requests, not thousands.
        try:
            floor = max(0.5, float(os.environ.get("BRAIN_CORRELATION_POLL_MIN_SECONDS", "5")))
        except Exception:
            floor = 5.0
        return max(floor, min(value, default))

    @staticmethod
    def _ensure_correlation_extrema(corr_data: Dict[str, Any]) -> Dict[str, Any]:
        """Derive scalar 'max'/'min' from BRAIN's correlation HISTOGRAM payload.

        BRAIN returns {"schema": {"properties": [{"name": "min"}, {"name": "max"},
        {"name": "alphas"}]}, "records": [[lo, hi, count], ...]} — a histogram of how
        many production alphas fall in each 0.1-wide correlation bucket. There is no
        top-level scalar. The max correlation is the upper edge of the highest bucket
        with a NON-ZERO count (empty trailing buckets must be ignored, otherwise every
        alpha reports max == 1.0).

        Mutates and returns corr_data. A payload that already carries scalars, or that
        is not a histogram (e.g. the local self-correlation shape, whose records are
        dicts), is left untouched.
        """
        if not isinstance(corr_data, dict):
            return corr_data
        if corr_data.get('max') is not None and corr_data.get('min') is not None:
            return corr_data

        records = corr_data.get('records')
        if not isinstance(records, list) or not records:
            return corr_data

        props = [p.get('name') for p in (corr_data.get('schema') or {}).get('properties', [])
                 if isinstance(p, dict)]
        try:
            i_lo, i_hi, i_n = props.index('min'), props.index('max'), props.index('alphas')
        except ValueError:
            # Unknown schema — assume the documented [lo, hi, count] ordering.
            i_lo, i_hi, i_n = 0, 1, 2

        los, his = [], []
        for rec in records:
            if not isinstance(rec, (list, tuple)) or len(rec) <= max(i_lo, i_hi, i_n):
                continue
            try:
                count = float(rec[i_n])
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue                      # empty bucket carries no alpha
            try:
                los.append(float(rec[i_lo]))
                his.append(float(rec[i_hi]))
            except (TypeError, ValueError):
                continue

        if not his:
            return corr_data
        if corr_data.get('max') is None:
            corr_data['max'] = max(his)
        if corr_data.get('min') is None:
            corr_data['min'] = min(los)
        return corr_data

    @staticmethod
    def _normalize_record_style_correlation(corr_data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a self/power-pool-style correlation payload in place.

        That payload carries per-alpha records as [id, name, ..., correlation, ...]
        rows described by ``schema.properties`` plus top-level scalar min/max.
        Rewrites ``records`` into [{'id', 'name', 'region', 'universe',
        'correlation'}, ...] sorted by correlation desc, and guarantees a scalar
        'max' (0.0 for an empty pool) so callers and the polling loop can
        terminate on ``max is not None``. Histogram payloads (no 'correlation'
        column) are left untouched.
        """
        if not isinstance(corr_data, dict):
            return corr_data
        props = [p.get('name') for p in (corr_data.get('schema') or {}).get('properties', [])
                 if isinstance(p, dict)]
        if 'correlation' not in props:
            return corr_data
        records = corr_data.get('records')
        if isinstance(records, list):
            keep = ('id', 'name', 'region', 'universe', 'correlation')
            dict_records = []
            for rec in records:
                if isinstance(rec, dict):
                    dict_records.append({k: rec.get(k) for k in keep if k in rec})
                    continue
                if not isinstance(rec, (list, tuple)) or len(rec) != len(props):
                    continue
                row = dict(zip(props, rec))
                dict_records.append({k: row.get(k) for k in keep if k in row})
            dict_records.sort(
                key=lambda r: (r.get('correlation') is not None, r.get('correlation')),
                reverse=True,
            )
            corr_data['records'] = dict_records
            if corr_data.get('max') is None:
                corrs = [r['correlation'] for r in dict_records
                         if isinstance(r.get('correlation'), (int, float))]
                # Empty pool (e.g. no Power Pool Alphas yet) => correlation is 0.
                corr_data['max'] = max(corrs) if corrs else 0.0
        return corr_data

    async def _poll_platform_correlation(
        self,
        alpha_id: str,
        endpoint: str = 'prod',
        label: str = 'production',
    ) -> Dict[str, Any]:
        max_wait_seconds = self._brain_correlation_max_wait()
        poll_interval = 30       # ceiling per attempt; Retry-After shortens it
        start_time = time.time()
        attempt = 0
        consecutive_empty = 0    # track consecutive empty-body responses
        consecutive_network_failures = 0

        while True:
            elapsed = time.time() - start_time
            if elapsed >= max_wait_seconds:
                self.log(f"{label} correlation timeout after {int(elapsed)}s for {alpha_id}", "WARNING")
                return {
                    'status': 'pending',
                    'message': (
                        f"{label} correlation data for alpha {alpha_id} was not available "
                        f"after {int(elapsed)}s of polling. The platform may still be computing "
                        "it. Please retry in a few minutes."
                    ),
                    'max': None,
                    'records': [],
                }

            attempt += 1
            try:
                if attempt % 5 == 1:
                    self.log(f"[corr等待] 正在等待 Alpha {alpha_id} 的 {label} 相关性数据 (第 {attempt} 次查询, 已等待 {int(elapsed)}s)", "INFO")

                response = await self._request('GET', f"{self.base_url}/alphas/{alpha_id}/correlations/{endpoint}")
                response.raise_for_status()
                
                text = (response.text or "").strip()
                if not text:
                    consecutive_empty += 1
                    if consecutive_empty == 3:
                        # Platform is still computing — log once so users understand the wait
                        self.log(
                            f"[corr计算中] Alpha {alpha_id} 的 {label} 相关性数据尚未就绪 "
                            f"(已收到 {consecutive_empty} 次空响应). "
                            "平台正在计算中，通常需要 1-5 分钟，请耐心等待...",
                            "INFO"
                        )
                    # BRAIN answers 200 + EMPTY BODY while it computes, and tells us how
                    # long to wait via Retry-After (typically 1s). Honour it instead of
                    # sleeping a flat 30s — that alone turns a multi-minute poll into
                    # a few seconds and makes bulk screening practical.
                    await asyncio.sleep(self._correlation_retry_after(response, poll_interval))
                    continue

                # Got a non-empty response — reset empty counter
                consecutive_empty = 0
                try:
                    corr_data = response.json()
                except json.JSONDecodeError:
                    corr_data = None
                if corr_data:
                    # 'prod' payload is a HISTOGRAM with no top-level scalar: schema
                    # properties are [min, max, alphas] and each record is
                    # [bucket_lo, bucket_hi, count] — 'max' must be derived here.
                    # 'power-pool' payload is record-style (per-alpha rows with a
                    # 'correlation' column) and is normalized instead. Either way a
                    # scalar 'max' is what makes the poller terminate at all.
                    self._ensure_correlation_extrema(corr_data)
                    self._normalize_record_style_correlation(corr_data)
                    if corr_data.get('max') is not None:
                        self.log(f"[corr成功] Alpha {alpha_id} {label} corr={corr_data['max']} (第 {attempt} 次查询, 耗时 {int(elapsed)}s)", "INFO")
                        return corr_data

            except (requests.RequestException, ConnectionError, TimeoutError) as e:
                consecutive_network_failures += 1
                retry_delay = min(5 * consecutive_network_failures, poll_interval)
                self.log(
                    f"Failed to get {label} correlation for {alpha_id} "
                    f"(network failure {consecutive_network_failures}): {e}. "
                    f"Retrying in {retry_delay}s",
                    "WARNING"
                )
                await asyncio.sleep(retry_delay)
                continue

            consecutive_network_failures = 0
            
            await asyncio.sleep(poll_interval)

    @staticmethod
    def _pnl_response_to_series(aid: str, pnl_data: dict) -> Optional[pd.Series]:
        """Convert a raw PnL API response dict to a pandas Series indexed by date."""
        try:
            if not pnl_data:
                return None
            records = pnl_data.get('records', [])
            schema = pnl_data.get('schema', {}).get('properties', [])
            if not records or not schema:
                return None
            cols = [p['name'] for p in schema]
            df = pd.DataFrame(records, columns=cols)
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)
            if 'pnl' not in df.columns:
                return None
            return df['pnl'].rename(aid)
        except Exception:
            return None

    def _os_pnl_pool_path(
        self,
        instrument_type: str,
        region: str,
        universe: str,
        delay: Union[int, str],
    ) -> Path:
        """Return the on-disk cache path for a configuration-specific OS PnL pool."""
        cache_dir = Path(__file__).parent / 'downloads'
        cache_dir.mkdir(parents=True, exist_ok=True)
        safe_parts = [
            str(instrument_type).strip().lower() or 'unknown',
            str(region).strip().lower() or 'unknown',
            str(universe).strip().lower() or 'unknown',
            f"delay{delay}",
        ]
        return cache_dir / f"os_pnl_pool_{'_'.join(safe_parts)}.pkl"

    def _os_ppac_ids_path(
        self,
        instrument_type: str,
        region: str,
        universe: str,
        delay: Union[int, str],
    ) -> Path:
        """Sidecar file holding the Power-Pool-Alpha id set for a configuration.

        Self-correlation itself uses the whole OS pool; this sidecar exists so
        ``correlation_type='powerpool'`` can select the Power-Pool-Alpha subset
        without re-listing the account's alphas.
        """
        return self._os_pnl_pool_path(
            instrument_type, region, universe, delay
        ).with_suffix('.ppac.json')

    def _load_ppac_ids(
        self,
        instrument_type: str,
        region: str,
        universe: str,
        delay: Union[int, str],
    ) -> set:
        """Load the cached Power-Pool-Alpha id set (empty set if unavailable)."""
        try:
            p = self._os_ppac_ids_path(instrument_type, region, universe, delay)
            if p.exists():
                return set(json.loads(p.read_text()))
        except Exception as e:
            self.log(f"[SC cache] Failed to load ppac ids sidecar: {e}", "WARNING")
        return set()

    async def _get_os_pnl_pool_lock(self, pool_path: Path) -> asyncio.Lock:
        """Return the in-process lock for one configuration-specific OS PnL cache."""
        pool_key = str(pool_path)
        async with self._os_pnl_pool_locks_guard:
            lock = self._os_pnl_pool_locks.get(pool_key)
            if lock is None:
                lock = asyncio.Lock()
                self._os_pnl_pool_locks[pool_key] = lock
            return lock

    @staticmethod
    def _exclude_os_pnl_target(pool: pd.DataFrame, exclude_id: Optional[str]) -> pd.DataFrame:
        if exclude_id and isinstance(pool, pd.DataFrame) and exclude_id in pool.columns:
            return pool.drop(columns=[exclude_id], errors='ignore')
        return pool

    @staticmethod
    def _os_list_cache_key(instrument_type: str, region: str, universe: str,
                           delay: Union[int, str]) -> str:
        return f"os_alpha_ids:{instrument_type}:{region}:{universe}:{delay}"

    @staticmethod
    def _os_list_params(instrument_type: str, region: str, universe: str,
                        delay: Union[int, str]) -> Dict[str, Any]:
        return {
            'stage': 'OS',
            'order': '-dateSubmitted',
            'settings.instrumentType': instrument_type,
            'settings.region': region,
            'settings.universe': universe,
            'settings.delay': delay,
        }

    async def get_submitted_ids_since(self, since: Optional[str] = None) -> Dict[str, Any]:
        """Ids of alphas submitted since ``since`` (ISO 8601), newest first.

        Answering "which of these N alphas got submitted?" by fetching N alpha
        records costs N requests; the platform caps submissions at a handful per
        day, so the same question is answered by listing what was actually
        submitted in the window — 1-2 pages regardless of N.
        """
        await self.ensure_authenticated()
        ids: List[str] = []
        rows: Dict[str, Dict[str, Any]] = {}
        offset, page_size = 0, 100
        params_base: Dict[str, Any] = {'stage': 'OS', 'order': '-dateSubmitted'}
        if since:
            params_base['dateSubmitted>'] = self._normalize_brain_datetime(since)
        while True:
            data = await self._request_json_with_retries(
                'GET', f"{self.base_url}/users/self/alphas",
                params={**params_base, 'limit': page_size, 'offset': offset},
                op_name=f"get_submitted_ids_since(offset={offset})",
            )
            results = data.get('results') or []
            for a in results:
                aid = a.get('id')
                if aid:
                    ids.append(aid)
                    rows[aid] = a
            if len(results) < page_size:
                break
            offset += page_size
            if offset >= 1000:  # safety valve; the window should never be this wide
                break
        return {'ids': ids, 'rows': rows, 'since': since, 'pages': offset // page_size + 1}

    async def _os_list_probe(
        self, instrument_type: str, region: str, universe: str, delay: Union[int, str]
    ) -> Optional[Dict[str, Any]]:
        """One-row probe that says whether the OS list changed, without paging it.

        Measured cost of this endpoint is ~50 ms per returned row: a 1-row page is
        ~380-500 ms while a full 100-row page is ~5 s, and field selection is not
        supported (fields/only/include/omit are all silently ignored). So the
        cheapest correct freshness test is to ask for a single row ordered by
        -dateSubmitted and compare (count, newest id) with what we already have:
        a submission changes both, and a deletion changes the count.
        """
        params = self._os_list_params(instrument_type, region, universe, delay)
        params['limit'] = 1
        try:
            data = await self._request_json_with_retries(
                'GET', f"{self.base_url}/users/self/alphas",
                params=params, op_name='os_list_probe',
            )
        except Exception as e:
            self.log(f"[SC cache] OS list probe failed ({e}); will re-page", "WARNING")
            return None
        results = data.get('results') or []
        return {
            'count': data.get('count'),
            'newest_id': (results[0] or {}).get('id') if results else None,
        }

    def _invalidate_os_list_cache(self, reason: str) -> int:
        """Drop the cached OS alpha lists. Called after a successful submission,
        which is the only event that changes them."""
        if not self.redis_client:
            return 0
        dropped = 0
        try:
            for key in list(self.redis_client.scan_iter('os_alpha_ids:*', count=500)):
                self.redis_client.delete(key)
                dropped += 1
        except Exception as e:
            self.log(f"[SC cache] Failed to invalidate OS list cache: {e}", "WARNING")
            return dropped
        if dropped:
            self.log(f"[SC cache] Invalidated {dropped} OS list cache entr(ies): {reason}", "INFO")
        return dropped

    async def _list_matching_os_alpha_ids(
        self,
        instrument_type: str,
        region: str,
        universe: str,
        delay: Union[int, str],
    ) -> List[str]:
        """Fetch OS alpha IDs that match the target alpha's market configuration.

        BRAIN self-correlation semantics compare against the user's self alpha
        pool for the same instrument/region/universe/delay, and can include
        both REGULAR and SUPER alphas. Filtering locally on the same
        configuration keeps the local calculation aligned with those semantics.
        """
        all_ids: List[str] = []
        ppac_ids: List[str] = []

        cache_key = self._os_list_cache_key(instrument_type, region, universe, delay)
        cached = self._get_cached_data(cache_key)
        if cached and isinstance(cached.get('ids'), list):
            age = time.time() - float(cached.get('verified_at') or 0)
            # The ppac sidecar was written by the run that populated this entry
            # and is read separately, so it stays consistent either way.
            if self._os_list_ttl_seconds and age <= self._os_list_ttl_seconds:
                return cached['ids']
            probe = await self._os_list_probe(instrument_type, region, universe, delay)
            if probe and probe.get('count') == cached.get('count') \
                    and probe.get('newest_id') == cached.get('newest_id'):
                cached['verified_at'] = time.time()
                self._set_cached_data(cache_key, cached, ttl=self._os_list_retain_seconds)
                self.log(
                    f"[SC cache] OS list unchanged for {region}/{universe}/delay{delay} "
                    f"({cached.get('count')} alphas) — verified with 1 row, skipped paging",
                    "INFO",
                )
                return cached['ids']

        offset = 0
        page_size = 100
        server_count: Optional[int] = None
        newest_id: Optional[str] = None
        while True:
            # Live probes confirm settings.* filters are honoured server-side
            # (settings.delay=7 returns count=0, not the unfiltered set), so the
            # market configuration is filtered by the API instead of downloading
            # every OS alpha and discarding ~2/3 of them client-side.
            params = dict(self._os_list_params(instrument_type, region, universe, delay))
            params['limit'] = page_size
            params['offset'] = offset
            try:
                data = await self._request_json_with_retries(
                    'GET',
                    f"{self.base_url}/users/self/alphas",
                    params=params,
                    op_name=f"list_matching_os_alphas(offset={offset})",
                )
            except Exception as e:
                self.log(f"Failed to page OS alpha list at offset={offset}: {e}", "WARNING")
                break
            results = data.get('results') or []
            if server_count is None:
                server_count = data.get('count')
            if newest_id is None and results:
                # Ordered by -dateSubmitted, so the first row of the first page is
                # exactly what the 1-row freshness probe will return next time.
                newest_id = (results[0] or {}).get('id')
            if not results:
                break
            for alpha in results:
                if not alpha.get('id'):
                    continue
                settings = alpha.get('settings', {})
                if settings.get('instrumentType') != instrument_type:
                    continue
                if settings.get('region') != region:
                    continue
                if settings.get('universe') != universe:
                    continue
                if str(settings.get('delay')) != str(delay):
                    continue
                all_ids.append(alpha['id'])
                # A Power Pool Alpha is identified by its classifications, e.g.
                # {"id": "POWER_POOL_ALPHA", "name": "Power Pool Alpha"}. Recorded
                # so correlation_type='powerpool' can select just these; the
                # self-correlation pool keeps them. Match on id OR name (as the
                # atom detector does) to be robust to the key the API returns.
                classifications = alpha.get('classifications') or []
                for c in classifications:
                    if not isinstance(c, dict):
                        continue
                    cid = c.get('id') or c.get('name') or ''
                    cname = c.get('name') or ''
                    if (isinstance(cid, str) and 'POWER_POOL' in cid.upper()) or \
                       (isinstance(cname, str) and cname.strip() == 'Power Pool Alpha'):
                        ppac_ids.append(alpha['id'])
                        break
            if len(results) < page_size:
                break
            offset += page_size

        # Persist the Power-Pool-Alpha id set so get_self_correlation can
        # exclude/select them without re-listing (survives the sync debounce
        # cache and process restarts).
        try:
            sidecar = self._os_ppac_ids_path(instrument_type, region, universe, delay)
            sidecar.write_text(json.dumps(sorted(set(ppac_ids))))
        except Exception as e:
            self.log(f"[SC cache] Failed to persist ppac ids sidecar: {e}", "WARNING")

        if all_ids:
            self._set_cached_data(
                cache_key,
                {
                    'ids': all_ids,
                    'ppac': sorted(set(ppac_ids)),
                    # server_count is what the probe compares against; fall back to
                    # the number we actually collected when the API omits it.
                    'count': server_count if server_count is not None else len(all_ids),
                    'newest_id': newest_id,
                    'verified_at': time.time(),
                },
                ttl=self._os_list_retain_seconds,
            )

        return all_ids

    async def sync_os_pnl_pool(
        self,
        instrument_type: str,
        region: str,
        universe: str,
        delay: Union[int, str],
        exclude_id: Optional[str] = None,
    ) -> pd.DataFrame:
        """Incrementally sync the matching OS alpha PnL pool cache on disk.

        Closed-loop logic (mirrors the reference implementation):
        - Fetch the current server-side list of matching OS alpha IDs.
        - Load the local pickle cache (if any) and drop any columns whose alpha
          is no longer present on the server (handles deletions).
        - Download PnL only for IDs that are on the server but missing locally
          (OS alpha PnL is effectively static, so old columns are reused).
        - Persist the merged pool back to disk and return it.
        """
        pool_path = self._os_pnl_pool_path(instrument_type, region, universe, delay)
        pool_lock = await self._get_os_pnl_pool_lock(pool_path)
        async with pool_lock:
            pool_key = str(pool_path)
            debounce = self._os_pnl_pool_sync_debounce_seconds
            cached_sync = self._os_pnl_pool_last_sync.get(pool_key)
            if cached_sync and debounce > 0:
                synced_at, synced_pool = cached_sync
                if time.time() - synced_at <= debounce:
                    return self._exclude_os_pnl_target(synced_pool, exclude_id)

            synced_pool = await self._sync_os_pnl_pool_unlocked(
                pool_path=pool_path,
                instrument_type=instrument_type,
                region=region,
                universe=universe,
                delay=delay,
            )
            self._os_pnl_pool_last_sync[pool_key] = (time.time(), synced_pool)
            return self._exclude_os_pnl_target(synced_pool, exclude_id)

    async def _sync_os_pnl_pool_unlocked(
        self,
        pool_path: Path,
        instrument_type: str,
        region: str,
        universe: str,
        delay: Union[int, str],
    ) -> pd.DataFrame:
        server_ids = await self._list_matching_os_alpha_ids(
            instrument_type, region, universe, delay
        )

        # Load existing cache and drop removed alphas (closed-loop cleanup)
        local_pool = pd.DataFrame()
        if pool_path.exists():
            try:
                local_pool = await asyncio.to_thread(pd.read_pickle, pool_path)
                if isinstance(local_pool, pd.DataFrame) and not local_pool.empty:
                    keep_cols = [c for c in local_pool.columns if c in set(server_ids)]
                    dropped = local_pool.shape[1] - len(keep_cols)
                    if dropped > 0:
                        self.log(f"[SC cache] Dropping {dropped} alpha(s) removed from OS list", "INFO")
                    local_pool = local_pool[keep_cols]
                else:
                    local_pool = pd.DataFrame()
            except Exception as e:
                self.log(f"[SC cache] Failed to read pool pickle, rebuilding: {e}", "WARNING")
                local_pool = pd.DataFrame()

        need_download = [aid for aid in server_ids if aid not in local_pool.columns]

        if not need_download:
            self.log(f"[SC cache] Pool up-to-date: {local_pool.shape[1]} OS alphas", "INFO")
            return local_pool

        self.log(f"[SC cache] Incremental download: {len(need_download)} new OS alpha(s)", "INFO")

        fetch_sem = asyncio.Semaphore(5)

        async def fetch_one(oid: str):
            async with fetch_sem:
                try:
                    data = await self.get_alpha_pnl(oid)
                    return self._pnl_response_to_series(oid, data)
                except Exception as e:
                    self.log(f"[SC cache] Skip {oid}: PnL fetch failed ({e})", "WARNING")
                    return None

        fetched = await asyncio.gather(*[fetch_one(oid) for oid in need_download])
        new_series = [s for s in fetched if s is not None]

        if new_series:
            new_df = pd.concat(new_series, axis=1)
            full_pool = new_df if local_pool.empty else pd.concat([local_pool, new_df], axis=1)
            full_pool = full_pool.sort_index()
            try:
                await asyncio.to_thread(full_pool.to_pickle, pool_path)
            except Exception as e:
                self.log(f"[SC cache] Failed to persist pool pickle: {e}", "WARNING")
            local_pool = full_pool
            self.log(f"[SC cache] Pool now has {local_pool.shape[1]} OS alphas", "INFO")
        else:
            self.log(f"[SC cache] No new PnL captured (all fetches failed?); keeping {local_pool.shape[1]} cached", "WARNING")

        return local_pool

    async def get_self_correlation(self, alpha_id: str, correlation_type: str = 'self') -> Dict[str, Any]:
        """Calculate self-correlation locally using an incrementally-cached OS PnL pool.

        - OS alpha PnL is considered static and cached on disk
          (``downloads/os_pnl_pool.pkl``); only newly-submitted OS alphas are
          downloaded on each call and stale entries are pruned.
        - The target alpha's PnL is always fetched fresh (it is typically still
          IS and may change between calls).
        - Correlation is computed on the last 4 years of daily returns, matching
          the reference ``calculate_sc_locally`` semantics.

        correlation_type selects the pool:
          * 'self' (default) / 'all' -> the WHOLE OS pool, Power Pool Alphas
            included. They are submitted alphas of yours like any other, and
            excluding them made the pool empty (fake max=0.0) in configurations
            where every OS alpha happens to be a PPAC.
          * 'powerpool' -> ONLY Power Pool Alphas, a local approximation of the
            platform's "Power Pool Correlation" (use check_correlation(
            'powerpool') for the authoritative number).
        """
        await self.ensure_authenticated()

        try:
            # Target alpha PnL is always fresh; details are needed only for the
            # market-configuration key. Fetch both independent endpoints at once.
            target_pnl_data, target_details = await asyncio.gather(
                self.get_alpha_pnl(alpha_id),
                self.get_alpha_details(alpha_id),
            )
            target_series = self._pnl_response_to_series(alpha_id, target_pnl_data)
            if target_series is None:
                self.log(f"Could not parse PnL for target alpha {alpha_id}", "WARNING")
                return {}

            target_settings = target_details.get('settings', {})
            instrument_type = target_settings.get('instrumentType')
            region = target_settings.get('region')
            universe = target_settings.get('universe')
            delay = target_settings.get('delay')
            if not all([instrument_type, region, universe]) or delay is None:
                self.log(
                    f"Missing target settings for self-correlation on {alpha_id}: {target_settings}",
                    "WARNING",
                )
                return {}

            # Sync only the OS pool matching the target alpha's market configuration.
            os_pool = await self.sync_os_pnl_pool(
                instrument_type=instrument_type,
                region=region,
                universe=universe,
                delay=delay,
                exclude_id=alpha_id,
            )

            if os_pool is None or os_pool.empty:
                self.log(f"No OS alphas available; self-correlation for {alpha_id} is 0", "INFO")
                return {'max': 0.0, 'records': [], 'local_calculation': True, 'pool_size': 0}

            # Combine target with pool, forward-fill gaps, diff -> daily returns.
            # Use a synthetic target column so any stale cached column with the
            # same alpha id cannot create duplicate labels.
            target_col = f"__target__{alpha_id}"
            combined = pd.concat([os_pool, target_series.rename(target_col).to_frame()], axis=1).ffill()
            rets = combined.diff()
            if rets.empty:
                return {'max': 0.0, 'records': [], 'local_calculation': True, 'pool_size': os_pool.shape[1]}

            last_date = rets.index.max()
            rets = rets[rets.index > last_date - pd.DateOffset(years=4)]

            if target_col not in rets.columns:
                return {'max': 0.0, 'records': [], 'local_calculation': True, 'pool_size': os_pool.shape[1]}

            target_rets = rets[target_col]
            pool_rets = rets.drop(columns=[target_col], errors='ignore')

            # Self-correlation uses the WHOLE OS pool: Power Pool Alphas are your
            # own submitted alphas too, and excluding them silently shrank the
            # pool -- in configurations where every OS alpha is a PPAC it went to
            # zero columns and reported a fake max of 0.0. Only the explicit
            # 'powerpool' request narrows the pool (to PPACs only).
            ppac_ids = self._load_ppac_ids(instrument_type, region, universe, delay)
            ctype = (correlation_type or 'self').lower()
            full_pool_size = int(pool_rets.shape[1])
            if ctype in ('powerpool', 'ppac', 'ppa'):
                keep_cols = [c for c in pool_rets.columns if c in ppac_ids]
                pool_rets = pool_rets[keep_cols]
            # ctype 'self'/'selfcorr'/'all' -> whole OS pool, no partition
            partitioned_pool_size = int(pool_rets.shape[1])

            if pool_rets.empty:
                return {
                    'max': 0.0,
                    'records': [],
                    'local_calculation': True,
                    'pool_size': partitioned_pool_size,
                    'correlation_type': ctype,
                    'full_os_pool_size': full_pool_size,
                    'ppac_ids_cached': len(ppac_ids),
                }

            # Compute only target-vs-pool correlations instead of the full N x N
            # matrix; this is the hot path when the OS pool is large.
            sc_series = pool_rets.corrwith(target_rets).dropna()
            max_corr = float(sc_series.max()) if not sc_series.empty else 0.0

            records = [
                {'id': oid, 'correlation': float(val)}
                for oid, val in sc_series.nlargest(10).items()
            ]

            self.log(
                f"[SC本地] Alpha {alpha_id}: max_{ctype}_corr={max_corr:.4f} "
                f"(pool={partitioned_pool_size}/{full_pool_size} OS alphas after "
                f"'{ctype}' partition; {len(ppac_ids)} power-pool ids cached)",
                "INFO",
            )
            return {
                'max': max_corr,
                'records': records,
                'local_calculation': True,
                'pool_size': partitioned_pool_size,
                'correlation_type': ctype,
                'full_os_pool_size': full_pool_size,
                'ppac_ids_cached': len(ppac_ids),
            }

        except Exception as e:
            self.log(f"Failed to calculate self-correlation locally: {str(e)}", "ERROR")
            raise

    async def get_mutual_correlation(
        self,
        alpha_ids: List[str],
        threshold: float = 0.5,
        years: int = 4,
    ) -> Dict[str, Any]:
        """Pairwise ("mutual") correlation AMONG a caller-supplied set of alphas.

        Unlike check_self_correlation (target-vs-OS-pool) and check_correlation
        (target-vs-production), this computes the full NxN correlation matrix
        among the given alphas' own daily returns — the check needed when
        selecting a set of alphas that must be mutually decorrelated (e.g. a
        submission basket with a max-pairwise-correlation rule).

        Correlation convention matches the local self-correlation: daily returns
        = diff of cumulative PnL (ffill gaps), restricted to the last ``years``.

        Returns the matrix, the single most-correlated pair, every pair at/above
        ``threshold``, whether all pairs are below it, and a greedy maximal
        subset whose members are all mutually below ``threshold``.
        """
        await self.ensure_authenticated()

        # De-duplicate while preserving order.
        ids = list(dict.fromkeys([a for a in (alpha_ids or []) if a]))
        if len(ids) < 2:
            return {'error': 'Provide at least 2 distinct alpha ids.', 'alpha_ids': ids}

        fetch_sem = asyncio.Semaphore(5)

        async def fetch_one(oid: str):
            async with fetch_sem:
                try:
                    data = await self.get_alpha_pnl(oid)
                    return oid, self._pnl_response_to_series(oid, data)
                except Exception as e:
                    self.log(f"[mutual-corr] Skip {oid}: PnL fetch failed ({e})", "WARNING")
                    return oid, None

        fetched = await asyncio.gather(*[fetch_one(o) for o in ids])
        series = {oid: s for oid, s in fetched if s is not None}
        missing = [oid for oid, s in fetched if s is None]
        if len(series) < 2:
            return {
                'error': 'Fewer than 2 alphas had usable PnL.',
                'missing_pnl': missing,
                'alpha_ids': ids,
            }

        present = [oid for oid in ids if oid in series]
        combined = pd.concat([series[oid].rename(oid).to_frame() for oid in present], axis=1).ffill()
        rets = combined.diff()
        if not rets.empty:
            last_date = rets.index.max()
            rets = rets[rets.index > last_date - pd.DateOffset(years=years)]
        rets = rets.dropna(how='all')
        if rets.shape[0] < 2:
            return {'error': 'Insufficient overlapping PnL history.', 'missing_pnl': missing, 'alpha_ids': ids}

        corr = rets.corr()
        cols = [c for c in present if c in corr.columns]

        def cval(a: str, b: str) -> float:
            try:
                v = float(corr.loc[a, b])
                return v if v == v else 0.0  # NaN -> 0
            except Exception:
                return 0.0

        pairs = []
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                pairs.append((cols[i], cols[j], cval(cols[i], cols[j])))
        pairs.sort(key=lambda x: -abs(x[2]))

        over = [{'a': a, 'b': b, 'correlation': round(c, 4)} for a, b, c in pairs if abs(c) >= threshold]
        max_pair = (
            {'a': pairs[0][0], 'b': pairs[0][1], 'correlation': round(pairs[0][2], 4)}
            if pairs else None
        )

        # Greedy maximal mutually-below-threshold subset: consider nodes in
        # ascending order of average |correlation| (least entangled first), keep
        # a node only if it is < threshold vs every already-kept node. This is a
        # heuristic (max independent set is NP-hard) but gives a good basket.
        if len(cols) > 1:
            avg_abs = {
                o: sum(abs(cval(o, p)) for p in cols if p != o) / (len(cols) - 1)
                for o in cols
            }
        else:
            avg_abs = {cols[0]: 0.0} if cols else {}
        order = sorted(cols, key=lambda o: avg_abs.get(o, 0.0))
        kept: List[str] = []
        for oid in order:
            if all(abs(cval(oid, k)) < threshold for k in kept):
                kept.append(oid)

        matrix = {a: {b: round(cval(a, b), 4) for b in cols} for a in cols}

        self.log(
            f"[mutual-corr] {len(cols)} alphas: max pair "
            f"{max_pair['correlation'] if max_pair else 'n/a'}, "
            f"{len(over)} pair(s) >= {threshold}, max mutually-<{threshold} subset size {len(kept)}",
            "INFO",
        )

        return {
            'alpha_ids': cols,
            'threshold': threshold,
            'years': years,
            'num_points': int(rets.shape[0]),
            'matrix': matrix,
            'max_pair': max_pair,
            'pairs_over_threshold': over,
            'all_below_threshold': len(over) == 0,
            'max_mutually_below_subset': kept,
            'max_mutually_below_subset_size': len(kept),
            'missing_pnl': missing,
            'local_calculation': True,
        }

    async def check_self_correlation(
        self,
        alpha_id: str,
        threshold: float = 0.7,
        correlation_type: str = 'self',
    ) -> Dict[str, Any]:
        """Compute self-correlation locally using the cached OS PnL pool.

        Args:
            alpha_id: Target alpha ID.
            threshold: Max-correlation threshold used for the pass/fail check.
            correlation_type: 'self' (default) or 'all' -> the whole OS pool,
                Power Pool Alphas included; 'powerpool' -> only Power Pool
                Alphas.
        """
        await self.ensure_authenticated()

        correlation_data = await self.get_self_correlation(alpha_id, correlation_type=correlation_type)
        if not isinstance(correlation_data, dict) or not correlation_data:
            return {
                'alpha_id': alpha_id,
                'threshold': threshold,
                'correlation_type': correlation_type,
                'max_correlation': None,
                'passes_check': None,
                'status': 'data_unavailable',
                'message': 'Local self-correlation data is unavailable for this alpha.',
                'correlation_data': correlation_data,
            }

        try:
            max_correlation = float(correlation_data.get('max'))
        except (TypeError, ValueError):
            max_correlation = None

        passes_check = max_correlation < threshold if max_correlation is not None else None

        return {
            'alpha_id': alpha_id,
            'threshold': threshold,
            'correlation_type': correlation_type,
            'max_correlation': max_correlation,
            'passes_check': passes_check,
            'local_calculation': True,
            'correlation_data': correlation_data,
        }

    async def check_correlation(self, alpha_id: str, correlation_type: str = "production", threshold: float = 0.7) -> Dict[str, Any]:
        """ Only where all IS metrics PASS to Check alpha correlation, Check alpha correlation against production alphas, self alphas, power-pool alphas, or combinations.

        correlation_type: 'production' | 'self' | 'powerpool' | 'both'
        ('both' = production + self, unchanged legacy behaviour).

        Rate limit: production AND power-pool correlations share ONE
        cross-process per-account slot that allows a single check to START every
        3 minutes (BRAIN_CORRELATION_MIN_INTERVAL_SECONDS). A check that is
        concurrent with, or too soon after, another one fails fast with
        status='correlation_busy' and a retry_after instead of waiting. The
        ``self`` path is computed locally and is never gated.
        """
        await self.ensure_authenticated()

        try:
            results = {
                'alpha_id': alpha_id,
                'threshold': threshold,
                'correlation_type': correlation_type,
                'checks': {}
            }

            # Determine which correlations to check
            check_types = []
            if correlation_type == "both":
                check_types = ["production", "self"]
            else:
                check_types = [correlation_type]

            all_passed = True

            for check_type in check_types:
                if check_type in ("powerpool", "power-pool", "ppa", "ppac"):
                    check_type = "powerpool"
                if check_type in ("production", "powerpool"):
                    if check_type == "production":
                        correlation_data = await self.get_production_correlation(alpha_id)
                    else:
                        correlation_data = await self.get_power_pool_correlation(alpha_id)

                    # Handle pending/data-not-yet-available case (super alphas, fresh simulations)
                    if correlation_data and correlation_data.get('status') == 'pending':
                        results['checks'][check_type] = {
                            'max_correlation': None,
                            'passes_check': None,
                            'status': 'pending',
                            'message': correlation_data.get('message', ''),
                            'correlation_data': correlation_data,
                        }
                        results['all_passed'] = None
                        results['status'] = 'pending'
                        results['message'] = correlation_data.get('message', '')
                        return results

                    if correlation_data and correlation_data.get('status') == 'correlation_busy':
                        results['checks'][check_type] = {
                            'max_correlation': None,
                            'passes_check': None,
                            'status': 'correlation_busy',
                            'message': correlation_data.get('message', ''),
                            'retry_after': correlation_data.get('retry_after'),
                            'correlation_data': correlation_data,
                        }
                        results['all_passed'] = None
                        results['status'] = 'correlation_busy'
                        results['message'] = correlation_data.get('message', '')
                        results['retry_after'] = correlation_data.get('retry_after')
                        return results

                    # 'production' needs non-empty histogram records to be trustworthy.
                    # 'powerpool' may legitimately have records == [] (no Power Pool
                    # Alphas submitted yet) with a normalized max of 0.0 — that is a
                    # real "correlation 0" answer, not missing data.
                    has_usable_data = (
                        correlation_data
                        and isinstance(correlation_data.get('records'), list)
                        and correlation_data.get('max') is not None
                        and (check_type == "powerpool" or len(correlation_data['records']) > 0)
                    )
                    if has_usable_data:
                        max_correlation = correlation_data['max']
                        passes_check = max_correlation < threshold
                        results['checks'][check_type] = {
                            'max_correlation': max_correlation,
                            'passes_check': passes_check,
                            'correlation_data': correlation_data
                        }
                        if not passes_check:
                            all_passed = False
                            results["all_passed"] = all_passed
                            return results
                    else:
                        # Data returned but has no usable records/max (empty or malformed).
                        # Return None to signal "data unavailable" rather than faking max=0.
                        results['checks'][check_type] = {
                            'max_correlation': None,
                            'passes_check': None,
                            'status': 'data_unavailable',
                            'message': (
                                f'{check_type} correlation data is unavailable for this alpha. '
                                'This may be a newly-created super alpha where the platform '
                                'has not yet computed the correlation. Please retry in a few minutes.'
                            ),
                            'correlation_data': correlation_data,
                        }
                        results['all_passed'] = None
                        results['status'] = 'data_unavailable'
                        return results
                elif check_type == "self":
                    correlation_data = await self.get_self_correlation(alpha_id)
                else:
                    continue
                
                # Analyze correlation data (self-correlation path)
                if correlation_data and correlation_data.get('max') is not None:
                    max_correlation = correlation_data['max']
                    passes_check = max_correlation < threshold
                else:
                    max_correlation = None
                    passes_check = None
                
                results['checks'][check_type] = {
                    'max_correlation': max_correlation,
                    'passes_check': passes_check,
                    'correlation_data': correlation_data
                }
                
                if passes_check is not True:
                    all_passed = False
            
            results['all_passed'] = all_passed
            
            return results
            
        except Exception as e:
            self.log(f"Failed to check correlation: {str(e)}", "ERROR")
            raise

    async def get_submission_check(self, alpha_id: str) -> Dict[str, Any]:
        """Comprehensive pre-submission check."""
        await self.ensure_authenticated()
        
        try:
            # This endpoint might not exist, so we simulate it by calling other functions
            # In a real scenario, this would be a single API call
            
            pnl_data = await self.get_alpha_pnl(alpha_id)
            yearly_stats = await self.get_alpha_yearly_stats(alpha_id)
            correlation = await self.check_correlation(alpha_id)
            
            return {
                "pnl_summary": pnl_data.get("pnlSummary", {}),
                "yearly_stats": yearly_stats,
                "correlation": correlation
            }
        except Exception as e:
            self.log(f"Failed submission check: {str(e)}", "ERROR")
            raise

    async def set_alpha_properties(self, alpha_id: str, name: Optional[str] = None, 
                                   color: Optional[str] = None, tags: Optional[List[str]] = None,
                                   descriptions: str = "None",
                                   selection_description: Optional[str] = None,
                                   combo_description: Optional[str] = None
                                   ) -> Dict[str, Any]:
        """Update alpha properties (name, color, tags, descriptions)."""
        await self.ensure_authenticated()
        
        try:
            # todo: ra_failed_count 是否为0
            payload = {
                "color": color,
                "name": name,
                "tags": tags if tags is not None else [],
                "regular": {"description": descriptions}
            }
            if selection_description is not None:
                payload["selection"] = {"description": selection_description}
            if combo_description is not None:
                payload["combo"] = {"description": combo_description}
            
            response = await self._request('PATCH', f"{self.base_url}/alphas/{alpha_id}", json=payload)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to set alpha properties: {str(e)}", "ERROR")
            raise

    async def get_record_sets(self, alpha_id: str, force_refresh: bool = False) -> Dict[str, Any]:
        """List available record sets for an alpha (a fixed set of five names)."""
        await self.ensure_authenticated()
        try:
            return await self._cached_get(
                f"{self.base_url}/alphas/{alpha_id}/recordsets",
                namespace='recordsets_index', key=alpha_id,
                permanent=True, force_refresh=force_refresh)
        except Exception as e:
            self.log(f"Failed to get record sets: {str(e)}", "ERROR")
            raise

    async def get_record_set_data(self, alpha_id: str, record_set_name: str,
                                  force_refresh: bool = False) -> Dict[str, Any]:
        """Get data from a specific record set.

        Record sets are the simulation's own output and never extend: an alpha
        submitted in 2025-03 still reports PnL ending 2023-12-29 and yearly-stats
        ending 2023, exactly like one submitted today. So they are permanent.
        """
        await self.ensure_authenticated()
        try:
            return await self._cached_get(
                f"{self.base_url}/alphas/{alpha_id}/recordsets/{record_set_name}",
                namespace='recordset', key=f"{alpha_id}:{record_set_name}",
                permanent=True, force_refresh=force_refresh)
        except Exception as e:
            self.log(f"Failed to get record set data: {str(e)}", "ERROR")
            raise

    async def get_user_activities(self, user_id: str, grouping: Optional[str] = None) -> Dict[str, Any]:
        """Get user activity diversity data."""
        await self.ensure_authenticated()
        
        try:
            params = {}
            if grouping:
                params['grouping'] = grouping
            
            response = await self._request('GET', f"{self.base_url}/users/{user_id}/activities", params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get user activities: {str(e)}", "ERROR")
            raise

    async def get_pyramid_multipliers(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Pyramid multipliers. The platform recomputes these on its own schedule
        (not per request), so a short cache removes the repeat reads that
        diversity scoring and dataset recommendation make."""
        await self.ensure_authenticated()
        try:
            return await self._cached_get(
                f"{self.base_url}/users/self/activities/pyramid-multipliers",
                namespace='pyramid_multipliers', key='self',
                redis_ttl=3600, force_refresh=force_refresh)
        except Exception as e:
            self.log(f"Failed to get pyramid multipliers: {str(e)}", "ERROR")
            raise

    async def get_pyramid_alphas(self, start_date: Optional[str] = None,
                               end_date: Optional[str] = None) -> Dict[str, Any]:
        """Get user's current alpha distribution across pyramid categories.
        Defaults to the current quarter if no dates are provided."""
        await self.ensure_authenticated()
        
        try:
            # Default to current quarter boundaries
            if not start_date or not end_date:
                now = datetime.utcnow()
                q_start_month = (now.month - 1) // 3 * 3 + 1
                quarter_start = datetime(now.year, q_start_month, 1)
                if q_start_month + 3 > 12:
                    quarter_end = datetime(now.year + 1, 1, 1)
                else:
                    quarter_end = datetime(now.year, q_start_month + 3, 1)
                if not start_date:
                    start_date = quarter_start.strftime("%Y-%m-%d")
                if not end_date:
                    end_date = quarter_end.strftime("%Y-%m-%d")

            params = {}
            if start_date:
                params["startDate"] = start_date
            if end_date:
                params["endDate"] = end_date

            cache_key = self._generate_cache_key('pyramid_alphas', params)
            cached_data = self._get_cached_data(cache_key)
            if cached_data:
                return {**cached_data, 'from_cache': True}

            try:
                timeout_seconds = max(
                    5,
                    int(os.environ.get("BRAIN_PYRAMID_ALPHAS_TIMEOUT_SECONDS", "15")),
                )
            except Exception:
                timeout_seconds = 15

            response = await self._request(
                'GET',
                f"{self.base_url}/users/self/activities/pyramid-alphas",
                params=params,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            data = response.json() if response.text else {}
            if isinstance(data, dict):
                data['from_cache'] = False
                self._set_cached_data(cache_key, data, ttl=3600)
            return data
        except Exception as e:
            self.log(f"Failed to get pyramid alphas: {str(e)}", "ERROR")
            raise
            
    async def get_user_competitions(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Get list of competitions that the user is participating in."""
        await self.ensure_authenticated()
        
        try:
            if not user_id:
                user_id = await self.get_self_user_id() or 'self'
            
            response = await self._request('GET', f"{self.base_url}/users/{user_id}/competitions")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get user competitions: {str(e)}", "ERROR")
            raise
            
    async def get_competition_details(self, competition_id: str, force_refresh: bool = False) -> Dict[str, Any]:
        """Competition metadata. Dates and rules are fixed once a competition is
        announced, so this is cached for a day rather than re-read per call."""
        await self.ensure_authenticated()
        try:
            return await self._cached_get(
                f"{self.base_url}/competitions/{competition_id}",
                namespace='competition', key=str(competition_id),
                redis_ttl=86400, force_refresh=force_refresh)
        except Exception as e:
            self.log(f"Failed to get competition details: {str(e)}", "ERROR")
            raise
            
    async def get_competition_agreement(self, competition_id: str, force_refresh: bool = False) -> Dict[str, Any]:
        """Competition rules/terms — a fixed legal document, stored permanently."""
        await self.ensure_authenticated()
        try:
            return await self._cached_get(
                f"{self.base_url}/competitions/{competition_id}/agreement",
                namespace='competition_agreement', key=str(competition_id),
                permanent=True, force_refresh=force_refresh)
        except Exception as e:
            self.log(f"Failed to get competition agreement: {str(e)}", "ERROR")
            raise

    async def get_platform_setting_options(self) -> Dict[str, Any]:
        """Get available instrument types, regions, delays, and universes with Redis caching (1 day TTL)."""
        await self.ensure_authenticated()
        
        try:
            # Generate cache key (no parameters needed as this endpoint returns fixed platform settings)
            cache_key = self._generate_cache_key('platform_settings', {})
            
            # Try to get from cache
            cached_data = self._get_cached_data(cache_key)
            if cached_data:
                return {**cached_data, 'from_cache': True}
            
            # Use OPTIONS method on simulations endpoint to get configuration options
            response = await self._request('OPTIONS', f"{self.base_url}/simulations")
            response.raise_for_status()
            
            # Parse the settings structure from the response
            settings_data = response.json()
            settings_options = settings_data['actions']['POST']['settings']['children']
            
            # Extract instrument configuration options
            instrument_type_data = {}
            region_data = {}
            universe_data = {}
            delay_data = {}
            neutralization_data = {}
            
            # Parse each setting type
            for key, setting in settings_options.items():
                if setting['type'] == 'choice':
                    if setting['label'] == 'Instrument type':
                        instrument_type_data = setting['choices']
                    elif setting['label'] == 'Region':
                        region_data = setting['choices']['instrumentType']
                    elif setting['label'] == 'Universe':
                        universe_data = setting['choices']['instrumentType']
                    elif setting['label'] == 'Delay':
                        delay_data = setting['choices']['instrumentType']
                    elif setting['label'] == 'Neutralization':
                        neutralization_data = setting['choices']['instrumentType']
            
            # Build comprehensive instrument options
            data_list = []
            
            for instrument_type in instrument_type_data:
                for region in region_data[instrument_type['value']]:
                    for delay in delay_data[instrument_type['value']]['region'][region['value']]:
                        row = {
                            'InstrumentType': instrument_type['value'],
                            'Region': region['value'],
                            'Delay': delay['value']
                        }
                        row['Universe'] = [
                            item['value'] for item in universe_data[instrument_type['value']]['region'][region['value']]
                        ]
                        row['Neutralization'] = [
                            item['value'] for item in neutralization_data[instrument_type['value']]['region'][region['value']]
                        ]
                        data_list.append(row)
            
            # Return structured data
            result = {
                'instrument_options': data_list,
                'total_combinations': len(data_list),
                'instrument_types': [item['value'] for item in instrument_type_data],
                'regions_by_type': {
                    item['value']: [r['value'] for r in region_data[item['value']]]
                    for item in instrument_type_data
                },
                'from_cache': False
            }
            
            # Cache the data (1 day TTL)
            self._set_cached_data(cache_key, result, ttl=604800)
            
            return result
            
        except Exception as e:
            self.log(f"Failed to get instrument options: {str(e)}", "ERROR")
            raise
            
    async def performance_comparison(self, alpha_id: str, competition: Optional[str] = None,
                                     team_id: Optional[str] = None) -> Dict[str, Any]:
        """Get before-and-after performance comparison data for an alpha.

        BRAIN exposes this resource below the alpha's owning scope.  The old
        ``/alphas/{id}/performance-comparison`` route now returns 404, so keep
        the scope selection explicit instead of passing a team as a query
        parameter to the personal endpoint.

        Scope precedence is competition, team, then the authenticated user.
        """
        await self.ensure_authenticated()

        try:
            if competition:
                owner_scope = f"competitions/{competition}"
            elif team_id:
                owner_scope = f"teams/{team_id}"
            else:
                owner_scope = "users/self"

            url = (
                f"{self.base_url}/{owner_scope}/alphas/{alpha_id}/"
                "before-and-after-performance"
            )

            # The endpoint returns an empty body with a Retry-After header while
            # the comparison is being computed, then JSON once it is ready.
            try:
                payload = await self._request_json_with_retries(
                    'GET', url, op_name="performance_comparison"
                )
                return archive_performance_comparison(
                    alpha_id, owner_scope, payload
                )
            except requests.HTTPError as error:
                response = error.response
                if response is None or response.status_code != 400:
                    raise

                # BRAIN drops this pre-submission report after an alpha enters
                # OS.  Translate only that known case; retain unexpected 400s.
                details_response = await self._request(
                    "GET", f"{self.base_url}/alphas/{alpha_id}"
                )
                details_response.raise_for_status()
                details = details_response.json()
                if details.get("stage") != "OS":
                    raise
                return {
                    "available": False,
                    "reason": "performance_comparison_is_pre_submission_only",
                    "alpha_id": alpha_id,
                    "stage": "OS",
                    "status": details.get("status"),
                    "dateSubmitted": details.get("dateSubmitted"),
                }
        except Exception as e:
            self.log(f"Failed to get performance comparison: {str(e)}", "ERROR")
            raise
            
    # --- Helper function for data flattening ---
    
    async def expand_nested_data(self, data: List[Dict[str, Any]], preserve_original: bool = True) -> List[Dict[str, Any]]:
        """Flatten complex nested data structures into tabular format."""
        try:
            df = pd.json_normalize(data, sep='_')
            if preserve_original:
                original_df = pd.DataFrame(data)
                df = pd.concat([original_df, df], axis=1)
                df = df.loc[:,~df.columns.duplicated()]
            return df.to_dict(orient='records')
        except Exception as e:
            self.log(f"Failed to expand nested data: {str(e)}", "ERROR")
            raise
            
    # --- New documentation endpoint ---
    
    async def get_documentation_page(self, page_id: str, force_refresh: bool = False) -> Dict[str, Any]:
        """Retrieve a documentation page. Tutorial content is static, so it is
        stored permanently and refreshed only on demand."""
        await self.ensure_authenticated()
        try:
            return await self._cached_get(
                f"{self.base_url}/tutorial-pages/{page_id}",
                namespace='tutorial_pages', key=str(page_id),
                permanent=True, force_refresh=force_refresh)
        except Exception as e:
            self.log(f"Failed to get documentation page: {str(e)}", "ERROR")
            raise

brain_client = BrainApiClient()

# --- Configuration Management ---

def _resolve_config_path(for_write: bool = False) -> str:
    """
    Resolve the configuration file path.
    
    Checks for a file specified by the MCP_CONFIG_FILE environment variable,
    then falls back to ~/.brain_mcp_config.json. If for_write is True,
    it ensures the directory exists.
    """
    if 'MCP_CONFIG_FILE' in os.environ:
        return os.environ['MCP_CONFIG_FILE']
    
    config_path = Path(__file__).parent / "user_config.json"
    
    if for_write:
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
        except (IOError, OSError) as e:
            logger.warning(f"Could not create config directory {config_path.parent}: {e}")
            # Fallback to a temporary file if home is not writable
            import tempfile
            return tempfile.NamedTemporaryFile(delete=False).name
            
    return str(config_path)

def _load_dotenv_into_environ():
    """Load .env into environment using python-dotenv if available; fallback to simple parser."""
    try:
        from dotenv import load_dotenv, find_dotenv
        env_path = find_dotenv(usecwd=True)
        if env_path:
            load_dotenv(env_path, override=False)
        else:
            # Try repo root relative to this file
            candidate = Path(__file__).parent / ".env"
            if candidate.exists():
                load_dotenv(candidate, override=False)
    except Exception:
        # Fallback: very simple .env parser (KEY=VALUE, no export, ignores quotes)
        try:
            candidate = Path(__file__).parent / ".env"
            if candidate.exists():
                for line in candidate.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' not in line:
                        continue
                    k, v = line.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    os.environ.setdefault(k, v)
        except Exception:
            pass

def load_config() -> Dict[str, Any]:
    """Load configuration from file and overlay environment variables (from .env if present)."""
    config: Dict[str, Any] = {}
    config_file = _resolve_config_path()
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                config = json.load(f) or {}
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Error loading config file {config_file}: {e}")

    # Load .env into environment (no override of already-set env)
    _load_dotenv_into_environ()

    # Overlay credentials from env if available
    env_email = os.getenv("CREDENTIALS_EMAIL")
    env_password = os.getenv("CREDENTIALS_PASSWORD")
    if env_email or env_password:
        creds = dict(config.get("credentials", {}))
        if env_email:
            creds["email"] = env_email
        if env_password:
            creds["password"] = env_password
        config["credentials"] = creds

    return config

def save_config(config: Dict[str, Any]):
    """Save configuration to file using the resolved config path.
    
    This function now uses the write-enabled path resolver to handle
    cases where the default home directory is not writable.
    """
    config_file = _resolve_config_path(for_write=True)
    try:
        with open(config_file, 'w') as f:
            json.dump(config, f, indent=2)
    except IOError as e:
        logger.error(f"Error saving config file to {config_file}: {e}")

# --- MCP Tool Definitions ---

_MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
try:
    _MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
except Exception:
    _MCP_PORT = 8000
_MCP_STREAMABLE_HTTP_PATH = os.getenv("MCP_STREAMABLE_HTTP_PATH", "/mcp")

mcp = FastMCP(
    "brain-platform-mcp",
    "A server for interacting with the WorldQuant BRAIN platform",
    host=_MCP_HOST,
    port=_MCP_PORT,
    streamable_http_path=_MCP_STREAMABLE_HTTP_PATH,
)

# Add health check endpoint for container monitoring
from mcp.server.fastmcp import Context
from starlette.responses import JSONResponse

@mcp.custom_route('/health', methods=['GET'])
async def health_check(context: Context):
    """Health check endpoint for Docker container monitoring."""
    return JSONResponse({
        "status": "healthy",
        "service": "brain-platform-mcp",
        "timestamp": datetime.utcnow().isoformat(),
        "redis_connected": brain_client.redis_client is not None
    })

# ============================================================================
# Response-slimming helpers
# ----------------------------------------------------------------------------
# Keep MCP tool outputs compact so long agent sessions (and any hook /
# transcript evaluators that re-read the conversation) don't blow the context
# window. These ONLY strip noise: fixed help strings, null sub-objects,
# redundant repeated fields, oversized free text, and full daily PnL series.
# The essential ids / metrics / checks / pyramid info are preserved (often in a
# clearer shape). Every helper is defensive: on an unexpected shape or an
# {"error": ...} payload it returns the input unchanged.
# ============================================================================

_RA_2Y_NAMES = ("LOW_2Y_SHARPE", "IS_LADDER_SHARPE")

# WebDataScope-0.10.20/src/scripts/background.js :: getAlphaCheckStates — canonical RA / PPA check names.
_RA_CHECK_NAMES = frozenset([
    "HIGH_TURNOVER", "LOW_TURNOVER", "LOW_FITNESS", "LOW_RETURNS", "LOW_SHARPE",
    "LOW_GLB_AMER_SHARPE", "LOW_GLB_APAC_SHARPE", "LOW_GLB_EMEA_SHARPE", "LOW_ASI_JPN_SHARPE",
    "IS_LADDER_SHARPE",  # ATOM-exempt but still counted in the RA gate
    "LOW_2Y_SHARPE", "LOW_SUB_UNIVERSE_SHARPE", "LOW_ROBUST_UNIVERSE_SHARPE",
    "LOW_AFTER_COST_ILLIQUID_UNIVERSE_SHARPE", "LOW_INVESTABILITY_CONSTRAINED_SHARPE",
    "LOW_ROBUST_UNIVERSE_RETURNS", "CONCENTRATED_WEIGHT",
])
_PPA_CHECK_NAMES = frozenset([
    "LOW_TURNOVER", "HIGH_TURNOVER", "LOW_SUB_UNIVERSE_SHARPE", "LOW_ROBUST_UNIVERSE_SHARPE",
    "LOW_ROBUST_UNIVERSE_SHARPE.WITH_RATIO", "LOW_ROBUST_UNIVERSE_RETURNS",
    "LOW_INVESTABILITY_CONSTRAINED_SHARPE",
])


def _ra_bad(result):
    # WebDataScope rule: a check counts as failing the RA/PPA gate iff result != "PASS" and result != "PENDING"
    return result != "PASS" and result != "PENDING"


def _truncate(s, n=160):
    if not isinstance(s, str):
        return s
    s2 = s.strip()
    return s2 if len(s2) <= n else s2[:n].rstrip() + "…"


def _unwrap_result(obj):
    """brain_client methods usually return {"result": <payload>}; some return the payload directly."""
    if isinstance(obj, dict) and list(obj.keys()) == ["result"]:
        return obj["result"], True
    return obj, False


def _rewrap(payload, was_wrapped):
    return {"result": payload} if was_wrapped else payload


def _is_error(payload):
    return isinstance(payload, dict) and "error" in payload


def _slim_checks(checks):
    """Compress an is.checks[] array into fail/warning/pass/pending buckets + pyramid info + headline values
    + precomputed RA/PPA failure counts (WebDataScope getAlphaCheckStates). Returns (buckets, pyramids, extracted, ra)."""
    out = {"fail": [], "warning": [], "pass": [], "pending": []}
    pyramids = None
    extracted = {}
    rename = {"LOW_ROBUST_UNIVERSE_SHARPE": "robust_universe_sharpe",
              "LOW_SUB_UNIVERSE_SHARPE": "sub_universe_sharpe"}
    failed_ra = 0
    failed_ppa = 0
    ra_failed_names = []
    ppa_failed_names = []
    for c in checks or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        res = c.get("result")
        val = c.get("value")
        if name == "MATCHES_PYRAMID":
            pyramids = {"effective": c.get("effective"),
                        "list": [{"name": p.get("name"), "multiplier": p.get("multiplier")}
                                 for p in (c.get("pyramids") or []) if isinstance(p, dict)]}
        if name in rename and val is not None:
            extracted[rename[name]] = val
        if name in _RA_2Y_NAMES and val is not None:
            extracted["two_year_sharpe"] = val
            if c.get("year") is not None:
                extracted["two_year_ladder_window"] = c.get("year")
        # --- RA / PPA failure counting (verbatim port of background.js getAlphaCheckStates) ---
        if name in _RA_CHECK_NAMES and _ra_bad(res):
            failed_ra += 1
            ra_failed_names.append(name)
        if (name in _PPA_CHECK_NAMES and _ra_bad(res)) or (name == "LOW_SHARPE" and isinstance(val, (int, float)) and val < 1):
            failed_ppa += 1
            ppa_failed_names.append(name)
        # --- buckets ---
        if res == "FAIL":
            out["fail"].append({k: c.get(k) for k in ("name", "value", "limit", "year", "message", "date")
                                if c.get(k) is not None})
        elif res == "WARNING":
            d = {k: c.get(k) for k in ("name", "value", "limit", "year", "message") if c.get(k) is not None}
            out["warning"].append(d if d else {"name": name})
        elif res == "PENDING":
            out["pending"].append(name)
        elif res in (None, "PASS", "OK"):
            out["pass"].append(name)
        else:
            out["pass"].append(f"{name}:{res}")
    ra = {"failed_ra_count": failed_ra, "failed_ppa_count": failed_ppa,
          "ra_failed": failed_ra > 0, "ppa_failed": failed_ppa > 0}
    if ra_failed_names:
        ra["ra_failed_checks"] = ra_failed_names
    if ppa_failed_names:
        ra["ppa_failed_checks"] = ppa_failed_names
    if pyramids and pyramids.get("list"):
        # WQPPYS: the pyramid leaf names joined, e.g. "sentiment/analyst"
        ra["pyramid_short"] = "/".join((p.get("name") or "").split("/")[-1].lower()
                                       for p in pyramids["list"] if p.get("name"))
    return out, pyramids, extracted, ra


def _slim_alpha(a):
    """Reduce a full alpha object to id / code / settings / key-metrics / checks / pyramids."""
    if not isinstance(a, dict):
        return a
    isd = a.get("is") or {}
    inv = isd.get("investabilityConstrained") or {}
    rn = isd.get("riskNeutralized") or {}
    checks, pyramids, extracted, ra = _slim_checks(isd.get("checks"))
    metrics = {k: isd.get(k) for k in ("sharpe", "fitness", "turnover", "returns", "drawdown",
                                       "margin", "longCount", "shortCount", "pnl", "bookSize", "startDate",
                                       "sharpe_se", "sharpe_t_stat", "selfCorrelation", "prodCorrelation")
               if isd.get(k) is not None}
    # also keep any other small scalar metric the platform may add later (excludes the big sub-dicts/checks)
    for k, v in isd.items():
        if k not in metrics and k not in ("checks", "investabilityConstrained", "riskNeutralized") and isinstance(v, (int, float)):
            metrics[k] = v
    metrics.update(extracted)
    if inv.get("sharpe") is not None:
        metrics["investability_sharpe"] = inv.get("sharpe")
        if inv.get("fitness") is not None:
            metrics["investability_fitness"] = inv.get("fitness")
    if rn.get("sharpe") is not None:
        metrics["risk_neutralized_sharpe"] = rn.get("sharpe")
    reg = a.get("regular")
    code = reg.get("code") if isinstance(reg, dict) else reg
    out = {
        "id": a.get("id"),
        "code": code,
        "status": a.get("status"),
        "stage": a.get("stage"),
        "dateSubmitted": a.get("dateSubmitted"),
        "settings": a.get("settings"),
        "metrics": metrics or None,
        "ra": ra,                 # precomputed Failed RA / Failed PPA (WebDataScope getAlphaCheckStates) — read this instead of recounting checks
        "checks": checks,
        "pyramids": pyramids,
    }
    for k in ("name", "color", "tags"):
        v = a.get(k)
        if v not in (None, "", []):
            out[k] = v
    for k in ("osmosisPoints",):
        v = a.get(k)
        if v is not None:
            out[k] = v
    return {k: v for k, v in out.items() if v is not None}


def _slim_alpha_response(obj):
    payload, w = _unwrap_result(obj)
    if _is_error(payload) or not isinstance(payload, dict):
        return obj
    return _rewrap(_slim_alpha(payload), w)


def _slim_alpha_list(obj):
    payload, w = _unwrap_result(obj)
    if not isinstance(payload, dict) or "results" not in payload:
        return obj
    out = {k: v for k, v in payload.items() if k != "results"}
    out["results"] = [_slim_alpha(a) if isinstance(a, dict) else a for a in payload.get("results", [])]
    return _rewrap(out, w)


def _slim_multisim(obj):
    payload, w = _unwrap_result(obj)
    if not isinstance(payload, dict) or "alpha_results" not in payload:
        return obj
    new_results = []
    for r in payload.get("alpha_results", []):
        if isinstance(r, dict) and isinstance(r.get("details"), dict):
            d = r["details"]
            if list(d.keys()) == ["result"]:
                d = d["result"]
            slim = _slim_alpha(d)
            new_results.append({"alpha_id": r.get("alpha_id"), "location": r.get("location"), **slim})
        else:
            new_results.append(r)
    out = {k: payload.get(k) for k in ("success", "message", "total_requested", "total_created",
                                       "multisimulation_id") if k in payload}
    out["alpha_results"] = new_results
    return _rewrap(out, w)


def _filter_by_date(payload, field: str, since: Optional[str]):
    """Keep rows whose ``field`` is on/after ``since`` (YYYY-MM-DD).

    Applied locally: the whole catalogue already sits in the permanent store, so
    "what changed since X" costs nothing rather than another paged sweep.
    """
    if not since or not isinstance(payload, dict) or 'results' not in payload:
        return payload
    cutoff = str(since)[:10]
    kept = [r for r in payload.get('results') or []
            if isinstance(r, dict) and str(r.get(field) or '')[:10] >= cutoff]
    out = dict(payload)
    out['results'] = kept
    out['count'] = len(kept)
    out['filtered_since'] = {field: cutoff}
    return out


def _facets(rows, spec, top=12):
    """Counts per value for a few columns, so a caller can narrow before paging.

    A catalogue query can match ten thousand rows; returning them all is ~500k
    tokens, which no model can read. Facets let the model see the shape of the
    match and re-query precisely instead of guessing search terms.
    """
    out = {}
    for name, getter in spec.items():
        counter = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            key = getter(r)
            if key is None or key == "":
                continue
            counter[key] = counter.get(key, 0) + 1
        ordered = sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))
        out[name] = dict(ordered[:top])
        if len(ordered) > top:
            out[name]["…"] = f"+{len(ordered) - top} more"
    return out


def _usercount_bucket(f):
    n = f.get("userCount")
    if not isinstance(n, (int, float)):
        return None
    if n == 0:
        return "0 (uncrowded)"
    if n <= 10:
        return "1-10"
    if n <= 100:
        return "11-100"
    return ">100"


_DATAFIELD_SORTS = {
    "userCount": lambda f: -(f.get("userCount") or 0),
    "alphaCount": lambda f: -(f.get("alphaCount") or 0),
    "coverage": lambda f: -(f.get("coverage") or 0),
    "dateCreated": lambda f: str(f.get("dateCreated") or ""),
    "-dateCreated": lambda f: _neg_date(f.get("dateCreated")),
    "id": lambda f: str(f.get("id") or ""),
}


def _neg_date(value):
    """Sort key that puts the newest date first."""
    return tuple(-int(p) for p in str(value or "0-0-0").split("-")[:3] if p.isdigit()) or (0,)


def _slim_datafields(obj, limit: int = 50, offset: int = 0, sort: str = "userCount"):
    payload, w = _unwrap_result(obj)
    if not isinstance(payload, dict) or "results" not in payload:
        return obj
    raw = [f for f in payload.get("results", []) if isinstance(f, dict)]
    total = len(raw)

    key = _DATAFIELD_SORTS.get(sort) or _DATAFIELD_SORTS["userCount"]
    try:
        raw.sort(key=key)
    except Exception:
        pass

    window = raw[offset:offset + max(1, limit)]
    fields = [{"id": f.get("id"), "type": f.get("type"), "coverage": f.get("coverage"),
               "userCount": f.get("userCount"), "alphaCount": f.get("alphaCount"),
               "dataset": (f.get("dataset") or {}).get("id") if isinstance(f.get("dataset"), dict) else f.get("dataset"),
               # dateCreated is how you tell that a field is newly published; it
               # was previously dropped here, so callers could not see freshness.
               "dateCreated": f.get("dateCreated"),
               "description": _truncate(f.get("description"), 160)}
              for f in window]

    out = {
        "count": total,
        "returned": len(fields),
        "offset": offset,
        "sort": sort,
        "results": fields,
        "facets": _facets(raw, {
            "dataset": lambda f: (f.get("dataset") or {}).get("id") if isinstance(f.get("dataset"), dict) else None,
            "category": lambda f: (f.get("category") or {}).get("id") if isinstance(f.get("category"), dict) else None,
            "type": lambda f: f.get("type"),
            "dateCreated": lambda f: f.get("dateCreated"),
            # How many fields nobody is using yet. The default sort buries these
            # at the bottom, so surface the size of that tail explicitly.
            "userCount": _usercount_bucket,
        }),
    }
    if total > offset + len(fields):
        out["next_offset"] = offset + len(fields)
        out["note"] = (f"Showing {len(fields)} of {total} matching fields (sorted by {sort}). "
                       "Narrow with search/dataset_id/data_type/since using the facet counts above, "
                       "or page with offset.")
    # Completeness signals must survive slimming: a truncated catalogue that
    # looks whole is exactly how 89% of the fields stayed invisible.
    for k in ("sharpe_filter_applied", "sharpe_filter_removed", "capped", "warning",
              "coverage", "declared_total", "declared_count", "complete",
              "incomplete_datasets", "dataset_id", "filtered_since",
              "fetched_rows", "fields_in_two_datasets", "metrics_from_universe", "note",
              "search_mode"):
        if k in payload:
            out[k] = payload[k]
    return _rewrap(out, w)


_DATASET_SORTS = {
    "valueScore": lambda d: -(d.get("valueScore") or 0),
    "userCount": lambda d: -(d.get("userCount") or 0),
    "alphaCount": lambda d: -(d.get("alphaCount") or 0),
    "fieldCount": lambda d: -(d.get("fieldCount") or 0),
    "pyramidMultiplier": lambda d: -(d.get("pyramidMultiplier") or 0),
    "-dateUpdated": lambda d: _neg_date(d.get("dateUpdated")),
    "dateUpdated": lambda d: str(d.get("dateUpdated") or ""),
    "id": lambda d: str(d.get("id") or ""),
}


def _slim_datasets(obj, limit: int = 40, offset: int = 0, sort: str = "valueScore"):
    payload, w = _unwrap_result(obj)
    if not isinstance(payload, dict) or "results" not in payload:
        return obj
    raw = [d for d in payload.get("results", []) if isinstance(d, dict)]
    total = len(raw)

    key = _DATASET_SORTS.get(sort) or _DATASET_SORTS["valueScore"]
    try:
        raw.sort(key=key)
    except Exception:
        pass

    window = raw[offset:offset + max(1, limit)]
    ds = []
    for d in window:
        cat = d.get("category")
        ds.append({"id": d.get("id"), "name": d.get("name"),
                   "category": cat.get("id") if isinstance(cat, dict) else cat,
                   "coverage": d.get("coverage"), "fieldCount": d.get("fieldCount"),
                   "userCount": d.get("userCount"), "alphaCount": d.get("alphaCount"),
                   "valueScore": d.get("valueScore"), "pyramidMultiplier": d.get("pyramidMultiplier"),
                   # dateUpdated is the dataset-level "new data landed" signal.
                   "dateUpdated": d.get("dateUpdated"),
                   "description": _truncate(d.get("description"), 200)})

    out = {
        "count": total,
        "returned": len(ds),
        "offset": offset,
        "sort": sort,
        "results": ds,
        "facets": _facets(raw, {
            "category": lambda d: (d.get("category") or {}).get("id") if isinstance(d.get("category"), dict) else None,
            "subcategory": lambda d: (d.get("subcategory") or {}).get("id") if isinstance(d.get("subcategory"), dict) else None,
            "dateUpdated": lambda d: d.get("dateUpdated"),
        }),
    }
    if total > offset + len(ds):
        out["next_offset"] = offset + len(ds)
        out["note"] = (f"Showing {len(ds)} of {total} datasets (sorted by {sort}). "
                       "Narrow with category/search/since, or page with offset.")
    return _rewrap(out, w)


def _records_to_dicts(payload):
    schema = payload.get("schema") or {}
    props = [p.get("name") for p in (schema.get("properties") or []) if isinstance(p, dict)]
    recs = payload.get("records") or []
    if props and recs and isinstance(recs[0], list):
        return [dict(zip(props, r)) for r in recs]
    return recs


def _slim_yearly(obj):
    payload, w = _unwrap_result(obj)
    if not isinstance(payload, dict) or "records" not in payload:
        return obj
    return _rewrap({"records": _records_to_dicts(payload)}, w)


def _slim_pnl(obj, max_rows=160):
    payload, w = _unwrap_result(obj)
    if not isinstance(payload, dict) or "records" not in payload:
        return obj
    schema = payload.get("schema") or {}
    props = [p.get("name") for p in (schema.get("properties") or []) if isinstance(p, dict)]
    recs = payload.get("records") or []
    n = len(recs)
    kept = recs
    if n > max_rows:
        stride = max(1, n // max_rows)
        kept = recs[::stride]
        if kept and recs and kept[-1] is not recs[-1]:
            kept = kept + [recs[-1]]
    out = {"properties": props, "records": kept, "num_records_original": n,
           "downsampled": len(kept) != n}
    return _rewrap(out, w)


def _slim_correlation_block(b):
    if not isinstance(b, dict):
        return b
    out = {}
    for k in ("max_correlation", "passes_check"):
        if k in b:
            out[k] = b[k]
    # Keep the busy/pending/unavailable signal — without it a locked-out call is
    # indistinguishable from "no data".
    for k in ("status", "message", "retry_after"):
        if b.get(k) is not None:
            out[k] = b[k]
    cd = b.get("correlation_data") or {}
    recs = cd.get("records")
    if isinstance(recs, list) and recs and isinstance(recs[0], list) and len(recs[0]) >= 3:
        out["histogram_nonzero"] = [{"range": [r[0], r[1]], "n": r[2]} for r in recs if len(r) >= 3 and r[2]]
        for k in ("max", "min"):
            if cd.get(k) is not None:
                out[k] = cd.get(k)
    elif isinstance(recs, list) and recs and isinstance(recs[0], dict):
        out["top_correlated"] = recs[:5]
    # pool_size is kept unconditionally: an empty pool yields records=[] and a
    # max of 0.0, which is otherwise indistinguishable from a genuine low
    # correlation.
    if cd.get("pool_size") is not None:
        out["pool_size"] = cd.get("pool_size")
    # Surface the pool metadata (local self-correlation).
    for k in ("correlation_type", "full_os_pool_size", "ppac_ids_cached"):
        if cd.get(k) is not None:
            out[k] = cd.get(k)
    return out


def _slim_check_correlation(obj):
    payload, w = _unwrap_result(obj)
    if _is_error(payload) or not isinstance(payload, dict):
        return obj
    # check_self_correlation top-level shape: {alpha_id, threshold, max_correlation, passes_check, correlation_data, ...}
    if "max_correlation" in payload and "checks" not in payload:
        out = {k: payload.get(k) for k in ("alpha_id", "threshold", "correlation_type", "passes_check", "local_calculation")
               if k in payload}
        out.update(_slim_correlation_block(payload))
        return _rewrap(out, w)
    # check_correlation shape: {alpha_id, threshold, correlation_type, checks: {production:{...}, self:{...}}, all_passed}
    out = {k: payload.get(k) for k in ("alpha_id", "threshold", "correlation_type", "status", "message", "retry_after")
           if payload.get(k) is not None}
    checks = payload.get("checks")
    if isinstance(checks, dict):
        out["checks"] = {k: _slim_correlation_block(v) for k, v in checks.items()}
    if "all_passed" in payload:
        out["all_passed"] = payload["all_passed"]
    return _rewrap(out, w)


def _slim_pyramids(obj, kind):
    """kind: 'alphas' -> alphaCount, 'multipliers' -> multiplier. Reshape list to {region: {Dn: {cat: val}}}."""
    payload, w = _unwrap_result(obj)
    if not isinstance(payload, dict) or "pyramids" not in payload:
        return obj
    val_key = "alphaCount" if kind == "alphas" else "multiplier"
    nested = {}
    for p in payload.get("pyramids", []):
        if not isinstance(p, dict):
            continue
        cat = p.get("category")
        cat_id = cat.get("id") if isinstance(cat, dict) else cat
        nested.setdefault(p.get("region"), {}).setdefault(f"D{p.get('delay')}", {})[cat_id] = p.get(val_key)
    return _rewrap({"pyramids": nested}, w)


def _slim_text_lookup(obj, fields=("description", "content"), n=4000):
    """Recursively truncate big free-text / raw fields in nested responses (operators, docs, lookINTO, ...)."""
    trunc_keys = set(fields) | {"raw"}
    def fix(o):
        if isinstance(o, dict):
            r = {}
            for k, v in o.items():
                if k in trunc_keys and isinstance(v, str):
                    r[k] = _truncate(v, n)
                else:
                    r[k] = fix(v)
            return r
        if isinstance(o, list):
            return [fix(x) for x in o]
        return o
    return fix(obj)


@mcp.tool()
async def authenticate() -> Dict[str, Any]:
    """
    Authenticate with WorldQuant BRAIN platform.
    
    This is the first step in any BRAIN workflow. You must authenticate before using any other tools.
    
    Args:
        None
    Returns:
        Authentication result with user info and permissions
    """
    try:
        # Load config to get credentials if not provided
        config = load_config()
        credentials = config.get("credentials", {})
        email = credentials.get("email")
        password = credentials.get("password")
        
        auth_result = await brain_client.authenticate(email, password)
        
        # # Save successful credentials
        # if auth_result.get('status') == 'authenticated':
        #     if 'credentials' not in config:
        #         config['credentials'] = {}
        #     config['credentials']['email'] = email
        #     config['credentials']['password'] = password
        #     save_config(config)
            
        return auth_result
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def authenticate_brainlabs() -> Dict[str, Any]:
    """
    Sign in to BRAIN Labs and return the live AWS WorkSpaces deepLink session URL.

    BRAIN Labs is delivered as an AWS WorkSpaces Web pixel-stream, so it cannot be
    code-driven headlessly; this tool performs the two-step sign-in (platform +
    Labs password) via Playwright and hands back the WorkSpaces URL to open, plus
    the decoded internal labs URL/token. Serialized through a single-concurrency
    lock (LABS_MAX_CONCURRENCY, default 1) because a Labs account has exactly one
    interactive session.

    Returns:
        {status, workspaces_url, labs_url, token, note} or {error}.
    """
    try:
        config = load_config()
        credentials = config.get("credentials", {})
        email = credentials.get("email")
        password = credentials.get("password")
        if not email or not password:
            return {"error": "No BRAIN credentials configured (CREDENTIALS_EMAIL / CREDENTIALS_PASSWORD)."}
        return await labs_client.open_labs_session(email, password)
    except Exception as e:
        return {"error": f"BRAIN Labs sign-in failed: {str(e)}"}

@mcp.tool()
async def emit_labs_script(
    dataset_id: str,
    fields: List[str],
    region: str = "USA",
    universe: str = "TOP3000",
    delay: int = 1,
    labs_output: str = "/tmp/labs_data_analysis_result.json",
) -> Dict[str, Any]:
    """
    Generate the pasteable BRAIN Labs data-analysis script for a dataset's MATRIX fields.

    Raw panel data is only available inside Labs (`from brain import Brain`), so the
    emitted script must be run in the Labs JupyterLab. Requires the LABS_AGENT_SCRIPT
    env var to point at labs_data_analysis_agent.py. Serialized by the Labs lock.

    Args:
        dataset_id: Dataset id to analyze.
        fields: MATRIX field ids (at most two for downstream Python alpha design).
        region/universe/delay: Simulation target context.
        labs_output: Path the in-Labs script writes its JSON result to.
    """
    try:
        return await labs_client.emit_labs_script(
            dataset_id=dataset_id,
            fields=fields,
            region=region,
            universe=universe,
            delay=delay,
            labs_output=labs_output,
        )
    except Exception as e:
        return {"error": f"emit_labs_script failed: {str(e)}"}

@mcp.tool()
async def ingest_labs_result(result_json: str) -> Dict[str, Any]:
    """
    Parse a BRAIN Labs data-analysis result (a JSON string or a file path) and return it.

    Use after running the emit_labs_script output inside Labs. Serialized by the Labs lock.
    """
    try:
        return await labs_client.ingest_labs_result(result_json)
    except Exception as e:
        return {"error": f"ingest_labs_result failed: {str(e)}"}

@mcp.tool()
async def manage_config(action: str = "get", settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Manage configuration settings - get or update configuration.
    
    Args:
        action: Action to perform ("get" to retrieve config, "set" to update config)
        settings: Configuration settings to update (required when action="set")
    
    Returns:
        Current or updated configuration including authentication status
    """
    config = load_config()
    
    if action == "set" and settings:
        config.update(settings)
        save_config(config)
        
    is_authed = await brain_client.is_authenticated()
    config['isAuthenticated'] = is_authed
    
    # Mask password for security
    if 'password' in config:
        config['password'] = '********'
        
    return config

# --- Simulation Tools ---

@mcp.tool()
async def create_simulation(
    type: str = "REGULAR",
    region: str = "USA",
    universe: str = "TOP3000",
    delay: int = 1,
    decay: int = 4,
    neutralization: str = "SUBINDUSTRY",
    truncation: float = 0.08,
    test_period: str = "P0Y0M",
    language: str = "FASTEXPR",
    unit_handling: str = "VERIFY",
    nan_handling: str = "ON",
    lookback: Optional[int] = None,
    alpha_expression: Optional[str] = None,
    combo: Optional[str] = None,
    selection: Optional[str] = None,
    pasteurization: str = "ON",
    max_trade: str = "OFF",
    selection_handling: str = "POSITIVE",
    selection_limit: int = 1000,
    component_activation: str = "IS",
    reuse_existing: bool = True,
) -> Dict[str, Any]:
    """
    Create a new simulation on BRAIN platform.

    This tool creates and starts a simulation with your alpha code. Use this after you have your alpha formula ready.

    Every completed backtest is recorded locally. If this exact expression and
    settings were simulated before, the recorded alpha is returned immediately
    (flagged `from_local_ledger`) instead of spending several minutes and a
    simulation slot re-running it. Pass `reuse_existing=false` to force a fresh
    backtest — worth doing after a monthly data release, since new datafields
    can change a result (see whats_new_in_data).
    if field type=VECTOR should deal with vec_ suffer vec_*(FIELD)
    Args:
        type: Simulation type ("REGULAR" or "SUPER")
        region: Market region (e.g., "USA")
        universe: Universe of stocks (e.g., "TOP3000")
        delay: Data delay (0 or 1)
        decay: Decay value for the simulation
        neutralization: Neutralization method
        truncation: Truncation value
        test_period: Test period (e.g., "P0Y0M" for 1 year 6 months)
        language: Expression language ("FASTEXPR" or "PYTHON")
        unit_handling: Unit handling method. Used for FASTEXPR simulations.
        nan_handling: NaN handling method
        lookback: Historical lookback window. Only used for PYTHON simulations; defaults to 256 for PYTHON.
        alpha_expression: Alpha expression code (for REGULAR type)
        combo: Combo code (for SUPER type)
        selection: Selection code (for SUPER type). For USA SUPER simulations,
            this must include (prod_correlation > 0)
    
    Returns:
        Simulation creation result with ID and location
    """
    instrument_type = "EQUITY"
    visualization = False
    try:
        normalized_language = language.upper()
        settings_kwargs = {
            "instrumentType": instrument_type,
            "region": region,
            "universe": universe,
            "delay": delay,
            "decay": decay,
            "neutralization": neutralization,
            "truncation": truncation,
            "testPeriod": test_period,
            "language": normalized_language,
            "visualization": visualization,
            "pasteurization": pasteurization,
            "maxTrade": max_trade,
            "selectionHandling": selection_handling,
            "selectionLimit": selection_limit,
            "componentActivation": component_activation,
        }

        if normalized_language == "PYTHON":
            settings_kwargs["lookback"] = 256 if lookback is None else lookback
            settings_kwargs["unitHandling"] = None
            settings_kwargs["nanHandling"] = None
        else:
            settings_kwargs["unitHandling"] = unit_handling
            settings_kwargs["nanHandling"] = nan_handling

        settings = SimulationSettings(
            **settings_kwargs
        )
        
        sim_data = SimulationData(
            type=type,
            settings=settings,
            regular=alpha_expression,
            combo=combo,
            selection=selection
        )
        
        return _slim_alpha_response(
            await brain_client.create_simulation(sim_data, reuse_existing=reuse_existing))
    except Exception as e:
        extra_info = ""
        error_msg = str(e)
        if error_msg and "does not support event inputs" in error_msg:
            extra_info = "If fields is vector type  should use vec_* operator with event input"
            return {"error": f"An unexpected error occurred: {str(e)}. {extra_info}"}
        return {"error": f"An unexpected error occurred: {str(e)}"}

# --- Alpha and Data Retrieval Tools ---

@mcp.tool()
async def get_alpha_details(alpha_id: str) -> Dict[str, Any]:
    """
    Get detailed information about an alpha.
    
    Args:
        alpha_id: The ID of the alpha to retrieve
    
    Returns:
        Detailed alpha information
    """
    try:
        return _slim_alpha_response(await brain_client.get_alpha_details(alpha_id))
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_datasets(
    category: Optional[str] = None,
    region: str = "USA",
    delay: int = 1,
    universe: str = "TOP3000",
    theme: str = "false",
    search: Optional[str] = None,
    since: Optional[str] = None,
    sort: str = "valueScore",
    limit: int = 40,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Get available datasets for research.

    Returns a page of datasets plus facet counts (category, subcategory,
    dateUpdated) describing the whole match, so you can narrow precisely instead
    of pulling the full catalogue — all 345 USA/TOP3000 datasets is ~36k tokens.

    Args:
        category: Type of datasets (e.g., "news","sentiment","option")
        region: Market region (e.g., "USA")
        delay: Data delay (0 or 1)
        universe: Universe of stocks (e.g., "TOP3000")
        theme: Theme filter
        search: Substring match over the dataset text
        since: Keep only datasets with dateUpdated >= this date ("2026-03-01").
               dateUpdated is when the vendor last shipped data for the set, so
               this is the "what is new" filter.
        sort: valueScore | userCount | alphaCount | fieldCount | pyramidMultiplier
              | -dateUpdated (newest first) | dateUpdated | id
        limit: Rows to return (default 40)
        offset: Row offset for paging; the response carries next_offset

    Returns:
        {count, returned, results, facets, next_offset?}
    """
    try:
        data = await brain_client.get_datasets(category, region, delay, universe, theme, search)
        data = _filter_by_date(data, 'dateUpdated', since)
        return _slim_datasets(data, limit=limit, offset=offset, sort=sort)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_datafields(
    region: str,
    dataset_id: Optional[str],
    universe: str,
    delay: int = 1,
    data_type: str = "",
    search: Optional[str] = None,
    filter_sharpe: bool = True,
    since: Optional[str] = None,
    sort: str = "userCount",
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Get available data fields for alpha construction.

    Returns a page of fields plus facet counts (dataset, category, type,
    dateCreated, userCount) describing the WHOLE match, so you can narrow
    precisely. Do not try to pull a whole region's catalogue: USA/TOP3000 holds
    91k fields. Read the facets, then re-query with dataset_id / data_type /
    search / since.

    COMPLETENESS: without `dataset_id`, results come from a window the platform
    caps at 10000 rows — for USA/TOP3000 that is 11% of the catalogue, and 267
    datasets return nothing. A capped result says so (`capped`, `warning`).
    Passing `dataset_id` always returns that dataset in full. To remove the cap
    everywhere, run build_datafield_catalogue once.

    SORT BIAS: the default `sort="userCount"` is most-used-first, so fields with
    userCount=0 sit at the very bottom and are easy to never see — yet for alpha
    research an uncrowded field is often the more valuable one. The `userCount`
    facet reports how many such fields the match contains; use `sort="dateCreated"`
    (newest data), `sort="coverage"`, or browse per `dataset_id` to reach them.

    By default, fields with OS/IS Sharpe ratio < 0 are filtered out.

    Args:
        region: Market region (e.g., "USA"、"GLB"、"IND"、"ASI"、"CHN")
        delay: Data delay (0 or 1)
        universe: Universe of stocks (e.g., USA和GLB默认"TOP3000"、IND默认"TOP500"、ASI默认"MINVOL1M"、CHN默认"TOP2000U")
        dataset_id: Specific dataset ID to filter by
        data_type: Type of data (e.g., "MATRIX",'VECTOR','GROUP')
        search: Full-text search over field descriptions (FTS5 + porter stemming,
            BM25-ranked). Space-separated words are AND-ed; FTS5 syntax works too
            ("dividend AND (cut OR lower OR reduce)", "\"short squeeze\"").
            It matches the WORDS in a description, not the meaning: "investor
            attention" returns nothing because no description uses that phrase,
            though search-volume and news-buzz fields exist. Widen with OR terms
            when a concept can be worded several ways. Falls back to substring
            matching if the index has not been built.
        filter_sharpe: Filter out fields with OS/IS Sharpe < 0 (default: True)
        since: Keep only fields with dateCreated >= this date ("2026-03-01").
               New fields land in monthly batches, so this is how you find data
               that was not there last time you looked.
        sort: userCount | alphaCount | coverage | -dateCreated (newest first)
              | dateCreated | id
        limit: Rows to return (default 50)
        offset: Row offset for paging; the response carries next_offset

    Returns:
        {count, returned, results, facets, next_offset?}
    """
    instrument_type = "EQUITY"
    theme = "false"
    try:
        data = await brain_client.get_datafields(
            instrument_type, region, delay, universe, theme, dataset_id, data_type, search, filter_sharpe)
        data = _filter_by_date(data, 'dateCreated', since)
        return _slim_datafields(data, limit=limit, offset=offset, sort=sort)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_alpha_pnl(alpha_id: str) -> Dict[str, Any]:
    """
    Get PnL (Profit and Loss) data for an alpha.
    
    Args:
        alpha_id: The ID of the alpha
    
    Returns:
        PnL data for the alpha
    """
    try:
        return _slim_pnl(await brain_client.get_alpha_pnl(alpha_id))
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_user_alphas(
    stage: str = "IS",
    limit: int = 30,
    offset: int = 0,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    submission_start_date: Optional[str] = None,
    submission_end_date: Optional[str] = None,
    order: Optional[str] = None,
    hidden: Optional[bool] = None,
    region: Optional[str] = None,
    status: Optional[str] = None,
    type: Optional[str] = None,
    is_super: Optional[bool] = None,
    color: Optional[str] = None,
    name: Optional[str] = None,
    tag: Optional[str] = None,
    language: Optional[str] = None,
    min_sharpe: Optional[float] = None,
    min_fitness: Optional[float] = None,
    max_turnover: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Get user's alphas with advanced filtering, pagination, and sorting.

    This tool retrieves a list of your alphas, allowing for detailed filtering based on stage,
    creation date, submission date, visibility, region, status, type, and super alpha flag.
    It also supports pagination and custom sorting.

    Args:
        stage (str): The stage of the alphas to retrieve.
            - "IS": In-Sample (alphas that have not been submitted).
            - "OS": Out-of-Sample (alphas that have been submitted).
            Defaults to "IS".
        limit (int): The maximum number of alphas to return in a single request.
            For example, `limit=50` will return at most 50 alphas. Defaults to 30.
        offset (int): The number of alphas to skip from the beginning of the list.
            Used for pagination. For example, `limit=50, offset=50` will retrieve alphas 51-100.
            Defaults to 0.
        start_date (Optional[str]): The earliest creation date for the alphas to be included.
            Filters for alphas created after this date (strictly greater).
            Example format: "2023-01-01T00:00:00Z". Date-only or timezone-less values
            are accepted and normalized to UTC automatically.
        end_date (Optional[str]): The latest creation date for the alphas to be included.
            Filters for alphas created before this date.
            Example format: "2023-12-31T23:59:59Z". Normalized like start_date.
        submission_start_date (Optional[str]): The earliest submission date for the alphas.
            Only applies to "OS" alphas. Filters for alphas submitted after this date.
            Example format: "2024-01-01T00:00:00Z". Normalized like start_date.
        submission_end_date (Optional[str]): The latest submission date for the alphas.
            Only applies to "OS" alphas. Filters for alphas submitted before this date.
            Example format: "2024-06-30T23:59:59Z". Normalized like start_date.
        order (Optional[str]): The sorting order for the returned alphas.
            Prefix with a hyphen (-) for descending order. Nested fields are supported.
            Examples: "name", "-dateSubmitted", "-is.sharpe", "dateCreated".
        hidden (Optional[bool]): Filter alphas based on their visibility.
            - `True`: Only return hidden alphas.
            - `False`: Only return non-hidden alphas.
            If not provided, both hidden and non-hidden alphas are returned.
        region (Optional[str]): Filter alphas by region (server-side, settings.region).
            Common values: "USA", "EUR", "ASI", "GLB", "CHN", "GBR", etc.
            If not provided, alphas from all regions are returned.
        status (Optional[str]): Filter alphas by status (server-side).
            Common values: "ACTIVE", "UNSUBMITTED", "DECOMMISSIONED", etc.
            If not provided, alphas with any status are returned.
        type (Optional[str]): Filter alphas by their expression type (server-side).
            Common values: "REGULAR", "SUPER", etc.
            If not provided, alphas of all types are returned.
        is_super (Optional[bool]): Filter to only super alphas (True) or non-super alphas (False).
            Applied server-side as type=SUPER / type!=SUPER. If not provided, both are returned.
        color (Optional[str]): Filter alphas by their color label (server-side).
            Values: "RED", "GREEN", "BLUE", "YELLOW", "PURPLE" (case-insensitive here,
            normalized to uppercase). If not provided, alphas of any color are returned.
        name (Optional[str]): Filter alphas by name (server-side, EXACT match only —
            the API has no substring/fuzzy name matching).
        tag (Optional[str]): Filter alphas that carry this tag (server-side, exact tag
            value, e.g. "PowerPoolSelected"). One tag per query.
        min_sharpe (Optional[float]): Keep only alphas with IS sharpe above this
            (server-side). The cheapest way to narrow a query — measured on one
            day's 6300 alphas, min_sharpe=1.0 returned 2411 and min_fitness=1.0
            returned 1016.
        min_fitness (Optional[float]): Same, on IS fitness (server-side).
        max_turnover (Optional[float]): Keep only alphas with IS turnover below
            this (server-side).
        language (Optional[str]): Filter alphas by expression language (server-side,
            settings.language). Values: "FASTEXPR", "PYTHON" (case-insensitive,
            normalized to uppercase). If not provided, alphas of all languages
            are returned.

    All filters are applied server-side by the BRAIN API, so pagination (count/limit/offset)
    reflects the filtered set directly.

    Returns:
        Dict[str, Any]: A dictionary containing a list of alpha details under the 'results' key,
        along with pagination information. If an error occurs, it returns a dictionary with an 'error' key.
    """
    try:
        return _slim_alpha_list(await brain_client.get_user_alphas(
            stage=stage, limit=limit, offset=offset, start_date=start_date,
            end_date=end_date, submission_start_date=submission_start_date,
            submission_end_date=submission_end_date, order=order, hidden=hidden,
            region=region, status=status, alpha_type=type, is_super=is_super,
            color=color, name=name, tag=tag, language=language,
            min_sharpe=min_sharpe, min_fitness=min_fitness, max_turnover=max_turnover,
        ))
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def submit_alpha(alpha_id: str) -> Dict[str, Any]:
    """
    Start an ASYNCHRONOUS submission of an alpha, with pre-submission IS metrics check.

    The platform-side submission check takes from a few minutes up to ~1 hour, so this
    tool does NOT wait for the result. It validates the alpha, starts a background
    submission task, and returns immediately. Use get_submission_status(alpha_id) to
    poll the outcome (polling every 1-5 minutes is enough; do not busy-wait).

    Before starting, this tool automatically checks the alpha's IS metrics against
    the submission thresholds (Sharpe, Fitness, Margin, Turnover, Returns, and all
    IS checks must not FAIL). If the check fails, submission is blocked and failure
    details are returned.

    Args:
        alpha_id: The ID of the alpha to submit
    Returns:
        Immediate acknowledgement that the background submission started (or the
        reason it was blocked). The final result must be read via
        get_submission_status(alpha_id).
    """
    try:
        # Fetch alpha details for IS metrics check
        alpha_details = await brain_client.get_alpha_details(alpha_id)

        stage = alpha_details.get('stage')
        if stage == 'OS' or alpha_details.get('dateSubmitted'):
            return {
                "success": False,
                "blocked": True,
                "reason": f"Alpha {alpha_id} is already submitted (stage={stage}).",
            }

        check_result = brain_client.pre_submit_check(alpha_details)
        if not check_result['passed']:
            return {
                "success": False,
                "blocked": True,
                "reason": "Pre-submission IS metrics check failed. Alpha does not meet submission thresholds.",
                "check_result": check_result,
            }

        # Passed check — start background submission and return immediately
        start = brain_client.start_submission(alpha_id, pre_check=check_result)
        return {
            "async": True,
            "blocked": False,
            "submission": start,
            "message": (
                f"Submission of {alpha_id} started in the background; the platform check "
                f"may take from a few minutes up to ~1 hour. Poll get_submission_status('{alpha_id}') "
                f"every few minutes for the final result."
            ),
            "check_result": check_result,
        }
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_submission_status(alpha_id: str) -> Dict[str, Any]:
    """
    Check the status of an asynchronous alpha submission started via submit_alpha.

    Returns the tracked background-task state: status is one of QUEUED, RUNNING
    (with phase/attempts/polls progress), or a terminal status — SUCCESS,
    ALREADY_SUBMITTED, FAILED (with failed_checks), TIMEOUT, ERROR. The 'done'
    field is true once the submission reached a terminal status.

    If the server was restarted and no in-memory record exists, it falls back to
    probing the platform (alpha stage / dateSubmitted) to tell whether the alpha
    is submitted.

    Args:
        alpha_id: The ID of the alpha whose submission to check
    Returns:
        Submission status snapshot
    """
    try:
        return await brain_client.get_submission_status(alpha_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def value_factor_trendScore(start_date: str, end_date: str) -> Dict[str, Any]:
    """Compute and return the diversity score for REGULAR alphas in a submission-date window.
    This function calculate the diversity of the users' submission, by checking the diversity, we can have a good understanding on the valuefactor's trend.
    This MCP tool wraps BrainApiClient.value_factor_trendScore and always uses submission dates (OS).

    Inputs:
        - start_date: ISO UTC start datetime (e.g. '2025-08-14T00:00:00Z')
        - end_date: ISO UTC end datetime (e.g. '2025-08-18T23:59:59Z')
        - p_max: optional integer total number of pyramid categories for normalization

    Returns: compact JSON with diversity_score, N, A, P, P_max, S_A, S_P, S_H, per_pyramid_counts
    """
    try:
        return await brain_client.value_factor_trendScore(start_date=start_date, end_date=end_date)
    except Exception as e:
        return {"error": str(e)}

# --- Community and Events Tools ---

@mcp.tool()
async def get_events() -> Dict[str, Any]:
    """
    Get available events and competitions.
    
    Returns:
        Available events and competitions
    """
    try:
        return await brain_client.get_events()
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_leaderboard(user_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Get leaderboard data.
    
    Args:
        user_id: Optional user ID to filter results
    
    Returns:
        Leaderboard data
    """
    try:
        return await brain_client.get_leaderboard(user_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


# --- SPC (Systematic Predictions Challenge) Tools ---

@mcp.tool()
async def get_spc_submissions(limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """
    List the current user's SPC (Systematic Predictions Challenge) prompt submissions.

    Args:
        limit: Maximum number of submissions to return (default: 50)
        offset: Pagination offset (default: 0)

    Returns:
        Paginated list of submissions with id, name, prompt, sampleOutput, model,
        modelVersion, weight, updateFrequency, lastModified, and status
    """
    try:
        return await brain_client.get_spc_submissions(limit, offset)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def create_spc_submission(
    name: str,
    prompt: str,
    sample_output: str,
    model: str,
    model_version: str,
    weight: float,
    update_frequency: str,
    skip_validation: bool = False,
) -> Dict[str, Any]:
    """
    Create a new SPC (Systematic Predictions Challenge) prompt submission.

    The prompt is run periodically by the platform on the chosen model; its JSON
    output (ISIN|MIC keys, confidence scores in [-1, 1]) forms a long/short
    portfolio whose PnL is scored. Local validation of the sample output
    (JSON shape, ISIN|MIC format, ISIN checksum, score range) runs before
    submitting; failures are returned without submitting.

    Args:
        name: Submission name (max 200 characters)
        prompt: English prompt text sent to the model (max 10000 characters)
        sample_output: Sample JSON output produced by the prompt, as a string.
            Must be a pure JSON object mapping "ISIN|MIC" to numeric scores in [-1, 1]
        model: One of gpt, claude, gemini, deepseek, kimi, qwen, glm, llama, minimax, mistral
        model_version: Model version string, e.g. "5" or "4.8" (max 100 characters)
        weight: Prompt weight between 0 and 1 (two decimals). 0 means the prompt does not run
        update_frequency: One of daily, weekly, monthly, quarterly
        skip_validation: Submit even if local validation fails (default: False)

    Returns:
        The created submission (including its id), or validation errors
    """
    try:
        return await brain_client.create_spc_submission(
            name, prompt, sample_output, model, model_version, weight, update_frequency, skip_validation
        )
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def set_spc_submission_weight(submission_id: str, weight: float) -> Dict[str, Any]:
    """
    Set the weight of an existing SPC submission. Setting weight to 0 withdraws it.

    Weight is the ONLY field the platform allows changing after creation; there
    is no DELETE, and prompt text, model, and frequency are immutable. To change
    a prompt's content, create a new submission with create_spc_submission and
    set the old one's weight to 0. Use get_spc_submissions to find ids.

    Args:
        submission_id: Id of the submission to update (e.g. "V45nl1y")
        weight: New weight between 0 and 1 (two decimals); 0 withdraws the prompt

    Returns:
        The updated submission, or validation errors
    """
    try:
        return await brain_client.set_spc_submission_weight(submission_id, weight)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_spc_leaderboard(
    board: Optional[str] = None,
    limit: int = 30,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Get the SPC (Systematic Predictions Challenge) monthly leaderboard.

    Args:
        board: Month key like "202607" (default: current month, chosen server-side)
        limit: Maximum number of entries to return (default: 30)
        offset: Pagination offset (default: 0)

    Returns:
        Leaderboard entries aggregated by user
    """
    try:
        return await brain_client.get_spc_leaderboard(board, limit, offset)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


# --- Forum Tools ---

@mcp.tool()
async def get_operators() -> Dict[str, Any]:
    """
    Get available operators for alpha creation.
    
    Returns:
        Dictionary containing operators list and count
    """
    try:
        operators = await brain_client.get_operators()
        if isinstance(operators, list):
            return _slim_text_lookup({"results": operators, "count": len(operators)}, n=160)
        return _slim_text_lookup(operators, n=160)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def run_selection(
    selection: str,
    instrument_type: str = "EQUITY",
    region: str = "USA",
    delay: int = 1,
    selection_limit: int = 1000,
    selection_handling: str = "POSITIVE",
) -> Dict[str, Any]:
    """
    Run a selection query to filter instruments.
    
    Args:
        selection: Selection criteria
        instrument_type: Type of instruments
        region: Geographic region
        delay: Delay setting
        selection_limit: Maximum number of results
        selection_handling: How to handle selection results
    
    Returns:
        Selection results
    """
    try:
        return await brain_client.run_selection(
            selection, instrument_type, region, delay, selection_limit, selection_handling
        )
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_user_profile(user_id: str = "self") -> Dict[str, Any]:
    """
    Get user profile information.
    
    Args:
        user_id: User ID (default: "self" for current user)
    
    Returns:
        User profile data
    """
    try:
        return await brain_client.get_user_profile(user_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_documentations() -> Dict[str, Any]:
    """
    Get available documentations and learning materials.
    
    Returns:
        List of documentations
    """
    try:
        return await brain_client.get_documentations()
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

# --- Message and Forum Tools ---

@mcp.tool()
async def get_messages(limit: Optional[int] = None, offset: int = 0) -> Dict[str, Any]:
    """
    Get messages for the current user with optional pagination.
    
    Args:
        limit: Maximum number of messages to return (e.g., 10 for top 10 messages)
        offset: Number of messages to skip (for pagination)
    
    Returns:
        Messages for the current user, optionally limited by count
    """
    try:
        return await brain_client.get_messages(limit, offset)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_glossary_terms(email: str = "", password: str = "") -> List[Dict[str, str]]:
    """
    Get glossary terms from WorldQuant BRAIN forum.
    
    Note: This uses Playwright and is implemented in forum_functions.py
    
    Args:
        email: Your BRAIN platform email address (optional if in config)
        password: Your BRAIN platform password (optional if in config)
    
    Returns:
        A list of glossary terms with definitions
    """
    try:
        config = load_config()
        credentials = config.get("credentials", {})
        email = email or credentials.get("email")
        password = password or credentials.get("password")
        if not email or not password:
            raise ValueError("Authentication credentials not provided or found in config.")
        
        return await brain_client.get_glossary_terms(email, password)
    except Exception as e:
        logger.error(f"Error in get_glossary_terms tool: {e}")
        return [{"error": str(e)}]

@mcp.tool()
async def search_forum_posts(search_query: str, email: str = "", password: str = "", 
                             max_results: int = 50) -> Dict[str, Any]:
    """
    Search forum posts on WorldQuant BRAIN support site.
    
    Note: This uses Playwright and is implemented in forum_functions.py
    
    Args:
        search_query: Search term or phrase
        email: Your BRAIN platform email address (optional if in config)
        password: Your BRAIN platform password (optional if in config)
        max_results: Maximum number of results to return (default: 50)
    
    Returns:
        Search results with analysis
    """
    try:
        config = load_config()
        credentials = config.get("credentials", {})
        email = email or credentials.get("email")
        password = password or credentials.get("password")
        if not email or not password:
            return {"error": "Authentication credentials not provided or found in config."}
            
        return await brain_client.search_forum_posts(email, password, search_query, max_results)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def read_forum_post(article_id: str, email: str = "", password: str = "", 
                          include_comments: bool = True) -> Dict[str, Any]:
    """
    Get a specific forum post by article ID.
    
    Note: This uses Zendesk support SSO plus JSON APIs and is implemented in forum_functions.py
    
    Args:
        article_id: The article ID to retrieve (e.g., "32984819083415-新人求模板")
        email: Your BRAIN platform email address (optional if in config)
        password: Your BRAIN platform password (optional if in config)
    
    Returns:
        Forum post content with comments
    """
    try:
        config = load_config()
        credentials = config.get("credentials", {})
        email = email or credentials.get("email")
        password = password or credentials.get("password")
        if not email or not password:
            return {"error": "Authentication credentials not provided or found in config."}

        return await brain_client.read_forum_post(email, password, article_id, include_comments)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_alpha_yearly_stats(alpha_id: str) -> Dict[str, Any]:
    """Get yearly statistics for an alpha."""
    try:
        return _slim_yearly(await brain_client.get_alpha_yearly_stats(alpha_id))
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def check_correlation(alpha_id: str) -> Dict[str, Any]:
    """Check alpha correlation against production alphas, self alphas, or both.

    Does NOT include the Power Pool (PPA) correlation — use
    check_power_pool_correlation for that (platform call, shares the same
    per-account correlation lock as the production check here).
    """
    correlation_type = "both"
    threshold = 0.7
    try:
        return _slim_check_correlation(await brain_client.check_correlation(alpha_id, correlation_type, threshold))
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def check_power_pool_correlation(alpha_id: str, threshold: float = 0.5) -> Dict[str, Any]:
    """Check alpha correlation against YOUR submitted Power Pool Alphas (PPA) via the PLATFORM.

    WHEN TO USE: ONLY when the user explicitly says they are hunting PPA
    alphas — i.e. the target profile is ppa_failed_count=0 with
    ra_failed_count>0. A regular-alpha candidate (ppa_failed_count=0 AND
    ra_failed_count=0 target) does NOT need this check — do not run it
    routinely; it consumes the account's single platform correlation slot,
    which allows only one check (prod or PPA) every 3 minutes.

    Calls ``GET /alphas/{id}/correlations/power-pool`` — the authoritative
    platform number. (check_self_correlation(correlation_type='powerpool') is
    only a local approximation; the PPA gate must be validated on-platform.)

    Rate limit: this tool SHARES one per-account slot with the
    production-correlation check, and that slot admits ONE check every 3
    minutes across all agents/processes. If a production or power-pool check is
    running, or finished less than 3 minutes ago, it returns
    status='correlation_busy' with retry_after instead of queueing — retry
    after that. Self-correlation is local and unaffected.

    Default threshold 0.5 matches the PPAC submission gate: a PPA whose
    power-pool correlation exceeds 0.5 is rejected (the platform sometimes
    surfaces this as a misleading 'ProdCorrelation' error).

    Returns max_correlation, passes_check, and the top correlated Power Pool
    alpha records. An empty Power Pool yields max_correlation=0.0 (pass).
    """
    try:
        return _slim_check_correlation(await brain_client.check_correlation(alpha_id, "powerpool", threshold))
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def check_self_correlation(
    alpha_id: str,
    threshold: float = 0.7,
    correlation_type: str = 'self',
) -> Dict[str, Any]:
    """Validate self-correlation with the local incremental-cache calculation.

    This does not call the BRAIN /correlations/self endpoint, so it does not
    consume the platform correlation slot and is NOT rate limited — call it as
    often as you like.

    The pool is your whole submitted-OS set for the target's market
    configuration, Power Pool Alphas INCLUDED — they are your submitted alphas
    like any other, and excluding them used to empty the pool (reporting a fake
    max of 0.0) wherever every OS alpha happened to be a PPAC:
      * correlation_type='self' (default) or 'all' -> whole OS pool.
      * correlation_type='powerpool' -> only Power Pool Alphas; a local
        approximation. The PPAC submission gate must be validated with
        check_correlation(alpha_id, 'powerpool'), the platform's own number.

    Args:
        alpha_id: Target alpha ID.
        threshold: Pass/fail threshold applied to each max correlation
            (passes when max < threshold). Default 0.7.
        correlation_type: 'self' | 'all' | 'powerpool'. Default 'self'.

    Returns:
        Dict with the local max self-correlation, pass/fail result, top
        correlated OS alpha records, and pool metadata (pool_size,
        full_os_pool_size, correlation_type). pool_size=0 means the pool was
        empty — no OS alpha exists for this market configuration — so the 0.0
        max is "nothing to compare against", not a low correlation.
    """
    try:
        return _slim_check_correlation(await brain_client.check_self_correlation(
            alpha_id,
            threshold=threshold,
            correlation_type=correlation_type,
        ))
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def compute_mutual_correlation(
    alpha_ids: List[str],
    threshold: float = 0.5,
    years: int = 4,
) -> Dict[str, Any]:
    """Compute pairwise ("mutual") correlation AMONG a given set of your alphas.

    Use this to vet a submission basket that must be mutually decorrelated
    (e.g. a "no two alphas may correlate above 0.5" rule). It is fully local —
    it fetches each alpha's PnL and correlates their daily returns; it does NOT
    call the BRAIN correlation endpoint or consume the correlation slot.

    Distinct from the other two correlation tools:
      * check_correlation      -> target vs the PRODUCTION pool.
      * check_self_correlation -> target vs your submitted-OS pool
                                  (Self excl. Power Pool Alphas / Power Pool only).
      * compute_mutual_correlation (this) -> the full NxN matrix AMONG the
                                  supplied alphas themselves.

    Correlation is on the last ``years`` of daily returns (diff of cumulative
    PnL), matching the local self-correlation convention.

    Args:
        alpha_ids: 2+ alpha IDs to correlate against each other.
        threshold: Max acceptable pairwise correlation (default 0.5).
        years: Trailing window of daily returns to use (default 4).

    Returns:
        Dict with: matrix (NxN), max_pair (most-correlated pair),
        pairs_over_threshold, all_below_threshold (bool), and
        max_mutually_below_subset (a greedy maximal basket whose members are all
        mutually below threshold), plus missing_pnl for any unfetchable ids.
    """
    try:
        return await brain_client.get_mutual_correlation(alpha_ids, threshold=threshold, years=years)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def set_alpha_properties(alpha_id: str, name: Optional[str] = None, 
                               color: Optional[str] = None, tags: Optional[List[str]] = None,
                               descriptions: str = "None",
                               selection_description: Optional[str] = None,
                               combo_description: Optional[str] = None) -> Dict[str, Any]:
    """
      Note: Update alpha properties (name, color, tags, descriptions).
      For SUPER alphas, selection_description and combo_description are also required and must
      each be at least 100 English characters.
      Args:
        color: may be one of `RED` `GREEN` `YELLOW` `BLUE` `PURPLE`；
        name: 使用生产相关性和自相关性命名，不能带空格；建议基于 correlation
        的最大值命名，例如 `0.6534_0.2306` 表示 prod correlation = 0.6534, self correlation = 0.2306；
        tags 如果alpha质量非常好可以设置 `PowerPoolSelected`，质量不好或不好判断就不要设置tag；
        descriptions: Write in English, <=100 words. The three sections MUST be separated by
        actual newline characters (i.e. use the JSON escape sequence \\n\\n between sections,
        NOT the literal text "\\n\\n"). Example value:
        "Idea: <your idea here>\\n\\nRationale for data used: <your rationale>\\n\\nRationale for operators used: <your rationale>"
        The three section headers must appear exactly as:
        - Idea:
        - Rationale for data used:
        - Rationale for operators used:
        selection_description: (SUPER alpha only) Description of the selection expression logic.
        Must be at least 100 English characters. Write in English.
        combo_description: (SUPER alpha only) Description of the combo expression logic.
        Must be at least 100 English characters. Write in English.
    """
    try:
        if descriptions and descriptions == "None":
            return {
                "error": (
                    "descriptions cannot be the literal string 'None'. "
                    "Please regenerate it in English using exactly these three sections: "
                    "Idea:, Rationale for data used:, and Rationale for operators used:."
                )
            }
        # Normalize literal \n sequences to actual newlines in case the LLM emits
        # backslash-n as two characters rather than a true newline escape.
        if descriptions and descriptions != "None":
            descriptions = descriptions.replace('\\n', '\n')
        return _slim_alpha_response(await brain_client.set_alpha_properties(alpha_id, name, color, tags, descriptions,
                                                       selection_description, combo_description))
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_record_sets(alpha_id: str) -> Dict[str, Any]:
    """List available record sets for an alpha."""
    try:
        return await brain_client.get_record_sets(alpha_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_record_set_data(alpha_id: str, record_set_name: str) -> Dict[str, Any]:
    """Get data from a specific record set."""
    try:
        return await brain_client.get_record_set_data(alpha_id, record_set_name)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_user_activities(user_id: str, grouping: Optional[str] = None) -> Dict[str, Any]:
    """Get user activity diversity data."""
    try:
        return await brain_client.get_user_activities(user_id, grouping)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_pyramid_multipliers() -> Dict[str, Any]:
    """Get current pyramid multipliers showing BRAIN's encouragement levels."""
    try:
        return _slim_pyramids(await brain_client.get_pyramid_multipliers(), "multipliers")
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_pyramid_alphas(start_date: Optional[str] = None,
                               end_date: Optional[str] = None) -> Dict[str, Any]:
    """Get user's current alpha distribution across pyramid categories.
    Defaults to the current quarter if no dates are provided."""
    try:
        return _slim_pyramids(await brain_client.get_pyramid_alphas(start_date, end_date), "alphas")
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


# --- Candidate pool ------------------------------------------------------- #
# BRAIN accepts only ~4 regular-alpha submissions per day, so qualified alphas
# queue in a local pool. Two facts make the pool more than a list:
#   1. Pyramid planning must count submitted + pooled together.
#   2. Submitting one alpha RAISES every other candidate's production and self
#      correlation, because the submitted alpha joins the pool they are measured
#      against:  projected_corr(B) = max(corr_now(B), |corr(B, submitted)|).
# The pool therefore enforces pairwise |corr| < prod threshold at admission —
# the only moment when acting on it is still cheap.

@mcp.tool()
async def pool_check(
    alpha_id: str,
    prod_threshold: float = 0.70,
    self_threshold: float = 0.70,
    mutual_threshold: float = 0.40,
    refresh_prod: bool = True,
) -> Dict[str, Any]:
    """Dry-run: may this alpha join the candidate pool? Does NOT modify the pool.

    Checks four things and reports every violation rather than stopping at the
    first: the alpha's own production correlation, its own self correlation, its
    pairwise correlation against each pooled candidate versus ``prod_threshold``
    (SAFETY — a pair at/above this means submitting either one pushes the other
    past the production gate), and versus ``mutual_threshold`` (DIVERSITY, the
    basket rule).

    Pairwise correlations are computed locally from PnL and consume no BRAIN
    correlation slot; only the alpha's own production correlation hits the
    rate-limited endpoint (skip it with refresh_prod=false when cached).
    """
    try:
        return await cpool.evaluate_candidate(
            brain_client, alpha_id, cpool.load_pool(),
            prod_threshold=prod_threshold,
            self_threshold=self_threshold,
            mutual_threshold=mutual_threshold,
            refresh_prod=refresh_prod,
        )
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


@mcp.tool()
async def pool_add(
    alpha_id: str,
    note: Optional[str] = None,
    force: bool = False,
    allow_diversity_fail: bool = False,
    prod_threshold: float = 0.70,
    self_threshold: float = 0.50,
    mutual_threshold: float = 0.40,
    refresh_prod: bool = True,
) -> Dict[str, Any]:
    """Admit an alpha to the candidate pool after running the pool_check gates.

    Rejected candidates are reported with reasons and NOT stored. ``force=true``
    admits anyway and records ``forced_reasons`` on the entry so the compromise
    stays visible in later planning. ``allow_diversity_fail=true`` waives only
    the mutual-correlation basket rule, never the production-safety rule.

    This never submits anything.
    """
    try:
        return await cpool.add_candidate(
            brain_client, alpha_id,
            note=note, force=force, allow_diversity_fail=allow_diversity_fail,
            prod_threshold=prod_threshold,
            self_threshold=self_threshold,
            mutual_threshold=mutual_threshold,
            refresh_prod=refresh_prod,
        )
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


@mcp.tool()
async def pool_remove(alpha_ids: List[str]) -> Dict[str, Any]:
    """Drop alphas from the candidate pool (e.g. after manual submission)."""
    try:
        return cpool.remove_candidates(alpha_ids)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


@mcp.tool()
async def pool_list(region: Optional[str] = None,
                    pyramid: Optional[str] = None) -> Dict[str, Any]:
    """List pooled candidates with metrics and correlations, newest gates first."""
    try:
        return cpool.list_pool(region=region, pyramid=pyramid)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


@mcp.tool()
async def pool_pyramid_coverage(
    region: Optional[str] = None,
    delay: Optional[int] = None,
    target: int = 3,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """True pyramid coverage: submitted alphas AND pooled candidates, per pyramid.

    A pyramid lights on SUBMITTED alphas only (``target`` of them, default 3).
    The pool column is the queue that can get it there, so each row reports
    ``needed_submissions`` and whether the pool can cover it:
      OS_SUFFICIENT                    already lit
      NEEDS_<n>_SUBMISSIONS_FROM_POOL  pool has enough candidates queued
      SHORT_BY_<n>_CANDIDATES          research still needed

    Omit region/delay to aggregate across all of them.
    """
    try:
        return await cpool.pyramid_coverage(
            brain_client, region=region, delay=delay, target=target,
            start_date=start_date, end_date=end_date,
        )
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


@mcp.tool()
async def pool_submission_plan(
    max_submissions: int = 4,
    region: Optional[str] = None,
    delay: Optional[int] = None,
    target: int = 3,
    prod_threshold: float = 0.70,
    self_threshold: float = 0.70,
    resolve_conflicts: bool = False,
) -> Dict[str, Any]:
    """Pick today's submission batch and prove it is safe for the rest of the pool.

    Ranks candidates by unmet pyramid need, then pyramid multiplier, then Sharpe,
    and takes up to ``max_submissions`` (BRAIN's daily regular-alpha cap is 4).
    A candidate is skipped when it clashes with one already selected, or when
    submitting it would push a candidate left in the pool past a gate:

        projected_prod_corr(B) = max(prod_corr_now(B), max |corr(B, batch)|)
        projected_self_corr(B) = max(self_corr_now(B), max |corr(B, batch)|)

    ``remaining_pool_after_batch`` lists that projection for every candidate left
    behind, and ``all_remaining_safe`` is the single answer to "will submitting
    this batch break anything still queued?".

    If two pooled candidates are mutually exclusive (submitting either destroys
    the other), a purely protective plan would submit neither and stall forever.
    Those pairs appear in ``conflicts`` with a recommended keep/drop;
    ``resolve_conflicts=true`` acts on the recommendation and lists the give-ups
    in ``sacrificed``.

    Returns a plan only — the agent never submits.
    """
    try:
        return await cpool.submission_plan(
            brain_client,
            max_submissions=max_submissions, region=region, delay=delay,
            target=target, prod_threshold=prod_threshold,
            self_threshold=self_threshold, resolve_conflicts=resolve_conflicts,
        )
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


@mcp.tool()
async def pool_sync(refresh_prod: bool = False, refresh_details: bool = False) -> Dict[str, Any]:
    """Refresh pooled entries; drop any that are now submitted.

    Costs 1-2 requests regardless of pool size: a pooled alpha is in-sample and
    its record is frozen (re-simulating produces a new id), so the only change
    worth detecting is submission — which is read from the submitted-alpha list
    rather than by fetching every entry.

    ``refresh_details=true`` re-reads every entry's record instead (one request
    per entry); use it to pick up manual edits or platform-side deletions.
    ``refresh_prod=true`` also re-queries production correlation per entry. That
    endpoint is single-concurrency and slow, so it is off by default — use it
    after you submit something, since submissions move everyone's numbers.
    """
    try:
        return await cpool.sync_pool(
            brain_client, refresh_prod=refresh_prod, refresh_details=refresh_details)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


@mcp.tool()
async def recommend_datasets(
    region: str = "USA",
    delay: int = 1,
    universe: str = "TOP3000",
    top_n: int = 20,
) -> Dict[str, Any]:
    """
    Recommend datasets for alpha construction with unlit pyramid priority:
    
    1. **Pyramid lighting (点塔)**: Uses the pyramid-alphas and pyramid-multipliers
       endpoints. Unlit pyramids (fewer than 3 alphas) are recommended first.
    2. **Dataset quality**: Ranks datasets by OS/IS Sharpe within the same pyramid.
    3. **Dataset popularity**: Favors datasets with more platform users and more
       submitted alphas (dataset userCount and alphaCount).
    4. **Randomness**: Adds a small random score so recommendations keep some variety.
    
    Each dataset gets a score (0~95):
    - Pyramid lighting: 0~40 pts
    - Dataset quality: 0~30 pts
    - Dataset users: 0~10 pts
    - Dataset submissions: 0~10 pts
    - Randomness: 0~5 pts
    
    Args:
        region: Market region (e.g., "USA", "CHN", "EUR", "ASI", "GLB")
        delay: Data delay (0 or 1)
        universe: Stock universe (e.g., "TOP3000")
        top_n: Number of top recommendations to return (default 20)
    
    Returns:
        Ranked dataset recommendations with scores, pyramid status summary,
        and neutralization options for the selected region/delay/universe.
    """
    try:
        return await brain_client.recommend_datasets(region, delay, universe, top_n)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}
        
@mcp.tool()
async def get_user_competitions(user_id: Optional[str] = None) -> Dict[str, Any]:
    """Get list of competitions that the user is participating in."""
    try:
        return await brain_client.get_user_competitions(user_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_competition_details(competition_id: str) -> Dict[str, Any]:
    """Get detailed information about a specific competition."""
    try:
        return await brain_client.get_competition_details(competition_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_competition_agreement(competition_id: str) -> Dict[str, Any]:
    """Get the rules, terms, and agreement for a specific competition."""
    try:
        return await brain_client.get_competition_agreement(competition_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_platform_setting_options() -> Dict[str, Any]:
    """Discover valid simulation setting options (instrument types, regions, delays, universes, neutralization).

    Use this when a simulation request might contain an invalid/mismatched setting. If an AI or user supplies
    incorrect parameters (e.g., wrong region for an instrument type), call this tool to retrieve the authoritative
    option sets and correct the inputs before proceeding.

    Returns:
        A structured list of valid combinations and choice lists to validate or fix simulation settings.
    """
    try:
        return _slim_text_lookup(await brain_client.get_platform_setting_options(), n=300)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def performance_comparison(alpha_id: str, competition: Optional[str] = None,
                                 team_id: Optional[str] = None) -> Dict[str, Any]:
    """Get the mandatory pre-submission comparison for an unsubmitted IS alpha.

    Call and save this check before submission.  BRAIN does not retain it for
    this API after an alpha moves to OS; an OS result means the report can no
    longer be retrieved, not that the endpoint is broken.  This tool never uses
    the removed legacy ``/alphas/{id}/performance-comparison`` route.

    Every non-empty valid response is automatically archived under
    ``D:\\wqb_tuzige\\tracking\\performance_comparison`` (override with
    ``BRAIN_PERFORMANCE_COMPARISON_ARCHIVE``).  The returned ``_archive`` field
    records whether and where it was saved.

    Args:
        alpha_id: The alpha ID (e.g. "A1wYQ2xd" or "XgpEr77l").
        competition: Optional competition ID (e.g. "PAC2026"). If omitted,
            the user's own (self) alpha endpoint is used.
        team_id: Optional team ID.
    """
    try:
        return await brain_client.performance_comparison(alpha_id, competition, team_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}
        
# --- Dataframe Tool ---

@mcp.tool()
async def expand_nested_data(data: List[Dict[str, Any]], preserve_original: bool = True) -> List[Dict[str, Any]]:
    """Flatten complex nested data structures into tabular format."""
    try:
        return await brain_client.expand_nested_data(data, preserve_original)
    except Exception as e:
        return [{"error": f"An unexpected error occurred: {str(e)}"}]
        
# --- Documentation Tool ---

@mcp.tool()
async def get_documentation_page(page_id: str) -> Dict[str, Any]:
    """Retrieve detailed content of a specific documentation page/article."""
    try:
        return await brain_client.get_documentation_page(page_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

# --- Advanced Simulation Tools ---

@mcp.tool()
async def create_multi_simulation(
    alpha_expressions: List[str],
    instrument_type: str = "EQUITY",
    region: str = "USA",
    universe: str = "TOP3000",
    delay: int = 1,
    decay: int = 4,
    neutralization: str = "INDUSTRY",
    truncation: float = 0.0,
    test_period: str = "P0Y0M",
    unit_handling: str = "VERIFY",
    nan_handling: str = "OFF",
    language: str = "FASTEXPR",
    lookback: Optional[int] = None,
    visualization: bool = False,
    pasteurization: str = "ON",
    max_trade: str = "OFF"
) -> Dict[str, Any]:
    """
    🚀 Create multiple regular alpha simulations on BRAIN platform in a single request.
    
    This tool creates a multisimulation with multiple regular alpha expressions,
    waits for all simulations to complete, and returns detailed results for each alpha.
    
    ⏰ NOTE: Multisimulations can take 8+ minutes to complete. This tool will wait
    for the entire process and return comprehensive results.
    Call get_platform_setting_options to get the valid options for the simulation.
    Args:
        alpha_expressions: List of alpha expressions/code strings (2-10 expressions required)
        instrument_type: Type of instruments (default: "EQUITY")
        region: Market region (default: "USA")
        universe: Universe of stocks (default: "TOP3000")
        delay: Data delay (default: 1)
        decay: Decay value (default: 4)
        neutralization: Neutralization method (default: "NONE")
        truncation: Truncation value (default: 0.0)
        test_period: Test period (default: "P0Y0M")
        unit_handling: Unit handling method. Used for FASTEXPR simulations.
        nan_handling: NaN handling method. Used for FASTEXPR simulations.
        language: Expression language ("FASTEXPR" or "PYTHON")
        lookback: Historical lookback window. Only used for PYTHON simulations; defaults to 256 for PYTHON.
        visualization: Enable visualization (default: False)
        pasteurization: Pasteurization setting (default: "ON")
        max_trade: Max trade setting (default: "OFF")
    
    Returns:
        Dictionary containing multisimulation results and individual alpha details
    """
    try:
        # Validate input
        if len(alpha_expressions) < 2:
            return {"error": "At least 2 alpha expressions are required"}
        if len(alpha_expressions) > 10:
            return {"error": "Maximum 10 alpha expressions allowed per request"}

        await brain_client.ensure_authenticated()
        normalized_language = language.upper()
        
        # Create multisimulation data
        multisimulation_data = []
        for alpha_expr in alpha_expressions:
            settings = {
                'instrumentType': instrument_type,
                'region': region,
                'universe': universe,
                'delay': delay,
                'decay': decay,
                'neutralization': neutralization,
                'truncation': truncation,
                'pasteurization': pasteurization,
                'language': normalized_language,
                'visualization': visualization,
                'testPeriod': test_period,
                'maxTrade': max_trade
            }

            if normalized_language == "PYTHON":
                settings['lookback'] = 256 if lookback is None else lookback
            else:
                settings['unitHandling'] = unit_handling
                settings['nanHandling'] = nan_handling

            simulation_item = {
                'type': 'REGULAR',
                'settings': settings,
                'regular': alpha_expr
            }
            multisimulation_data.append(simulation_item)
        
        # Send multisimulation request
        response = await brain_client._request('POST', f"{brain_client.base_url}/simulations", json=multisimulation_data)
        
        if response.status_code != 201:
            return {
                "error": f"Failed to create multisimulation. Status: {response.status_code}",
                "details": response.text,
            }
        
        # Get multisimulation location
        location = response.headers.get('Location', '')
        if not location:
            return {"error": "No location header in multisimulation response"}
        
        # Wait for children to appear and get results
        return _slim_multisim(await _wait_for_multisimulation_completion(location, len(alpha_expressions)))
        
    except Exception as e:
        return {"error": f"Error creating multisimulation: {str(e)}"}

async def _wait_for_multisimulation_completion(location: str, expected_children: int) -> Dict[str, Any]:
    """Wait for multisimulation to complete and return results"""
    try:
        # Simple progress indicator for users
        print(f"Waiting for multisimulation to complete... (this may take several minutes)", file=sys.stderr)
        print(f"Expected {expected_children} alpha simulations", file=sys.stderr)
        print("", file=sys.stderr)
        # Wait for children to appear - much more tolerant for 8+ minute multisimulations
        children = []
        max_wait_attempts = 200  # Increased significantly for 8+ minute multisimulations
        wait_attempt = 0
        
        while wait_attempt < max_wait_attempts and len(children) == 0:
            wait_attempt += 1
            
            try:
                multisim_response = await brain_client._request('GET', location)
                if multisim_response.status_code == 200:
                    multisim_data = multisim_response.json()
                    children = multisim_data.get('children', [])
                    
                    if children:
                        break
                    else:
                        # Wait before next attempt - use longer intervals for multisimulations
                        retry_after = multisim_response.headers.get("Retry-After", 5)
                        wait_time = float(retry_after)
                        await asyncio.sleep(wait_time)
            except Exception as e:
                await asyncio.sleep(5)
        
        if not children:
            return {"error": f"Children did not appear within {max_wait_attempts} attempts (multisimulation may still be processing)"}
        
        # Process each child to get alpha results
        alpha_results = []
        for i, child_id in enumerate(children):
            try:
                # The children are full URLs, not just IDs
                child_url = child_id if child_id.startswith('http') else f"{brain_client.base_url}/simulations/{child_id}"
                
                # Wait for this alpha to complete - more tolerant timing
                finished = False
                max_alpha_attempts = 100  # Increased for longer alpha processing
                alpha_attempt = 0
                
                while not finished and alpha_attempt < max_alpha_attempts:
                    alpha_attempt += 1
                    
                    try:
                        alpha_progress = await brain_client._request('GET', child_url)
                        if alpha_progress.status_code == 200:
                            alpha_data = alpha_progress.json()
                            # Retry-After arrives as a string; comparing it to the
                            # int 0 never matched, so a literal "0" fell through to
                            # sleep(0) and busy-polled the platform.
                            try:
                                wait_time = float(alpha_progress.headers.get("Retry-After") or 0)
                            except (TypeError, ValueError):
                                wait_time = 0.0
                            if wait_time <= 0:
                                finished = True
                                break
                            await asyncio.sleep(wait_time)
                        else:
                            await asyncio.sleep(5)
                    except Exception as e:
                        await asyncio.sleep(5)
                
                if finished:
                    # Get alpha details from the completed simulation
                    alpha_id = alpha_data.get("alpha")
                    if alpha_id:
                        # Now get the actual alpha details from the alpha endpoint
                        try:
                            child_alpha = await brain_client.get_alpha_details(alpha_id)
                            await brain_client.record_alpha_locally(child_alpha)
                            alpha_results.append({
                                'alpha_id': alpha_id,
                                'location': child_url,
                                'details': child_alpha
                            })
                        except Exception as detail_err:
                            alpha_results.append({
                                'alpha_id': alpha_id,
                                'location': child_url,
                                'error': f'Failed to get alpha details: {detail_err}'
                            })
                    else:
                        alpha_results.append({
                            'location': child_url,
                            'error': 'No alpha ID found in completed simulation'
                        })
                else:
                    alpha_results.append({
                        'location': f"child_{i+1}",
                        'error': f'Alpha simulation did not complete within {max_alpha_attempts} attempts'
                    })
                    
            except Exception as e:
                alpha_results.append({
                    'location': f"child_{i+1}",
                    'error': str(e)
                })
        
        # Return comprehensive results
        print(f"Multisimulation completed! Retrieved {len(alpha_results)} alpha results", file=sys.stderr)
        return {
            'success': True,
            'message': f'Successfully created {expected_children} regular alpha simulations',
            'total_requested': expected_children,
            'total_created': len(alpha_results),
            'multisimulation_id': location.split('/')[-1],
            'multisimulation_location': location,
            'alpha_results': alpha_results
        }
        
    except Exception as e:
        return {"error": f"Error waiting for multisimulation completion: {str(e)}"}

# --- Three-stage multisimulation tools (throttling-era pattern) --------------
# Split of create_multi_simulation into submit / check / fetch so callers
# control the polling cadence, see rate-limit signals (429 + Retry-After,
# CONCURRENT/DAILY_SIMULATION_LIMIT bodies, per-child CANCELLED statuses),
# and never need multi-minute blocking tool calls.

def _build_multisim_payload(alpha_expressions, instrument_type, region, universe,
                            delay, decay, neutralization, truncation, test_period,
                            unit_handling, nan_handling, language, lookback,
                            visualization, pasteurization, max_trade):
    normalized_language = language.upper()
    payload = []
    for alpha_expr in alpha_expressions:
        settings = {
            'instrumentType': instrument_type,
            'region': region,
            'universe': universe,
            'delay': delay,
            'decay': decay,
            'neutralization': neutralization,
            'truncation': truncation,
            'pasteurization': pasteurization,
            'language': normalized_language,
            'visualization': visualization,
            'testPeriod': test_period,
            'maxTrade': max_trade
        }
        if normalized_language == "PYTHON":
            settings['lookback'] = 256 if lookback is None else lookback
        else:
            settings['unitHandling'] = unit_handling
            settings['nanHandling'] = nan_handling
        payload.append({'type': 'REGULAR', 'settings': settings,
                        'regular': alpha_expr})
    return payload


@mcp.tool()
async def submit_multi_simulation(
    alpha_expressions: List[str],
    instrument_type: str = "EQUITY",
    region: str = "USA",
    universe: str = "TOP3000",
    delay: int = 1,
    decay: int = 4,
    neutralization: str = "INDUSTRY",
    truncation: float = 0.0,
    test_period: str = "P0Y0M",
    unit_handling: str = "VERIFY",
    nan_handling: str = "OFF",
    language: str = "FASTEXPR",
    lookback: Optional[int] = None,
    visualization: bool = False,
    pasteurization: str = "ON",
    max_trade: str = "OFF"
) -> Dict[str, Any]:
    """Stage 1/3: submit a multisimulation and return IMMEDIATELY (seconds).

    Returns {submitted, location, multisimulation_id} on success. On HTTP 429
    returns {error: "RATE_LIMITED", retry_after}. Poll the location with
    check_multi_simulation, then call fetch_multi_simulation_result.
    Same parameters as create_multi_simulation.
    """
    try:
        if len(alpha_expressions) < 2:
            return {"error": "At least 2 alpha expressions are required"}
        if len(alpha_expressions) > 10:
            return {"error": "Maximum 10 alpha expressions allowed per request"}
        await brain_client.ensure_authenticated()
        payload = _build_multisim_payload(
            alpha_expressions, instrument_type, region, universe, delay, decay,
            neutralization, truncation, test_period, unit_handling, nan_handling,
            language, lookback, visualization, pasteurization, max_trade)
        response = await brain_client._request(
            'POST', f"{brain_client.base_url}/simulations", json=payload)
        if response.status_code == 429:
            return {
                "error": "RATE_LIMITED",
                "status_code": 429,
                "retry_after": response.headers.get("Retry-After"),
                "details": response.text[:500],
            }
        if response.status_code != 201:
            return {
                "error": f"Failed to create multisimulation. Status: {response.status_code}",
                "status_code": response.status_code,
                "details": response.text[:800],
            }
        location = response.headers.get('Location', '')
        if not location:
            return {"error": "No location header in multisimulation response"}
        return {
            "submitted": True,
            "location": location,
            "multisimulation_id": location.rstrip('/').split('/')[-1],
            "n_expressions": len(alpha_expressions),
        }
    except Exception as e:
        return {"error": f"Error submitting multisimulation: {str(e)}"}


@mcp.tool()
async def check_multi_simulation(location: str) -> Dict[str, Any]:
    """Stage 2/3: single status probe of a multisimulation (one GET, seconds).

    Returns {status, children, retry_after}. Re-call after sleeping
    ~retry_after seconds until status is COMPLETE or ERROR (children may
    appear before completion). RATE_LIMITED status means back off harder.
    """
    try:
        await brain_client.ensure_authenticated()
        response = await brain_client._request('GET', location)
        if response.status_code == 429:
            return {"status": "RATE_LIMITED",
                    "retry_after": response.headers.get("Retry-After") or 30}
        if response.status_code != 200:
            return {"status": "HTTP_ERROR",
                    "status_code": response.status_code,
                    "details": response.text[:500]}
        data = response.json()
        retry_after = response.headers.get("Retry-After")
        status = data.get("status")
        if not status:
            status = "IN_PROGRESS" if retry_after else "UNKNOWN"
        return {
            "status": status,
            "children": data.get("children", []),
            "n_children": len(data.get("children", [])),
            "retry_after": float(retry_after) if retry_after else 0.0,
        }
    except Exception as e:
        return {"status": "CLIENT_ERROR", "error": str(e)}


@mcp.tool()
async def fetch_multi_simulation_result(location: str) -> Dict[str, Any]:
    """Stage 3/3: fetch per-child results after check reports completion.

    One GET per child simulation plus one GET per produced alpha. Child
    simulations without an alpha id report their platform status explicitly
    (e.g. CANCELLED / ERROR) instead of a generic message. Output matches
    create_multi_simulation's alpha_results shape.
    """
    try:
        await brain_client.ensure_authenticated()
        response = await brain_client._request('GET', location)
        if response.status_code != 200:
            return {"error": f"multisim fetch failed: {response.status_code}",
                    "details": response.text[:400]}
        data = response.json()
        children = data.get("children", [])
        if not children:
            return {"error": "no children yet — poll check_multi_simulation until completion",
                    "status": data.get("status")}
        # Children are independent: fetch them concurrently (the rate limiter
        # keeps the fan-out inside the platform's quota) instead of paying two
        # serialized round trips per child.
        async def _fetch_child(i: int, child_id: Any) -> Dict[str, Any]:
            child_url = child_id if str(child_id).startswith('http') \
                else f"{brain_client.base_url}/simulations/{child_id}"
            try:
                child_resp = await brain_client._request('GET', child_url)
                if child_resp.status_code != 200:
                    return {"location": child_url,
                            "error": f"child fetch failed: {child_resp.status_code}"}
                child = child_resp.json()
                child_status = child.get("status")
                alpha_id = child.get("alpha")
                if not alpha_id:
                    return {"location": child_url,
                            "status": child_status,
                            "error": f"No alpha ID (child status: {child_status})"}
                try:
                    details = await brain_client.get_alpha_details(alpha_id)
                except Exception as e:
                    return {"alpha_id": alpha_id, "location": child_url,
                            "error": f"Failed to get alpha details: {e}"}
                await brain_client.record_alpha_locally(details)
                return {"alpha_id": alpha_id, "location": child_url, "details": details}
            except Exception as e:
                return {"location": f"child_{i+1}", "error": str(e)}

        alpha_results = list(await asyncio.gather(
            *[_fetch_child(i, c) for i, c in enumerate(children)]
        ))
        return _slim_multisim({
            "success": True,
            "message": f"fetched {len(alpha_results)} child results",
            "total_requested": len(children),
            "total_created": len(alpha_results),
            "multisimulation_id": location.rstrip('/').split('/')[-1],
            "multisim_status": data.get("status"),
            "alpha_results": alpha_results,
        })
    except Exception as e:
        return {"error": f"Error fetching multisimulation result: {str(e)}"}

# --- Payment and Financial Tools ---

@mcp.tool()
async def get_daily_and_quarterly_payment(email: str = "", password: str = "") -> Dict[str, Any]:
    """
    Get daily and quarterly payment information from WorldQuant BRAIN platform.
    
    This function retrieves both base payments (daily alpha performance payments) and 
    other payments (competition rewards, quarterly payments, referrals, etc.).
    
    Args:
        email: Your BRAIN platform email address (optional if in config)
        password: Your BRAIN platform password (optional if in config)
    
    Returns:
        Dictionary containing base payment and other payment data with summaries and detailed records
    """
    try:
        config = load_config()
        credentials = config.get("credentials", {})
        email = email or credentials.get("email")
        password = password or credentials.get("password")
        if not email or not password:
            return {"error": "Authentication credentials not provided or found in config."}
            
        # Reuse the live session instead of re-running the full Basic-auth
        # handshake (which also clears cookies for every other in-flight call).
        brain_client.auth_credentials = {'email': email, 'password': password}
        await brain_client.ensure_authenticated()

        # Get base payments
        try:
            base_response = await brain_client._request('GET', f"{brain_client.base_url}/users/self/activities/base-payment")
            base_response.raise_for_status()
            base_payments = base_response.json()
        except:
            base_payments = "no data"
            
        try:
            # Get other payments
            other_response = await brain_client._request('GET', f"{brain_client.base_url}/users/self/activities/other-payment")
            other_response.raise_for_status()
            other_payments = other_response.json()
        except:
            other_payments = "no data"    
        return {
            "base_payments": base_payments,
            "other_payments": other_payments
        }
        
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

from typing import Sequence
PERSISTENT_NAMESPACES = (
    'alpha_details', 'alpha_pnl', 'yearly_stats', 'recordset', 'recordsets_index',
    'datafields', 'datasets', 'operators', 'tutorials', 'tutorial_pages',
    'competition_agreement', 'simulation', 'forum_glossary', 'forum_post',
    'datafields_ds',
)


@mcp.tool()
async def analyze_my_research(
    scope: str = "summary",
    region: Optional[str] = None,
    universe: Optional[str] = None,
    delay: Optional[int] = None,
    good_sharpe: float = 1.5,
    contains: Optional[str] = None,
    min_attempts: int = 30,
    limit: int = 25,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> Dict[str, Any]:
    """Mine your own alpha history for what actually works. Zero platform requests.

    Runs against the local SQLite corpus built by sync_alpha_corpus. Because the
    corpus keeps failures as well as successes, it can report *rates* rather than
    just lists — "this field produced a Sharpe>1.5 alpha 12% of the time you used
    it" is a far better research signal than "here are some good alphas".

    scope:
      - "summary"      (default) corpus size, coverage by configuration, hit rate
      - "productivity" per datafield: attempts, hits, hit-rate, best Sharpe.
                       Ranked by hit-rate among fields with >= min_attempts, so a
                       field used twice with one hit does not top the list.
                       READ WITH THE SAMPLE SIZE: a token that only appears in a
                       family of already-careful alphas inherits their hit rate
                       rather than causing it (generate_stats measured 57/57 on
                       this account purely because it only occurs in hand-built
                       Python alphas). Trust the large-sample rows.
      - "operators"    the same, per operator
      - "gaps"         configurations you have barely explored
      - "similar"      full-text search over expressions: have I tried this idea?
                       (pass `contains`)
      - "best"         highest-Sharpe alphas, optionally filtered by configuration

    TIME MATTERS: research improves, so a rate blended over many months describes
    neither the past nor the present. Measured on this account, USA/TOP3000 went
    from a 5.6% hit rate over Jan-Feb to 27.8% over Jan-Apr — the same
    configuration, five times the rate. Use `since`/`until` to ask about a period
    you actually care about; the response reports the window it used.

    Args:
        good_sharpe: the bar that counts as a "hit" (default 1.5)
        min_attempts: ignore rarely-used tokens in the rate rankings
        contains: FTS5 query for scope="similar" (e.g. "ts_rank close")
        since / until: restrict to alphas created in this window ("2026-06-01").
            Compared against UTC-normalised timestamps, so a plain date works.
    """
    store = brain_client.alpha_store
    scope = (scope or "summary").lower().strip()
    try:
        stats = await store.stats()
    except Exception as e:
        return {"error": f"corpus unavailable: {e}"}
    if not stats.get("alphas"):
        return {"error": "The local alpha corpus is empty.",
                "hint": 'Run sync_alpha_corpus(action="start") first.'}

    where, params = [], []
    if region:
        where.append("region = ?"); params.append(region.upper())
    if universe:
        where.append("universe = ?"); params.append(universe.upper())
    if delay is not None:
        where.append("delay = ?"); params.append(delay)
    # Timestamps are stored UTC-normalised, so text comparison is chronological.
    if since:
        where.append("date_created >= ?"); params.append(str(since))
    if until:
        where.append("date_created < ?"); params.append(str(until))
    cfg_sql = (" AND " + " AND ".join(where)) if where else ""
    window = {'since': since, 'until': until} if (since or until) else 'all time'

    if scope == "summary":
        by_cfg = await store.query(
            "SELECT region, universe, delay, COUNT(*) attempts, "
            "SUM(CASE WHEN sharpe >= ? THEN 1 ELSE 0 END) hits, "
            "ROUND(MAX(sharpe), 3) best FROM alphas "
            "WHERE sharpe IS NOT NULL" + cfg_sql + " GROUP BY region, universe, delay "
            "ORDER BY attempts DESC LIMIT ?", (good_sharpe, *params, limit))
        for r in by_cfg:
            r["hit_rate"] = round(r["hits"] / r["attempts"], 4) if r["attempts"] else None
        overall = (await store.query(
            "SELECT COUNT(*) attempts, SUM(CASE WHEN sharpe >= ? THEN 1 ELSE 0 END) hits "
            "FROM alphas WHERE sharpe IS NOT NULL" + cfg_sql, (good_sharpe, *params)))[0]
        # A monthly breakdown makes an improving (or decaying) process visible
        # instead of hiding it inside one blended rate.
        trend = await store.query(
            "SELECT substr(date_created,1,7) month, COUNT(*) attempts, "
            "SUM(CASE WHEN sharpe >= ? THEN 1 ELSE 0 END) hits FROM alphas "
            "WHERE sharpe IS NOT NULL" + cfg_sql + " GROUP BY month ORDER BY month",
            (good_sharpe, *params))
        for r in trend:
            r["hit_rate"] = round(r["hits"] / r["attempts"], 4) if r["attempts"] else None
        return {"corpus": stats, "good_sharpe": good_sharpe, "window": window,
                "by_month": trend,
                "overall": {**overall,
                            "hit_rate": round((overall["hits"] or 0) / overall["attempts"], 4)
                            if overall["attempts"] else None},
                "by_configuration": by_cfg,
                "source": "local SQLite corpus (0 platform requests)"}

    if scope in ("productivity", "operators"):
        kind = "field" if scope == "productivity" else "operator"
        if not stats.get("token_rows"):
            return {"error": "Token index not built yet.",
                    "hint": 'Run sync_alpha_corpus(action="start"); the index is built after a sync.'}
        rows = await store.query(
            "SELECT t.token, COUNT(*) attempts, "
            "SUM(CASE WHEN a.sharpe >= ? THEN 1 ELSE 0 END) hits, "
            "ROUND(MAX(a.sharpe), 3) best, ROUND(AVG(a.sharpe), 4) avg_sharpe "
            "FROM alpha_tokens t JOIN alphas a ON a.id = t.alpha_id "
            "WHERE t.kind = ? AND a.sharpe IS NOT NULL" + cfg_sql +
            " GROUP BY t.token HAVING attempts >= ? "
            "ORDER BY (CAST(hits AS REAL) / attempts) DESC, attempts DESC LIMIT ?",
            (good_sharpe, kind, *params, min_attempts, limit))
        for r in rows:
            r["hit_rate"] = round(r["hits"] / r["attempts"], 4) if r["attempts"] else None
        out_meta = {}
        stale = stats.get("token_index_stale_by")
        if stale:
            out_meta["warning"] = (
                f"The expression index covers {stats.get('token_index_covers')} of "
                f"{stats.get('alphas')} alphas — {stale} newer ones are not counted, so these "
                "rates are understated. Re-run sync_alpha_corpus to refresh it.")
        return {"scope": scope, "kind": kind, "good_sharpe": good_sharpe,
                "window": window, "min_attempts": min_attempts, "results": rows, **out_meta,
                "note": ("hit_rate is hits/attempts — meaningful only because the corpus "
                         "keeps the alphas that failed, not just the ones that worked. "
                         "Weigh each row by its `attempts`: a rare token that only appears "
                         "inside an already-strong family of alphas inherits their hit rate "
                         "rather than causing it."),
                "source": "local SQLite corpus (0 platform requests)"}

    if scope == "gaps":
        rows = await store.query(
            "SELECT region, universe, delay, neutralization, COUNT(*) attempts, "
            "SUM(CASE WHEN sharpe >= ? THEN 1 ELSE 0 END) hits FROM alphas "
            "WHERE sharpe IS NOT NULL" + cfg_sql +
            " GROUP BY region, universe, delay, neutralization "
            "ORDER BY attempts ASC LIMIT ?", (good_sharpe, *params, limit))
        return {"scope": "gaps", "window": window, "least_explored": rows,
                "note": "Combinations with the fewest attempts — where the search is thinnest.",
                "source": "local SQLite corpus (0 platform requests)"}

    if scope == "similar":
        if not contains:
            return {"error": 'scope="similar" needs `contains`, e.g. contains="ts_rank close"'}
        try:
            rows = await store.query(
                "SELECT a.id, a.region, a.universe, a.delay, a.sharpe, a.fitness, "
                "a.date_created, a.expression FROM alphas_fts f "
                "JOIN alphas a ON a.rowid = f.rowid "
                "WHERE alphas_fts MATCH ?" + cfg_sql +
                " ORDER BY a.sharpe DESC LIMIT ?", (contains, *params, limit))
        except Exception as e:
            return {"error": f"FTS query failed: {e}",
                    "hint": "FTS5 syntax: bare words are AND-ed; quote phrases."}
        for r in rows:
            r["expression"] = _truncate(r["expression"], 200)
        return {"scope": "similar", "query": contains, "matches": len(rows), "results": rows,
                "source": "local SQLite corpus (0 platform requests)"}

    if scope == "best":
        rows = await store.query(
            "SELECT id, region, universe, delay, neutralization, sharpe, fitness, turnover, "
            "stage, date_created, expression FROM alphas WHERE sharpe IS NOT NULL" + cfg_sql +
            " ORDER BY sharpe DESC LIMIT ?", (*params, limit))
        for r in rows:
            r["expression"] = _truncate(r["expression"], 200)
        return {"scope": "best", "window": window, "results": rows,
                "source": "local SQLite corpus (0 platform requests)"}

    return {"error": f"Unknown scope {scope!r}",
            "valid": ["summary", "productivity", "operators", "gaps", "similar", "best"]}


@mcp.tool()
async def sync_alpha_corpus(
    action: str = "status",
    since: str = "2026-01-01",
    restart: bool = False,
) -> Dict[str, Any]:
    """Mirror this account's own alpha history into a local SQLite corpus.

    WHY: /users/self/alphas costs ~50 ms and ~3 KB per row and is metered at 30
    requests/minute, and its `count` saturates at 10000 so it cannot even tell
    you how much there is. Locally the same corpus answers "have I tried this",
    "what worked in USA/TOP3000", and "which datafield actually produces
    high-Sharpe alphas" with zero platform requests.

    Every alpha is kept, not only the good ones: productivity is a ratio, and the
    denominator is the alphas that did not work.

    Pagination note: offset cannot be used here (the platform rejects offset>=1000
    with "Cannot display more than the first 1,000 alphas"), so the sweep walks
    forward on dateCreated. It is resumable — the cursor lives in the database.

    action:
      - "status" (default) progress, rate and corpus stats. No side effects.
      - "start"  begin/resume the sync in the background. Returns immediately.
      - "stop"   pause it; the saved cursor means nothing is refetched later.

    Args:
        since: earliest dateCreated to mirror (default 2026-01-01)
        restart: discard the saved cursor and start the window over
    """
    action = (action or "status").lower().strip()
    client = brain_client

    if action == "status":
        snap = client._alpha_sync_snapshot()
        snap['corpus'] = await client.alpha_store.stats()
        if not snap.get('started_at'):
            saved = await client.alpha_store.get_state(client.ALPHA_SYNC_CURSOR)
            snap['saved_cursor'] = saved
            snap['note'] = ('Idle: no sync running in this process (progress is per-process, '
                            'the corpus and cursor are on disk). The corpus figures above are '
                            'current. action="start" resumes from the saved cursor, which after '
                            'a completed run means it only picks up what is new.')
        return snap

    if action == "stop":
        return await client.stop_alpha_sync()

    if action == "start":
        return await client.start_alpha_sync(since=since, restart=restart)

    return {'error': f'Unknown action {action!r}', 'valid': ['status', 'start', 'stop']}


@mcp.tool()
async def build_datafield_catalogue(
    action: str = "status",
    mode: str = "dedup",
    region: Optional[str] = None,
    universe: Optional[str] = None,
    delay: Optional[int] = None,
    instrument_type: str = "EQUITY",
) -> Dict[str, Any]:
    """Build the COMPLETE datafield catalogue, dataset by dataset, in the background.

    WHY THIS EXISTS: an unfiltered /data-fields sweep cannot see the whole
    catalogue. Its `count` saturates at 10000 and offset=10000 is rejected with
    "Invalid offset. Please use filters to narrow down the result." For
    USA/TOP3000 the datasets declare 91076 fields, so a sweep reaches 11% of them
    and 267 datasets return nothing at all — with no signal in the payload that
    anything is missing. Filtering by dataset.id lifts the cap, so the catalogue
    is rebuilt one dataset at a time.

    Runs as a background task inside this server so it shares the rate limiter
    with normal traffic (a separate process would run a second limiter that
    thinks it owns the whole 30/min budget, and the two would collide into 429s).
    Expect foreground calls to be ~1-2s slower while it runs.

    Fully resumable: datasets already stored are skipped, so stopping and
    restarting never repeats work.

    action:
      - "status" (default) progress, coverage and ETA. No side effects.
      - "start"  begin building every market configuration this account uses
                 (derived from the OS PnL pools). Returns immediately.
                 Pass region/universe/delay to build just one configuration.
      - "stop"   cancel the running build; finished datasets stay on disk.

    mode (for "start"):
      - "dedup" (default) which field ids exist depends on region+delay, not on
                universe — verified live: option4 returns the same 1298 ids for
                USA/TOP500 and USA/TOP3000, and all four USA universes declare
                the same 345 datasets / 91076 fields. Only userCount /
                alphaCount / coverage differ. So a configuration whose dataset
                list matches one already built reuses it instead of downloading
                it again, and the payload records which universe the usage
                metrics came from. Roughly halves the work (~4.9h vs ~11.7h for
                this account's 23 configurations).
      - "all"   download every configuration separately, so each carries its own
                usage metrics. Use it when per-universe crowding matters.
    """
    action = (action or "status").lower().strip()
    client = brain_client

    if action == "status":
        snap = client._catalogue_snapshot()
        if not snap.get('started_at'):
            configs = client._configs_in_use()
            return {
                'running': False,
                'note': 'No build has been started in this process.',
                'configurations_in_use': len(configs),
                'configurations': configs,
                'hint': ('Call with action="start" to build the complete catalogue. '
                         'mode="dedup" (default) reuses a region\'s field set across its '
                         'universes and is roughly half the work; mode="all" downloads each '
                         'configuration separately so every universe carries its own '
                         'userCount / coverage.'),
            }
        return snap

    if action == "stop":
        return await client.stop_catalogue_build()

    if action == "start":
        configs = None
        if region and universe and delay is not None:
            configs = [{'instrumentType': instrument_type, 'region': region,
                        'universe': universe, 'delay': delay}]
        if mode not in ('dedup', 'all'):
            return {'error': f'Unknown mode {mode!r}', 'valid': ['dedup', 'all']}
        return await client.start_catalogue_build(configs, mode=mode)

    return {'error': f'Unknown action {action!r}', 'valid': ['status', 'start', 'stop']}


@mcp.tool()
async def sync_platform_cache(
    scope: str = "status",
    region: Optional[str] = None,
    universe: Optional[str] = None,
    delay: Optional[int] = None,
    instrument_type: str = "EQUITY",
    data_type: str = "",
    dataset_id: Optional[str] = None,
    alpha_ids: Optional[List[str]] = None,
    max_entries: int = 3,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Inspect and manually refresh the permanent on-disk platform cache.

    Immutable platform data (submitted-alpha records, alpha PnL, datafield and
    dataset catalogues, the operator list) is stored forever and NEVER expires,
    so ordinary tool calls cost zero platform traffic once warm. Use this tool
    when you actually want to go back to the platform.

    scope:
      - "status"   (default, no network) sizes and ages of every stored namespace.
      - "list"     (no network) the stored parameter sets of one namespace; pass
                   `region`-style args as filters is not needed — use `max_entries`.
      - "migrate"  (no network) copy entries still sitting in Redis into the disk
                   store. Run this once after upgrading; it frees the Redis RAM.
      - "operators"  refetch the operator catalogue (1 request).
      - "datasets"   refetch dataset catalogues. With region/universe/delay given,
                     refreshes exactly that set; otherwise the `max_entries`
                     oldest stored sets.
      - "datafields" same, but each set is up to ~200 paginated requests at
                     1 req/s, so it requires `confirm=True` and is capped hard.
      - "datafields_full" build the COMPLETE catalogue for ONE configuration by
                     walking every dataset. Needed because an unfiltered sweep
                     stops at 10000 rows while the datasets declare far more
                     (USA/TOP3000: 91076), leaving hundreds of datasets invisible.
                     Resumable; needs confirm=True. For all configurations at
                     once use build_datafield_catalogue (runs in the background).
      - "alphas"     refetch the records and PnL of `alpha_ids`.

    Refreshing anything that costs more than a handful of requests requires
    `confirm=True`; without it the call reports what it *would* fetch.
    """
    store = brain_client.store
    scope = (scope or "status").lower().strip()

    if scope == "status":
        stats = await store.stats(PERSISTENT_NAMESPACES)
        out: Dict[str, Any] = {'scope': 'status', 'store_root': str(store.root), 'disk': stats}
        if brain_client.redis_client:
            try:
                leftovers = {}
                for ns in PERSISTENT_NAMESPACES:
                    leftovers[ns] = sum(
                        1 for _ in brain_client.redis_client.scan_iter(f'{ns}:*', count=1000)
                    )
                out['redis_leftovers'] = leftovers
                if any(leftovers.values()):
                    out['hint'] = 'Run scope="migrate" to move these to disk and free the RAM.'
            except Exception as e:
                out['redis_leftovers'] = {'error': str(e)}
        # Which configurations still have blind spots: an unfiltered sweep can
        # only ever see 10000 rows, so compare what is stored against what the
        # datasets declare.
        coverage_rows = []
        for entry in await store.list_entries('datafields'):
            kp = entry.get('key_params') or {}
            # Only whole-configuration catalogues; dataset- or type-scoped
            # entries are subsets and would misreport coverage.
            if not (kp.get('region') and kp.get('universe')):
                continue
            if kp.get('dataset_id') or (kp.get('data_type') or ''):
                continue
            payload = await store.get('datafields', entry.get('key'))
            if not isinstance(payload, dict):
                continue
            _annotate_catalogue_completeness(payload)
            declared = payload.get('declared_total')
            unique = payload.get('count') or 0
            # A field can belong to two datasets, so completeness is judged on
            # rows fetched, not on unique ids.
            fetched = payload.get('fetched_rows', unique)
            coverage_rows.append({
                'config': f"{kp['region']}/{kp['universe']}/delay{kp.get('delay')}",
                'unique_fields': unique,
                'fields_fetched': fetched,
                'declared': declared,
                'coverage': payload.get('coverage'),
                'complete': bool(declared) and fetched >= declared,
                'capped': bool(payload.get('capped')),
            })
        if coverage_rows:
            coverage_rows.sort(key=lambda r: (r['complete'], r['config']))
            out['catalogue_coverage'] = coverage_rows
            blind = [r['config'] for r in coverage_rows if not r['complete']]
            if blind:
                out['catalogue_warning'] = (
                    f"{len(blind)} configuration(s) hold a truncated catalogue: {blind[:8]}"
                    + ('…' if len(blind) > 8 else '')
                    + ". Run build_datafield_catalogue(action='start') to fix."
                )
        out['note'] = ('Stored entries never expire. Nothing here was refetched — '
                       'pass a refresh scope to go back to the platform.')
        return out

    if scope == "list":
        ns = (data_type or 'datafields').lower()
        if ns not in PERSISTENT_NAMESPACES:
            return {'error': f'Unknown namespace {ns}', 'valid': list(PERSISTENT_NAMESPACES)}
        return {'scope': 'list', 'namespace': ns,
                'entries': await store.list_entries(ns, limit=max(1, max_entries))}

    if scope == "migrate":
        if not brain_client.redis_client:
            return {'error': 'Redis unavailable; nothing to migrate.'}
        moved, freed, failed = 0, 0, 0
        for ns in PERSISTENT_NAMESPACES:
            for rkey in list(brain_client.redis_client.scan_iter(f'{ns}:*', count=1000)):
                try:
                    raw = brain_client.redis_client.get(rkey)
                    if not raw:
                        continue
                    payload = json.loads(raw)
                    # Namespaced keys are "<ns>:<id-or-hash>"; the store keys
                    # datafields/datasets by the full hashed key, the rest by id.
                    suffix = rkey.split(':', 1)[-1]
                    key = rkey if ns in ('datafields', 'datasets') else suffix
                    if ns == 'operators':
                        key = 'all'
                        if isinstance(payload, dict) and 'payload' in payload:
                            payload = payload['payload']
                    if ns == 'alpha_details' and not brain_client._is_frozen_alpha(payload):
                        continue  # still mutable — leave it on its Redis TTL
                    size = brain_client.redis_client.memory_usage(rkey) or 0
                    await store.put(ns, key, payload)
                    brain_client.redis_client.delete(rkey)
                    moved += 1
                    freed += size
                except Exception:
                    failed += 1
        return {'scope': 'migrate', 'moved': moved, 'failed': failed,
                'redis_freed_mb': round(freed / 1048576, 2),
                'disk': await store.stats(PERSISTENT_NAMESPACES),
                'note': 'No platform requests were made.'}

    if scope == "operators":
        data = await brain_client.get_operators(force_refresh=True)
        return {'scope': 'operators', 'refreshed': True, 'operators': len(data or [])}

    if scope == "field_index":
        # Local reindex of the datafield descriptions. No platform requests.
        result = await brain_client.build_datafield_search_index()
        return {'scope': 'field_index', **result,
                'note': ('Full-text index over datafield descriptions (porter stemming). '
                         'Rebuild after build_datafield_catalogue adds configurations.')}

    if scope == "alphas":
        ids = [a for a in (alpha_ids or []) if a]
        if not ids:
            return {'error': 'scope="alphas" needs alpha_ids.'}
        if len(ids) > 50 and not confirm:
            return {'error': 'More than 50 alphas; pass confirm=True.', 'requested': len(ids)}
        async def _one(aid: str) -> Dict[str, Any]:
            row: Dict[str, Any] = {'alpha_id': aid}
            try:
                det = await brain_client.get_alpha_details(aid, force_refresh=True)
                row['stage'] = det.get('stage')
                row['stored_permanently'] = brain_client._is_frozen_alpha(det)
            except Exception as e:
                row['details_error'] = str(e)
            try:
                await brain_client.get_alpha_pnl(aid, force_refresh=True)
                row['pnl'] = 'refreshed'
            except Exception as e:
                row['pnl_error'] = str(e)
            return row
        return {'scope': 'alphas', 'results': list(await asyncio.gather(*[_one(a) for a in ids]))}

    if scope == "datafields_full":
        if not (region and universe and delay is not None):
            return {'error': 'scope="datafields_full" needs region, universe and delay.',
                    'hint': 'For every configuration at once use build_datafield_catalogue.'}
        datasets = await brain_client.get_datasets(None, region, delay, universe, 'false', None)
        drows = [x for x in (datasets.get('results') or []) if isinstance(x, dict) and x.get('id')]
        declared = sum(x.get('fieldCount') or 0 for x in drows)
        cfg = brain_client._df_config_key(instrument_type, region, delay, universe)
        stored, missing_fields = 0, 0
        for x in drows:
            if await store.get('datafields_ds', f"{cfg}:{x['id']}"):
                stored += 1
            else:
                missing_fields += x.get('fieldCount') or 0
        if not confirm:
            pages = missing_fields / 50.0
            return {
                'scope': 'datafields_full',
                'config': {'region': region, 'universe': universe, 'delay': delay},
                'datasets_total': len(drows),
                'datasets_already_stored': stored,
                'declared_fields': declared,
                'fields_still_missing': missing_fields,
                'estimated_requests': int(pages) + (len(drows) - stored),
                'estimated_minutes': round(pages / 27.0, 1),
                'note': ('An unfiltered sweep is capped at 10000 rows, so most datasets are '
                         'invisible in the normal catalogue. This walks each dataset individually. '
                         'Resumable. Pass confirm=True to run it in the foreground, or use '
                         'build_datafield_catalogue to run it in the background.'),
            }
        result = await brain_client.build_full_datafield_catalogue(
            instrument_type, region, delay, universe, force_refresh=False, progress=True)
        return {
            'scope': 'datafields_full',
            'config': {'region': region, 'universe': universe, 'delay': delay},
            'fields': result.get('count'),
            'declared_total': result.get('declared_total'),
            'coverage': result.get('coverage'),
            'datasets': result.get('datasets'),
            'incomplete_datasets': result.get('incomplete_datasets'),
        }

    if scope in ("datasets", "datafields"):
        explicit = bool(region and universe and delay is not None)
        targets: List[Dict[str, Any]] = []
        if explicit:
            targets = [{'region': region, 'universe': universe, 'delay': delay,
                        'instrumentType': instrument_type, 'data_type': data_type,
                        'dataset_id': dataset_id}]
        else:
            rows = await store.list_entries(scope)
            rows.sort(key=lambda r: r.get('fetched_at') or 0)  # stalest first
            for r in rows[:max(1, max_entries)]:
                kp = r.get('key_params') or {}
                if kp.get('region') and kp.get('universe'):
                    targets.append({
                        'region': kp.get('region'), 'universe': kp.get('universe'),
                        'delay': kp.get('delay'), 'instrumentType': kp.get('instrumentType', 'EQUITY'),
                        'data_type': kp.get('data_type', ''), 'dataset_id': kp.get('dataset_id'),
                    })
        if not targets:
            stored = len(await store.list_entries(scope))
            return {'scope': scope, 'refreshed': [], 'stored_entries': stored,
                    'note': ('Entries migrated out of Redis carry only a hashed key, so their '
                             'parameters are unknown until something reads them once. Pass '
                             'region/universe/delay explicitly to refresh a specific set.'
                             if stored else
                             'Nothing stored yet and no region/universe/delay given.')}

        # /data-fields is metered at 1 req/s; a single catalogue is ~200 pages.
        est = len(targets) * (200 if scope == 'datafields' else 3)
        if not confirm:
            return {'scope': scope, 'would_refresh': targets,
                    'estimated_requests': est,
                    'estimated_minutes': round(est / 30.0, 1),
                    'note': 'Dry run. Pass confirm=True to actually refetch.'}
        if scope == 'datafields' and len(targets) > 5:
            return {'error': 'Refusing to refresh more than 5 datafield catalogues at once '
                             f'(~{est} requests). Narrow it down or lower max_entries.'}

        refreshed = []
        for t in targets:
            try:
                if scope == 'datasets':
                    res = await brain_client.get_datasets(
                        region=t['region'], delay=t['delay'], universe=t['universe'],
                        force_refresh=True)
                else:
                    res = await brain_client.get_datafields(
                        instrument_type=t.get('instrumentType', 'EQUITY'), region=t['region'],
                        delay=t['delay'], universe=t['universe'],
                        dataset_id=t.get('dataset_id'), data_type=t.get('data_type', ''),
                        force_refresh=True)
                refreshed.append({**t, 'count': res.get('count')})
            except Exception as e:
                refreshed.append({**t, 'error': str(e)})
        return {'scope': scope, 'refreshed': refreshed}

    return {'error': f'Unknown scope {scope!r}',
            'valid': ['status', 'list', 'migrate', 'operators', 'datasets', 'datafields',
                      'datafields_full', 'field_index', 'alphas']}


@mcp.tool()
async def search_my_simulations(
    region: Optional[str] = None,
    universe: Optional[str] = None,
    delay: Optional[int] = None,
    neutralization: Optional[str] = None,
    contains: Optional[str] = None,
    min_sharpe: Optional[float] = None,
    min_fitness: Optional[float] = None,
    max_turnover: Optional[float] = None,
    sort: str = "sharpe",
    limit: int = 25,
) -> Dict[str, Any]:
    """Search the backtests this server has run, from the local ledger.

    Every simulation created through this server is recorded with its expression,
    settings and IS metrics, so your own research history is queryable without
    paging /users/self/alphas (which returns ~50 ms and 4 KB per row and is
    metered at 30 requests/minute). Zero platform requests.

    Args:
        region / universe / delay / neutralization: exact-match filters
        contains: substring the alpha expression must contain
        min_sharpe / min_fitness / max_turnover: metric thresholds
        sort: sharpe | fitness | turnover | returns | recent
        limit: rows to return (default 25)
    """
    try:
        rows = await brain_client.read_simulation_ledger()
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}
    total = len(rows)
    if not total:
        return {"count": 0, "results": [],
                "note": "No simulations recorded yet — the ledger fills as backtests run through this server."}

    def metric(r, name):
        v = (r.get("metrics") or {}).get(name)
        return v if isinstance(v, (int, float)) else None

    def keep(r):
        if region and str(r.get("region") or "").upper() != region.upper(): return False
        if universe and str(r.get("universe") or "").upper() != universe.upper(): return False
        if delay is not None and str(r.get("delay")) != str(delay): return False
        if neutralization and str(r.get("neutralization") or "").upper() != neutralization.upper(): return False
        if contains and contains.lower() not in str(r.get("expression") or "").lower(): return False
        if min_sharpe is not None and (metric(r, "sharpe") or -9e9) < min_sharpe: return False
        if min_fitness is not None and (metric(r, "fitness") or -9e9) < min_fitness: return False
        if max_turnover is not None and (metric(r, "turnover") or 9e9) > max_turnover: return False
        return True

    matched = [r for r in rows if keep(r)]
    sorters = {
        "sharpe": lambda r: -(metric(r, "sharpe") or -9e9),
        "fitness": lambda r: -(metric(r, "fitness") or -9e9),
        "returns": lambda r: -(metric(r, "returns") or -9e9),
        "turnover": lambda r: (metric(r, "turnover") if metric(r, "turnover") is not None else 9e9),
        "recent": lambda r: -(r.get("simulated_at") or 0),
    }
    matched.sort(key=sorters.get(sort, sorters["sharpe"]))

    facets = _facets(matched, {
        "region": lambda r: r.get("region"),
        "universe": lambda r: r.get("universe"),
        "neutralization": lambda r: r.get("neutralization"),
    })
    out = {
        "ledger_size": total,
        "count": len(matched),
        "returned": min(limit, len(matched)),
        "sort": sort,
        "facets": facets,
        "results": [{
            "alpha_id": r.get("alpha_id"),
            "expression": _truncate(r.get("expression"), 220),
            "region": r.get("region"), "universe": r.get("universe"),
            "delay": r.get("delay"), "neutralization": r.get("neutralization"),
            "metrics": {k: v for k, v in (r.get("metrics") or {}).items() if v is not None},
            "simulated_at": datetime.fromtimestamp(r.get("simulated_at") or 0).isoformat(),
        } for r in matched[:max(1, limit)]],
        "source": "local simulation ledger (0 platform requests)",
    }
    return out


@mcp.tool()
async def whats_new_in_data(
    region: str = "USA",
    universe: str = "TOP3000",
    delay: int = 1,
    since: Optional[str] = None,
    top_datasets: int = 15,
) -> Dict[str, Any]:
    """What data the platform has published recently, for one market configuration.

    BRAIN ships new datafields in monthly batches and stamps every field with
    ``dateCreated`` and every dataset with ``dateUpdated``. This reports the
    release timeline and what landed in the most recent batches, so you can tell
    at a glance whether there is anything new to research since you last looked.

    Runs entirely against the local permanent catalogue — zero platform requests.
    Refresh the catalogue first with sync_platform_cache(scope="datafields", ...)
    if you want to pick up a release that happened after it was last fetched.

    Args:
        region / universe / delay: market configuration
        since: only report on/after this date ("2026-03-01"). Defaults to the
               two most recent release batches.
        top_datasets: how many datasets to break the newest batch down by
    """
    try:
        fields = await brain_client.get_datafields(
            "EQUITY", region, delay, universe, "false", None, "", None, False)
        datasets = await brain_client.get_datasets(None, region, delay, universe, "false", None)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

    frows = [f for f in (fields.get("results") or []) if isinstance(f, dict)]
    drows = [x for x in (datasets.get("results") or []) if isinstance(x, dict)]
    if not frows:
        return {"error": "No catalogue stored for this configuration yet; call get_datafields once to fetch it."}

    def timeline(rows, key):
        counts = {}
        for r in rows:
            v = str(r.get(key) or "")[:10]
            if v:
                counts[v] = counts.get(v, 0) + 1
        return dict(sorted(counts.items(), reverse=True))

    field_timeline = timeline(frows, "dateCreated")
    dataset_timeline = timeline(drows, "dateUpdated")

    releases = list(field_timeline)
    if since:
        cutoff = str(since)[:10]
    else:
        # Default to the two most recent batches: enough to answer "anything new?"
        cutoff = releases[1] if len(releases) > 1 else (releases[0] if releases else "")

    new_fields = [f for f in frows if str(f.get("dateCreated") or "")[:10] >= cutoff]
    new_datasets = [x for x in drows if str(x.get("dateUpdated") or "")[:10] >= cutoff]

    by_dataset = {}
    for f in new_fields:
        ds = f.get("dataset")
        did = ds.get("id") if isinstance(ds, dict) else ds
        if did:
            by_dataset[did] = by_dataset.get(did, 0) + 1
    ranked = sorted(by_dataset.items(), key=lambda kv: -kv[1])[:max(1, top_datasets)]
    ds_names = {x.get("id"): x.get("name") for x in drows}

    return {
        "config": {"region": region, "universe": universe, "delay": delay},
        "since": cutoff,
        "catalogue_totals": {"datafields": len(frows), "datasets": len(drows)},
        "new_since": {"datafields": len(new_fields), "datasets": len(new_datasets)},
        "field_release_timeline": dict(list(field_timeline.items())[:12]),
        "dataset_update_timeline": dict(list(dataset_timeline.items())[:12]),
        "top_datasets_in_window": [
            {"dataset": did, "name": ds_names.get(did), "new_fields": n} for did, n in ranked
        ],
        "sample_new_fields": [
            {"id": f.get("id"), "dataset": (f.get("dataset") or {}).get("id")
                if isinstance(f.get("dataset"), dict) else None,
             "type": f.get("type"), "userCount": f.get("userCount"),
             "dateCreated": f.get("dateCreated"),
             "description": _truncate(f.get("description"), 120)}
            for f in sorted(new_fields, key=lambda f: -(f.get("userCount") or 0))[:15]
        ],
        "next_step": (f'get_datafields(region="{region}", universe="{universe}", delay={delay}, '
                      f'since="{cutoff}", sort="-dateCreated") to page the full list, or pass '
                      'dataset_id to drill into one of the datasets above.'),
        "source": "local permanent catalogue (0 platform requests)",
    }


@mcp.tool()
async def get_api_traffic_status() -> Dict[str, Any]:
    """Inspect how much BRAIN API traffic this server is generating and why.

    Reports, per endpoint family, the quota learned from the platform's own
    RateLimit headers, how many requests were sent in the last minute, and any
    active cooldown; plus the Redis cache footprint that is keeping requests
    off the wire. Use it to diagnose slow tools before assuming the platform is
    the bottleneck.
    """
    out: Dict[str, Any] = {
        'rate_limits': brain_client.rate_limiter.snapshot(),
        'max_concurrency': brain_client._max_concurrency,
        'auth_cached_for_seconds': round(
            max(0.0, brain_client._auth_validated_until - time.time()), 1
        ),
    }
    # Permanent tier: immutable platform data, never expires.
    out['permanent_store'] = await brain_client.store.stats(PERSISTENT_NAMESPACES)

    # Hot tier: only data that actually changes still lives in Redis.
    if brain_client.redis_client:
        try:
            counts: Dict[str, int] = {}
            for prefix in ('user_alphas', 'pyramid_alphas', 'platform_settings',
                           'alpha_details', 'alpha_pnl', 'datafields', 'datasets'):
                counts[prefix] = sum(
                    1 for _ in brain_client.redis_client.scan_iter(f'{prefix}:*', count=500)
                )
            info = brain_client.redis_client.info('memory')
            out['redis'] = {
                'keys_by_prefix': counts,
                'used_memory_human': info.get('used_memory_human'),
                'maxmemory_human': info.get('maxmemory_human'),
            }
            stale = sum(counts.get(ns, 0) for ns in PERSISTENT_NAMESPACES)
            if stale:
                out['redis']['hint'] = (
                    f'{stale} immutable entr(ies) still in Redis — run '
                    'sync_platform_cache(scope="migrate") to move them to disk.'
                )
        except Exception as e:
            out['redis'] = {'error': str(e)}
    else:
        out['redis'] = {'backend': None,
                        'warning': 'Redis unavailable — mutable lookups go to the platform.'}
    try:
        pools = sorted(Path(__file__).parent.joinpath('downloads').glob('os_pnl_pool*.pkl'))
        out['os_pnl_pools'] = {
            'files': len(pools),
            'bytes': sum(p.stat().st_size for p in pools),
        }
    except Exception:
        pass
    return out


@mcp.tool()
async def lookINTO_SimError_message(locations: Sequence[str]) -> dict:
    """
    Fetch and parse error/status from multiple simulation locations (URLs).
    Args:
        locations: List of simulation result URLs (e.g., /simulations/{id})
    Returns:
        List of dicts with location, error message, and raw response
    """
    results = []
    for loc in locations:
        try:
            resp = await brain_client._request('GET', loc)
            if resp.status_code != 200:
                results.append({
                    "location": loc,
                    "location_url": brain_client._to_absolute_url(loc),
                    "error": f"HTTP {resp.status_code}",
                    "status_code": resp.status_code,
                    "raw": brain_client._response_payload(resp)
                })
                continue
            data = resp.json() if resp.text else {}
            error_msg = brain_client._simulation_error_message(data)
            # If alpha ID is missing, include that info
            if not data.get("alpha") and error_msg == "Unknown error":
                error_msg = "Simulation did not get through; inspect raw/status for the platform response."
            extra_info = ""
            if error_msg and "does not support event inputs" in error_msg:
                extra_info = "Operator xxx does not support event inputs : If fields is vector type  should use vec_* operator with event input"
            results.append({
                "location": loc,
                "location_url": brain_client._to_absolute_url(loc),
                "error": error_msg,
                "status": data.get("status"),
                "alpha": data.get("alpha"),
                "raw": data,
                "extra_info": extra_info
            })
        except Exception as e:
            results.append({
                "location": loc,
                "error": str(e),
                "raw": None
            })
    return _slim_text_lookup({"results": results}, n=2000)

# --- Main entry point ---
if __name__ == "__main__":
    print("running the server", file=sys.stderr)
    
    # Validate critical environment setup
    config = load_config()
    creds = config.get("credentials", {})
    if not creds.get("email") or not creds.get("password"):
        print("[WARNING] No BRAIN credentials found in config. Authentication will fail until credentials are provided.", file=sys.stderr)
    
    # Verify Redis connectivity
    if brain_client.redis_client:
        print("[INFO] Redis connection established successfully", file=sys.stderr)
    else:
        print("[WARNING] Redis connection failed - caching disabled", file=sys.stderr)
    
    # Run using Streamable HTTP transport in container environment so the server remains
    # running and accessible over HTTP (not stdio which exits in non-interactive containers).
    try:
        mcp.run(transport='streamable-http')
    except TypeError:
        # Fallback if signature differs
        mcp.run('streamable-http')
