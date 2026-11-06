"""
图谱存储：SQLite，带公司 / 时间快照 / 来源 / 属性维度。

设计要点：
- 公司级实体（entities）：以规范化名 ekey 作为同一公司的实体身份，跨快照复用；
- 三元组（triplets）：每条都带 snapshot_id / crawl_time / source_url / 属性 JSON，
  同一公司不同时间爬取会各存一份，据此做变化分析；
- 属性是 JSON 存储的开放字典，不预设任何键名。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any

from . import config

_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def slugify(name: str) -> str:
    s = re.sub(r"[\s/\\:*?\"<>|（）()【】\[\]{}]+", "-", str(name)).strip("-")
    s = re.sub(r"-{2,}", "-", s).lower()
    return s or uuid.uuid4().hex[:10]


def norm_ekey(name: str) -> str:
    """实体规范化键：去空白、去尾部标点、统一小写（跨快照实体对齐）。"""
    s = str(name or "").strip()
    s = re.sub(r"\s+", "", s)
    s = s.strip("。；;，,、.．·—-_")
    return s.lower()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.GRAPH_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _lock, connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS companies(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              slug TEXT UNIQUE NOT NULL,
              name TEXT NOT NULL,
              created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS snapshots(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              company_id INTEGER NOT NULL,
              snapshot_id TEXT UNIQUE NOT NULL,
              crawl_time TEXT,
              note TEXT,
              page_count INTEGER DEFAULT 0,
              file_count INTEGER DEFAULT 0,
              chunk_count INTEGER DEFAULT 0,
              triplet_count INTEGER DEFAULT 0,
              llm TEXT,
              created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS entities(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              company_id INTEGER NOT NULL,
              ekey TEXT NOT NULL,
              name TEXT NOT NULL,
              type TEXT,
              types_json TEXT DEFAULT '[]',
              props_json TEXT DEFAULT '{}',
              first_snapshot TEXT,
              last_snapshot TEXT,
              updated_at TEXT,
              UNIQUE(company_id, ekey)
            );
            CREATE TABLE IF NOT EXISTS triplets(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              company_id INTEGER NOT NULL,
              snapshot_id TEXT NOT NULL,
              crawl_time TEXT,
              src_ekey TEXT NOT NULL,
              dst_ekey TEXT NOT NULL,
              src_name TEXT,
              dst_name TEXT,
              src_type TEXT,
              dst_type TEXT,
              relation TEXT NOT NULL,
              props_json TEXT DEFAULT '{}',
              source_url TEXT,
              source_title TEXT,
              source_kind TEXT DEFAULT 'page',
              created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_triplets_company ON triplets(company_id);
            CREATE INDEX IF NOT EXISTS idx_triplets_snap ON triplets(snapshot_id);
            CREATE INDEX IF NOT EXISTS idx_triplets_keys ON triplets(company_id, src_ekey, dst_ekey, relation);
            CREATE TABLE IF NOT EXISTS chunks(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              company_id INTEGER NOT NULL,
              snapshot_id TEXT NOT NULL,
              source_url TEXT,
              source_title TEXT,
              source_kind TEXT DEFAULT 'page',
              ord INTEGER DEFAULT 0,
              text TEXT,
              created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS raw_sources(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              company_id INTEGER NOT NULL,
              snapshot_id TEXT NOT NULL,
              kind TEXT,
              url TEXT,
              title TEXT,
              path TEXT,
              crawl_time TEXT,
              created_at TEXT
            );
            """
        )


init_db()


# ────────────────────────── 公司 / 快照 ──────────────────────────

def get_or_create_company(name: str) -> sqlite3.Row:
    slug = slugify(name)
    with _lock, connect() as conn:
        row = conn.execute("SELECT * FROM companies WHERE slug=?", (slug,)).fetchone()
        if row:
            if row["name"] != name:
                conn.execute("UPDATE companies SET name=? WHERE id=?", (name, row["id"]))
                row = conn.execute("SELECT * FROM companies WHERE slug=?", (slug,)).fetchone()
            return row
        conn.execute(
            "INSERT INTO companies(slug, name, created_at) VALUES(?,?,?)",
            (slug, name, now_iso()),
        )
        return conn.execute("SELECT * FROM companies WHERE slug=?", (slug,)).fetchone()


def create_snapshot(company_id: int, snapshot_id: str, crawl_time: str, note: str, llm_desc: str) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO snapshots(company_id, snapshot_id, crawl_time, note, llm, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (company_id, snapshot_id, crawl_time, note, llm_desc, now_iso()),
        )


def finalize_snapshot(snapshot_id: str, page_count: int, file_count: int, chunk_count: int, triplet_count: int) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE snapshots SET page_count=?, file_count=?, chunk_count=?, triplet_count=? WHERE snapshot_id=?",
            (page_count, file_count, chunk_count, triplet_count, snapshot_id),
        )


def list_companies() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.*, COUNT(DISTINCT s.snapshot_id) AS snapshot_count,"
            " (SELECT COUNT(*) FROM triplets t WHERE t.company_id=c.id) AS triplet_count,"
            " (SELECT COUNT(*) FROM entities e WHERE e.company_id=c.id) AS entity_count"
            " FROM companies c LEFT JOIN snapshots s ON s.company_id=c.id"
            " GROUP BY c.id ORDER BY c.id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def list_snapshots(company_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM snapshots WHERE company_id=? ORDER BY crawl_time DESC, id DESC",
            (company_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def latest_snapshot_id(company_id: int) -> str | None:
    snaps = list_snapshots(company_id)
    return snaps[0]["snapshot_id"] if snaps else None


def get_company_by_slug(slug: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM companies WHERE slug=?", (slug,)).fetchone()


def list_snapshots_by_slug(company_slug: str) -> list[dict]:
    comp = get_company_by_slug(company_slug)
    if not comp:
        return []
    return list_snapshots(int(comp["id"]))


# ────────────────────────── 实体 ──────────────────────────

def upsert_entity(
    company_id: int,
    name: str,
    etype: str | None,
    props: dict,
    snapshot_id: str,
) -> str:
    ekey = norm_ekey(name)
    if not ekey:
        return ""
    types = [t for t in {str(etype or "").strip(), } if t]
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT * FROM entities WHERE company_id=? AND ekey=?", (company_id, ekey)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO entities(company_id, ekey, name, type, types_json, props_json,"
                " first_snapshot, last_snapshot, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    company_id,
                    ekey,
                    name,
                    etype or "",
                    json.dumps(types, ensure_ascii=False),
                    json.dumps(props or {}, ensure_ascii=False),
                    snapshot_id,
                    snapshot_id,
                    now_iso(),
                ),
            )
        else:
            old_types = json.loads(row["types_json"] or "[]")
            merged_types = list(dict.fromkeys([row["type"] or "", *old_types, *(types or [])]))
            old_props = json.loads(row["props_json"] or "{}")
            merged_props = {**old_props, **(props or {})}
            conn.execute(
                "UPDATE entities SET name=?, type=?, types_json=?, props_json=?, last_snapshot=?, updated_at=?"
                " WHERE company_id=? AND ekey=?",
                (
                    name or row["name"],
                    etype or row["type"],
                    json.dumps(merged_types, ensure_ascii=False),
                    json.dumps(merged_props, ensure_ascii=False),
                    snapshot_id,
                    now_iso(),
                    company_id,
                    ekey,
                ),
            )
    return ekey


def entity_props_map(company_id: int) -> dict[str, dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT ekey, name, type, props_json FROM entities WHERE company_id=?", (company_id,)
        ).fetchall()
        return {
            r["ekey"]: {"name": r["name"], "type": r["type"], **json.loads(r["props_json"] or "{}")}
            for r in rows
        }


# ────────────────────────── 三元组 ──────────────────────────

def insert_triplet(
    company_id: int,
    snapshot_id: str,
    crawl_time: str,
    src_ekey: str,
    dst_ekey: str,
    src_name: str,
    dst_name: str,
    src_type: str,
    dst_type: str,
    relation: str,
    props: dict,
    source_url: str,
    source_title: str,
    source_kind: str,
) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO triplets(company_id, snapshot_id, crawl_time, src_ekey, dst_ekey,"
            " src_name, dst_name, src_type, dst_type, relation, props_json,"
            " source_url, source_title, source_kind, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                company_id,
                snapshot_id,
                crawl_time,
                src_ekey,
                dst_ekey,
                src_name,
                dst_name,
                src_type,
                dst_type,
                relation,
                json.dumps(props or {}, ensure_ascii=False),
                source_url,
                source_title,
                source_kind,
                now_iso(),
            ),
        )


def _triplet_rows(company_id: int, snapshot_id: str | None) -> list[sqlite3.Row]:
    with connect() as conn:
        if snapshot_id and snapshot_id != "all":
            return conn.execute(
                "SELECT * FROM triplets WHERE company_id=? AND snapshot_id=?", (company_id, snapshot_id)
            ).fetchall()
        return conn.execute("SELECT * FROM triplets WHERE company_id=?", (company_id,)).fetchall()


def select_triplets(company_id: int, snapshot_id: str | None) -> list[dict]:
    rows = _triplet_rows(company_id, snapshot_id)
    out: list[dict] = []
    seen: set[tuple] = set()
    for r in rows:  # 按 crawl_time 降序去重：同一 key 保留最新
        pass
    for r in sorted(rows, key=lambda x: (x["crawl_time"] or "", x["id"]), reverse=True):
        key = (r["src_ekey"], r["relation"], r["dst_ekey"])
        if snapshot_id != "all" and snapshot_id:
            out.append(dict(r))
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(r))
    return out


def diff_snapshots(company_id: int, snap_a: str, snap_b: str) -> dict:
    """对比两个快照的三元组：新增 / 移除 / 属性变化。"""

    def key_of(r: dict) -> tuple:
        return (r["src_name"] or r["src_ekey"], r["relation"], r["dst_name"] or r["dst_ekey"])

    ta = _triplet_rows(company_id, snap_a)
    tb = _triplet_rows(company_id, snap_b)
    # 每个快照内部按 key 去重（保留最新一条）
    def dedup(rows) -> dict[tuple, dict]:
        best: dict[tuple, dict] = {}
        for r in sorted(rows, key=lambda x: (x["crawl_time"] or "", x["id"]), reverse=True):
            k = key_of(r)
            if k not in best:
                best[k] = dict(r)
        return best

    ma, mb = dedup(ta), dedup(tb)
    added, removed, changed = [], [], []
    for k, r in mb.items():
        if k not in ma:
            added.append(r)
        else:
            if (json.loads(r["props_json"] or "{}") != json.loads(ma[k]["props_json"] or "{}")) or (
                (r["src_type"] or "") != (ma[k]["src_type"] or "")
                or (r["dst_type"] or "") != (ma[k]["dst_type"] or "")
            ):
                changed.append({"before": ma[k], "after": r})
    for k, r in ma.items():
        if k not in mb:
            removed.append(r)

    def slim(r: dict) -> dict:
        return {
            "src": r["src_name"],
            "src_type": r["src_type"],
            "relation": r["relation"],
            "dst": r["dst_name"],
            "dst_type": r["dst_type"],
            "props": json.loads(r["props_json"] or "{}"),
            "source_url": r["source_url"],
            "crawl_time": r["crawl_time"],
        }

    return {
        "snapshot_a": snap_a,
        "snapshot_b": snap_b,
        "added": [slim(r) for r in added],
        "removed": [slim(r) for r in removed],
        "changed": [
            {"before": slim(c["before"]), "after": slim(c["after"])} for c in changed
        ],
        "summary": {"added": len(added), "removed": len(removed), "changed": len(changed)},
    }


# ────────────────────────── chunks / raw ──────────────────────────

def insert_chunk(company_id: int, snapshot_id: str, source_url: str, source_title: str, source_kind: str, ord_: int, text: str) -> int:
    with _lock, connect() as conn:
        cur = conn.execute(
            "INSERT INTO chunks(company_id, snapshot_id, source_url, source_title, source_kind, ord, text, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (company_id, snapshot_id, source_url, source_title, source_kind, ord_, text, now_iso()),
        )
        return int(cur.lastrowid or 0)


def insert_raw_source(company_id: int, snapshot_id: str, kind: str, url: str, title: str, path: str, crawl_time: str) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO raw_sources(company_id, snapshot_id, kind, url, title, path, crawl_time, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (company_id, snapshot_id, kind, url, title, path, crawl_time, now_iso()),
        )


def count_chunks(company_id: int, snapshot_id: str) -> int:
    with connect() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM chunks WHERE company_id=? AND snapshot_id=?",
                (company_id, snapshot_id),
            ).fetchone()["c"]
        )


def entities_for_company(company_id: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM entities WHERE company_id=? ORDER BY name", (company_id,)
        ).fetchall()
