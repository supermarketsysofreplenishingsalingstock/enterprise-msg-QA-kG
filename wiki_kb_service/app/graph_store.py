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
        # 旧库迁移：chunks.processed（1=已抽取，0=待处理；历史行视为已处理）
        try:
            conn.execute("ALTER TABLE chunks ADD COLUMN processed INTEGER DEFAULT 1")
        except sqlite3.OperationalError:
            pass  # 列已存在
        # 旧库迁移：triplets.chunk_id（来源分块，用于「重新抽取选中的内容」精确替换；
        # 历史行该列为 NULL，重新抽取时按 snapshot+source_url 整源替换兜底）
        try:
            conn.execute("ALTER TABLE triplets ADD COLUMN chunk_id INTEGER")
        except sqlite3.OperationalError:
            pass  # 列已存在
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_triplets_chunk ON triplets(chunk_id)")
        except sqlite3.OperationalError:
            pass
        # 图片链路：images 登记表 + image_graph 入图记录；triplets.image_id 标记来源图片
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS images(
              id TEXT PRIMARY KEY,
              company_id INTEGER NOT NULL,
              name TEXT,
              url TEXT,
              file_path TEXT,
              width INTEGER,
              height INTEGER,
              size INTEGER,
              status TEXT DEFAULT 'pending',
              ocr_text TEXT DEFAULT '',
              ocr_model TEXT,
              ocr_at TEXT,
              description TEXT DEFAULT '',
              describe_model TEXT,
              describe_mode TEXT,
              described_at TEXT,
              snapshot_id TEXT,
              crawl_time TEXT,
              error TEXT,
              created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_images_company ON images(company_id, status);
            CREATE TABLE IF NOT EXISTS image_graph(
              company_id INTEGER NOT NULL,
              image_id TEXT NOT NULL,
              triplet_count INTEGER DEFAULT 0,
              status TEXT DEFAULT 'processed',
              processed_at TEXT,
              UNIQUE(company_id, image_id)
            );
            """
        )
        try:
            conn.execute("ALTER TABLE triplets ADD COLUMN image_id TEXT")
        except sqlite3.OperationalError:
            pass  # 列已存在
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_triplets_image ON triplets(image_id)")
        except sqlite3.OperationalError:
            pass


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
    chunk_id: int | None = None,
    image_id: str | None = None,
) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO triplets(company_id, snapshot_id, crawl_time, src_ekey, dst_ekey,"
            " src_name, dst_name, src_type, dst_type, relation, props_json,"
            " source_url, source_title, source_kind, chunk_id, image_id, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                chunk_id,
                image_id,
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

def insert_chunk(company_id: int, snapshot_id: str, source_url: str, source_title: str, source_kind: str, ord_: int, text: str, processed: int = 1) -> int:
    with _lock, connect() as conn:
        cur = conn.execute(
            "INSERT INTO chunks(company_id, snapshot_id, source_url, source_title, source_kind, ord, text, processed, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (company_id, snapshot_id, source_url, source_title, source_kind, ord_, text, processed, now_iso()),
        )
        return int(cur.lastrowid or 0)


# ────────────────────────── 待处理 chunk（延迟抽取） ──────────────────────────

def count_unprocessed_chunks(company_id: int) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE company_id=? AND processed=0", (company_id,)
        ).fetchone()
        return int(row["n"] or 0)


def get_unprocessed_chunks(company_id: int, limit: int = 500) -> list[dict]:
    """待处理 chunk，附带所属快照的 crawl_time（写三元组来源时间用）。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.id AS chunk_id, c.company_id, c.snapshot_id, c.source_url, c.source_title,"
            " c.source_kind, c.ord, c.text, COALESCE(s.crawl_time, '') AS crawl_time"
            " FROM chunks c LEFT JOIN snapshots s ON s.snapshot_id = c.snapshot_id"
            " WHERE c.company_id=? AND c.processed=0"
            " ORDER BY c.created_at, c.ord LIMIT ?",
            (company_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_chunks_processed(chunk_ids: list[int]) -> None:
    if not chunk_ids:
        return
    with _lock, connect() as conn:
        conn.executemany(
            "UPDATE chunks SET processed=1 WHERE id=?",
            [(int(i),) for i in chunk_ids],
        )


def mark_chunks_unprocessed(chunk_ids: list[int]) -> None:
    """把分块标回待处理（重新抽取失败时用，方便「处理新增内容」重试）。"""
    if not chunk_ids:
        return
    with _lock, connect() as conn:
        conn.executemany(
            "UPDATE chunks SET processed=0 WHERE id=?",
            [(int(i),) for i in chunk_ids],
        )


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


def chunks_with_text(company_id: int, limit: int = 2000) -> list[dict]:
    """取 chunk 全文（BM25 全文检索用；list_chunks 只返回 snippet）。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id AS chunk_id, snapshot_id, source_url, source_title, source_kind,"
            " ord, text FROM chunks WHERE company_id=?"
            " ORDER BY snapshot_id DESC, ord LIMIT ?",
            (company_id, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]


def entities_for_company(company_id: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM entities WHERE company_id=? ORDER BY name", (company_id,)
        ).fetchall()


# ────────────────────────── 重新抽取（选中 chunk） ──────────────────────────

def list_chunks(company_id: int, snapshot_id: str | None = None, limit: int = 1000) -> list[dict]:
    """列出可重新抽取的 chunk（带来源、快照时间、已产出三元组数）。"""
    sql = (
        "SELECT c.id AS chunk_id, c.snapshot_id, c.source_url, c.source_title, c.source_kind,"
        " c.ord, c.processed, length(c.text) AS text_len, substr(c.text, 1, 160) AS snippet,"
        " COALESCE(s.crawl_time, '') AS crawl_time,"
        " (SELECT COUNT(*) FROM triplets t WHERE t.chunk_id = c.id) AS triplet_count,"
        " (SELECT COUNT(*) FROM triplets t WHERE t.company_id = c.company_id"
        "   AND t.snapshot_id = c.snapshot_id"
        "   AND COALESCE(t.source_url,'') = COALESCE(c.source_url,'')) AS source_triplet_count"
        " FROM chunks c LEFT JOIN snapshots s ON s.snapshot_id = c.snapshot_id"
        " WHERE c.company_id=?"
    )
    args: list[Any] = [company_id]
    if snapshot_id and snapshot_id not in ("all", "latest"):
        sql += " AND c.snapshot_id=?"
        args.append(snapshot_id)
    sql += " ORDER BY s.crawl_time DESC, c.snapshot_id, c.ord LIMIT ?"
    args.append(int(limit))
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def get_chunks_by_ids(company_id: int, chunk_ids: list[int]) -> list[dict]:
    ids = [int(i) for i in (chunk_ids or []) if str(i).strip()]
    if not ids:
        return []
    ph = ",".join("?" for _ in ids)
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.id AS chunk_id, c.company_id, c.snapshot_id, c.source_url, c.source_title,"
            " c.source_kind, c.ord, c.text, COALESCE(s.crawl_time, '') AS crawl_time"
            f" FROM chunks c LEFT JOIN snapshots s ON s.snapshot_id = c.snapshot_id"
            f" WHERE c.company_id=? AND c.id IN ({ph}) ORDER BY c.ord",
            [company_id, *ids],
        ).fetchall()
        return [dict(r) for r in rows]


def triplet_counts_by_chunk(company_id: int, chunk_ids: list[int]) -> dict[int, int]:
    """每个 chunk 当前已产出的三元组条数（用于判断「重新抽取会不会丢东西」）。"""
    ids = [int(i) for i in (chunk_ids or []) if str(i).strip()]
    if not ids:
        return {}
    ph = ",".join("?" for _ in ids)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT chunk_id, COUNT(*) AS n FROM triplets WHERE company_id=?"
            f" AND chunk_id IN ({ph}) GROUP BY chunk_id",
            [company_id, *ids],
        ).fetchall()
        return {int(r["chunk_id"]): int(r["n"]) for r in rows}


def sources_fully_covered(company_id: int, chunks: list[dict]) -> set[tuple[str, str]]:
    """返回被选中 chunk 完整覆盖的 (snapshot_id, source_url) 集合。

    只有整源被选中时才允许顺带清理「没有 chunk_id 的历史三元组」，
    否则会误删同来源其他分块的数据。
    """
    if not chunks:
        return set()
    groups: dict[tuple[str, str], set[int]] = {}
    for c in chunks:
        groups.setdefault(
            (str(c["snapshot_id"]), str(c["source_url"] or "")), set()
        ).add(int(c["chunk_id"]))
    covered: set[tuple[str, str]] = set()
    with connect() as conn:
        for (sid, url), sel in groups.items():
            total = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM chunks WHERE company_id=? AND snapshot_id=?"
                    " AND COALESCE(source_url,'')=?",
                    (company_id, sid, url),
                ).fetchone()["n"]
            )
            if total and total == len(sel):
                covered.add((sid, url))
    return covered


def replace_chunk_triplets(
    company_id: int,
    chunk: dict,
    rows: list[dict],
    purge_null_source: bool = False,
) -> dict:
    """原子替换某个 chunk 的三元组：删除旧数据 + 写入新数据在同一个事务里。

    rows 由调用方构造（已含 src_ekey/dst_ekey 等），字段：
      snapshot_id, crawl_time, src_ekey, dst_ekey, src_name, dst_name,
      src_type, dst_type, relation, props, source_url, source_title, source_kind, chunk_id

    任何一步失败都整体回滚 —— 旧数据不会被删掉（这是「重新抽取把数据抽没了」的修复点）。
    """
    cid = int(chunk["chunk_id"])
    sid = str(chunk["snapshot_id"])
    url = str(chunk["source_url"] or "")
    inserted = len(rows)
    deleted = 0
    with _lock, connect() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "DELETE FROM triplets WHERE company_id=? AND chunk_id=?", (company_id, cid)
            )
            deleted += int(cur.rowcount or 0)
            if purge_null_source:
                cur = conn.execute(
                    "DELETE FROM triplets WHERE company_id=? AND snapshot_id=?"
                    " AND COALESCE(source_url,'')=? AND chunk_id IS NULL",
                    (company_id, sid, url),
                )
                deleted += int(cur.rowcount or 0)
            conn.executemany(
                "INSERT INTO triplets(company_id, snapshot_id, crawl_time, src_ekey, dst_ekey,"
                " src_name, dst_name, src_type, dst_type, relation, props_json,"
                " source_url, source_title, source_kind, chunk_id, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        company_id,
                        str(r.get("snapshot_id") or sid),
                        str(r.get("crawl_time") or now_iso()),
                        r["src_ekey"],
                        r["dst_ekey"],
                        r.get("src_name", ""),
                        r.get("dst_name", ""),
                        r.get("src_type", ""),
                        r.get("dst_type", ""),
                        r["relation"],
                        json.dumps(r.get("props") or {}, ensure_ascii=False),
                        r.get("source_url", url),
                        r.get("source_title", ""),
                        r.get("source_kind", "page"),
                        int(r.get("chunk_id") or cid),
                        now_iso(),
                    )
                    for r in rows
                ],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"inserted": inserted, "deleted": deleted}


def delete_triplets_for_chunks(company_id: int, chunks: list[dict]) -> int:
    """删除选中 chunk 产出的三元组（旧实现，仅为兼容保留；重新抽取已改走原子替换）。

    兼容历史数据：早期三元组没有 chunk_id（NULL）。若选中的 chunk 已覆盖某个
    (snapshot_id, source_url) 下的全部分块，则把该来源下 chunk_id 为 NULL 的旧三元组
    一并删除，避免重新抽取后新旧两套并存、图谱写重。
    """
    if not chunks:
        return 0
    ids = [int(c["chunk_id"]) for c in chunks]
    ph = ",".join("?" for _ in ids)
    deleted = 0
    with _lock, connect() as conn:
        cur = conn.execute(
            f"DELETE FROM triplets WHERE company_id=? AND chunk_id IN ({ph})",
            [company_id, *ids],
        )
        deleted += int(cur.rowcount or 0)

        # 覆盖整源时清理无 chunk_id 的历史三元组
        groups: dict[tuple[str, str], list[int]] = {}
        for c in chunks:
            groups.setdefault((str(c["snapshot_id"]), str(c["source_url"] or "")), []).append(
                int(c["chunk_id"])
            )
        for (sid, url), sel_ids in groups.items():
            total = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM chunks WHERE company_id=? AND snapshot_id=?"
                    " AND COALESCE(source_url,'')=?",
                    (company_id, sid, url),
                ).fetchone()["n"]
            )
            if total == 0 or total != len(set(sel_ids)):
                continue  # 只抽了该来源的一部分分块：无法归属的历史三元组不动
            cur = conn.execute(
                "DELETE FROM triplets WHERE company_id=? AND snapshot_id=?"
                " AND COALESCE(source_url,'')=? AND chunk_id IS NULL",
                (company_id, sid, url),
            )
            deleted += int(cur.rowcount or 0)
    return deleted


def delete_orphan_entities(company_id: int, keep_ekeys: list[str] | None = None) -> int:
    """删除不再被任何三元组引用的实体（重新抽取后残留的拼接实体等）。"""
    keep = [str(k) for k in (keep_ekeys or []) if k]
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT ekey FROM entities WHERE company_id=?"
            " AND ekey NOT IN (SELECT src_ekey FROM triplets WHERE company_id=?)"
            " AND ekey NOT IN (SELECT dst_ekey FROM triplets WHERE company_id=?)",
            (company_id, company_id, company_id),
        ).fetchall()
        targets = [r["ekey"] for r in rows if r["ekey"] not in keep]
        if targets:
            conn.executemany(
                "DELETE FROM entities WHERE company_id=? AND ekey=?",
                [(company_id, k) for k in targets],
            )
    return len(targets)


def recount_snapshot_triplets(company_id: int, snapshot_id: str) -> int:
    with _lock, connect() as conn:
        n = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM triplets WHERE company_id=? AND snapshot_id=?",
                (company_id, snapshot_id),
            ).fetchone()["n"]
        )
        conn.execute(
            "UPDATE snapshots SET triplet_count=? WHERE snapshot_id=?", (n, snapshot_id)
        )
    return n


def snapshot_ids_of_company(company_id: int) -> list[str]:
    with connect() as conn:
        return [r["snapshot_id"] for r in conn.execute(
            "SELECT snapshot_id FROM snapshots WHERE company_id=? ORDER BY crawl_time", (company_id,)
        ).fetchall()]


# ────────────────────────── 图片链路（登记 / 状态 / 入图记录） ──────────────────────────

def insert_image(
    image_id: str,
    company_id: int,
    name: str,
    url: str,
    file_path: str,
    width: int | None,
    height: int | None,
    size: int,
    snapshot_id: str | None,
    crawl_time: str | None,
) -> bool:
    """登记一张图片；同公司内容重复（同 sha1）返回 False。"""
    with _lock, connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO images(id, company_id, name, url, file_path, width, height,"
            " size, status, snapshot_id, crawl_time, created_at)"
            " VALUES(?,?,?,?,?,?,?,?, 'pending', ?, ?, ?)",
            (image_id, company_id, name, url, file_path, width, height, size,
             snapshot_id, crawl_time, now_iso()),
        )
        return cur.rowcount > 0


def list_images(company_id: int, statuses: list[str] | None = None) -> list[dict]:
    q = "SELECT * FROM images WHERE company_id=?"
    args: list = [company_id]
    if statuses:
        q += " AND status IN (%s)" % ",".join("?" * len(statuses))
        args += list(statuses)
    q += " ORDER BY created_at DESC, id"
    with connect() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def get_image(company_id: int, image_id: str) -> dict | None:
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM images WHERE company_id=? AND id=?", (company_id, image_id)
        ).fetchone()
        return dict(r) if r else None


def update_image(image_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock, connect() as conn:
        conn.execute(f"UPDATE images SET {cols} WHERE id=?", (*fields.values(), image_id))


def image_stats(company_id: int) -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN status='processed' THEN 1 ELSE 0 END) AS processed,"
            " SUM(CASE WHEN status IN ('pending','failed','processing') THEN 1 ELSE 0 END) AS pending,"
            " SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed"
            " FROM images WHERE company_id=?",
            (company_id,),
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "processed": int(row["processed"] or 0),
        "pending": int(row["pending"] or 0),
        "failed": int(row["failed"] or 0),
    }


def mark_image_in_graph(company_id: int, image_id: str, triplet_count: int = 0) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO image_graph(company_id, image_id, triplet_count, status, processed_at)"
            " VALUES(?,?,?, 'processed', ?)"
            " ON CONFLICT(company_id, image_id) DO UPDATE SET"
            " triplet_count=excluded.triplet_count, status='processed',"
            " processed_at=excluded.processed_at",
            (company_id, image_id, triplet_count, now_iso()),
        )


def image_in_graph_ids(company_id: int) -> set[str]:
    with connect() as conn:
        return {
            r["image_id"]
            for r in conn.execute(
                "SELECT image_id FROM image_graph WHERE company_id=? AND status='processed'",
                (company_id,),
            ).fetchall()
        }


def delete_image_triplets(company_id: int, image_ids: list[str]) -> int:
    """删除指定图片此前入图产生的三元组（重新处理前调用）。"""
    if not image_ids:
        return 0
    n = 0
    with _lock, connect() as conn:
        for iid in image_ids:
            cur = conn.execute(
                "DELETE FROM triplets WHERE company_id=? AND image_id=?", (company_id, iid)
            )
            n += cur.rowcount
        conn.execute(
            "DELETE FROM image_graph WHERE company_id=? AND image_id IN (%s)"
            % ",".join("?" * len(image_ids)),
            (company_id, *image_ids),
        )
    return n
