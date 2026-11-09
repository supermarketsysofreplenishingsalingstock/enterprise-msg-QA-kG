"""
Ingest 编排：LLM Wiki ingest 与图谱关系分析一体完成。

流程（每次爬取 -> 一个快照）：
 1. 接收爬虫回传：页面列表 + 已下载文件列表
 2. 文件解析（pdf/docx/txt/csv/html -> 文本）
 3. 原始资料层落盘（wiki/raw/<snapshot>/*.md，只读存档）
 4. 分 chunk -> DynamicLLMPathExtractor 开放式抽取（实体/关系/属性，不预设 schema）
 5. 三元组写入 SQLite 图谱（带快照/时间/来源/属性）
 6. Wiki 层更新：公司总览页 + 实体页（LLM 摘要 + 图谱数据渲染）+ index.md + log.md
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from . import config, graph_store, jobs, wiki
from .extractor import chunk_text, extract_from_chunk, is_meaningful_triplet
from .llm_factory import get_llm, config as cfg

FILE_TEXT_MAX = 400_000  # 单文件解析文本上限
PAGE_TEXT_MAX = 30_000  # 单页文本上限


def new_snapshot_id() -> str:
    return f"snap_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"


# ────────────────────────── 文本解析 ──────────────────────────

def html_to_text(html: str) -> str:
    try:
        soup = BeautifulSoup(html or "", "lxml")
    except Exception:
        soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)[:PAGE_TEXT_MAX]


def parse_file_to_text(path: str, content_type: str = "") -> str:
    p = Path(path)
    if not p.exists() or not p.is_file():
        return ""
    suffix = p.suffix.lower()
    try:
        if suffix == ".pdf":
            return _pdf_text(p)
        if suffix in (".docx",):
            return _docx_text(p)
        if suffix in (".txt", ".csv", ".md", ".log", ".json", ".xml", ".html", ".htm"):
            raw = p.read_text(encoding="utf-8", errors="ignore")
            if suffix in (".html", ".htm"):
                raw = html_to_text(raw)
            return raw[:FILE_TEXT_MAX]
        if suffix in (".xlsx", ".xls"):
            return _excel_text(p)
        # 其余（zip 等）暂不解析正文，仅保留原始文件信息
        return ""
    except Exception as e:  # noqa: BLE001
        return f"（文件解析失败：{e}）"


def _pdf_text(p: Path) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(p))
        parts = []
        for i, page in enumerate(reader.pages[:200]):
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(parts)[:FILE_TEXT_MAX]
    except ImportError:
        return "（未安装 pypdf，无法解析 PDF 正文）"


def _docx_text(p: Path) -> str:
    try:
        import docx  # type: ignore

        d = docx.Document(str(p))
        parts = [para.text for para in d.paragraphs if para.text.strip()]
        for table in d.tables[:50]:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    parts.append(" | ".join(cells))
        return "\n".join(parts)[:FILE_TEXT_MAX]
    except ImportError:
        return "（未安装 python-docx，无法解析 docx 正文）"


def _excel_text(p: Path) -> str:
    try:
        from openpyxl import load_workbook  # type: ignore

        wb = load_workbook(str(p), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets[:10]:
            parts.append(f"# Sheet: {ws.title}")
            for row in ws.iter_rows(max_row=200, values_only=True):
                vals = [str(v) for v in row if v is not None]
                if vals:
                    parts.append(" | ".join(vals))
        return "\n".join(parts)[:FILE_TEXT_MAX]
    except ImportError:
        return "（未安装 openpyxl，无法解析 excel 正文）"


# ────────────────────────── 抽取 -> 入图（共用） ──────────────────────────

async def _extract_chunk_to_graph(
    *,
    company_id: int,
    snapshot_id: str,
    crawl_time: str,
    chunk_id: int | None,
    text: str,
    source_url: str,
    source_title: str,
    source_kind: str,
    errors: list[str],
) -> int:
    """对单个 chunk 跑抽取并写入图谱（实体 upsert + 三元组），返回写入条数。

    三元组带 chunk_id，便于「重新抽取选中的内容」时精确替换旧数据。
    """
    res = await extract_from_chunk(text or "")
    if res.get("error"):
        errors.append(f"chunk {chunk_id}: {res['error']}")
        return 0
    n = 0
    for t in res["triplets"]:
        if not is_meaningful_triplet(t):
            continue
        src_ekey = graph_store.upsert_entity(
            company_id, t["head"], t["head_type"], t["head_props"], snapshot_id
        )
        dst_ekey = graph_store.upsert_entity(
            company_id, t["tail"], t["tail_type"], t["tail_props"], snapshot_id
        )
        if not src_ekey or not dst_ekey:
            continue
        graph_store.insert_triplet(
            company_id=company_id,
            snapshot_id=snapshot_id,
            crawl_time=crawl_time,
            src_ekey=src_ekey,
            dst_ekey=dst_ekey,
            src_name=t["head"],
            dst_name=t["tail"],
            src_type=t["head_type"],
            dst_type=t["tail_type"],
            relation=t["relation"],
            props={**t["relation_props"]},
            source_url=source_url,
            source_title=source_title,
            source_kind=source_kind,
            chunk_id=chunk_id,
        )
        n += 1
    return n


# ────────────────────────── Ingest 主流程 ──────────────────────────

async def run_ingest(payload: dict) -> dict:
    company_name = str(payload.get("company") or "").strip()
    if not company_name:
        raise ValueError("payload.company 不能为空")

    pages = [p for p in (payload.get("pages") or []) if isinstance(p, dict)]
    files = [f for f in (payload.get("files") or []) if isinstance(f, dict)]
    crawl_time = str(payload.get("crawl_time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    note = str(payload.get("note") or "")
    dry_run = bool(payload.get("dry_run"))
    # extract=False：只存原始资料 + chunk（标记待处理），不做 LLM 抽取；
    # 之后可在图谱页点「处理新增内容」补做抽取。
    do_extract = (not dry_run) and bool(payload.get("extract", True))

    company = graph_store.get_or_create_company(company_name)
    company_id = int(company["id"])
    company_slug = str(company["slug"])
    snapshot_id = str(payload.get("snapshot_id") or new_snapshot_id())
    llm_desc = json.dumps(config.describe_llm(), ensure_ascii=False)

    graph_store.create_snapshot(company_id, snapshot_id, crawl_time, note, llm_desc)

    # 1) 收集全部原始文本来源
    sources: list[dict] = []  # {kind,url,title,text}
    for pg in pages:
        text = str(pg.get("text") or "").strip()
        if not text and pg.get("html"):
            text = html_to_text(str(pg.get("html")))
        if not text:
            text = str(pg.get("excerpt") or "").strip()
        if not text:
            continue
        sources.append(
            {
                "kind": "page",
                "url": str(pg.get("url") or ""),
                "title": str(pg.get("title") or pg.get("url") or "未命名页面"),
                "text": text[:PAGE_TEXT_MAX],
            }
        )
    for fl in files:
        path = str(fl.get("path") or "")
        text = parse_file_to_text(path, str(fl.get("content_type") or ""))
        sources.append(
            {
                "kind": "file",
                "url": str(fl.get("url") or ""),
                "title": str(fl.get("title") or Path(path).name if path else "未命名文件"),
                "text": text[:FILE_TEXT_MAX] if text else f"（文件：{path}，未解析出正文）",
            }
        )

    # 2) 原始资料层落盘 + 登记
    for s in sources:
        p = wiki.save_raw_source(company_slug, snapshot_id, s["kind"], s["url"], s["title"], s["text"], crawl_time)
        graph_store.insert_raw_source(company_id, snapshot_id, s["kind"], s["url"], s["title"], str(p), crawl_time)

    # 3) 分 chunk 登记（未抽取时标记 processed=0，供「处理新增内容」补做）
    chunk_rows: list[dict] = []
    ord_i = 0
    chunk_processed_flag = 1 if do_extract else 0
    for s in sources:
        for piece in chunk_text(s["text"]):
            cid = graph_store.insert_chunk(
                company_id, snapshot_id, s["url"], s["title"], s["kind"], ord_i, piece,
                processed=chunk_processed_flag,
            )
            chunk_rows.append(
                {
                    "chunk_id": cid,
                    "url": s["url"],
                    "title": s["title"],
                    "kind": s["kind"],
                    "text": piece,
                }
            )
            ord_i += 1

    # 4) 开放式抽取（不预设 schema；保留全部三元组；属性必提）
    triplet_count = 0
    errors: list[str] = []
    if do_extract:
        for cr in chunk_rows:
            triplet_count += await _extract_chunk_to_graph(
                company_id=company_id,
                snapshot_id=snapshot_id,
                crawl_time=crawl_time,
                chunk_id=cr["chunk_id"],
                text=cr["text"],
                source_url=cr["url"],
                source_title=cr["title"],
                source_kind=cr["kind"],
                errors=errors,
            )

    file_count = sum(1 for s in sources if s["kind"] == "file")
    page_count = sum(1 for s in sources if s["kind"] == "page")
    graph_store.finalize_snapshot(
        snapshot_id, page_count, file_count, len(chunk_rows), triplet_count
    )

    # 5) Wiki 层更新（与图谱抽取同流程完成；dry_run / 延迟抽取时不调 LLM）
    if do_extract:
        wiki_stats = await regenerate_wiki_pages(
            company_slug, company_name, company_id, snapshot_id, with_summary=True
        )
    else:
        wiki_stats = {"pages_written": 0, "summaries": 0}

    wiki.append_log(
        company_slug,
        f"ingest 快照 {snapshot_id}：来源 页面{page_count}/文件{file_count}，chunk {len(chunk_rows)}，"
        + (
            f"新抽取三元组 {triplet_count}，更新实体页 {wiki_stats['pages_written']}"
            if do_extract
            else f"已存为待处理内容（未抽取，待处理 chunk {len(chunk_rows)}）"
        )
        + f"；LLM={llm_desc}"
        + (f"；抽取错误 {len(errors)} 条" if errors else ""),
    )
    wiki.rebuild_index(company_slug, company_name)

    return {
        "ok": True,
        "company": company_name,
        "company_slug": company_slug,
        "snapshot_id": snapshot_id,
        "crawl_time": crawl_time,
        "page_count": page_count,
        "file_count": file_count,
        "chunk_count": len(chunk_rows),
        "triplet_count": triplet_count,
        "wiki_pages_updated": wiki_stats["pages_written"],
        "llm_summaries": wiki_stats["summaries"],
        "extraction_errors": errors[:10],
        "dry_run": dry_run,
        "extracted": do_extract,
        "pending_chunks": 0 if do_extract else len(chunk_rows),
    }


# ────────────────────────── 处理待抽取内容 ──────────────────────────

async def run_process_pending(payload: dict, progress=None) -> dict:
    """处理指定公司尚未抽取的 chunk（图谱页「处理新增内容」按钮）。"""
    _p = progress or (lambda **_kw: None)
    company_raw = str(payload.get("company") or "").strip()
    if not company_raw:
        raise ValueError("payload.company 不能为空")
    company = graph_store.get_company_by_slug(graph_store.slugify(company_raw))
    if not company:
        raise ValueError(f"公司不存在：{company_raw}")
    company_id = int(company["id"])
    company_name = str(company["name"])
    company_slug = str(company["slug"])
    limit = int(payload.get("limit") or 500)

    chunks = graph_store.get_unprocessed_chunks(company_id, limit=limit)
    if not chunks:
        return {
            "ok": True,
            "company": company_name,
            "company_slug": company_slug,
            "chunk_count": 0,
            "triplet_count": 0,
            "wiki_pages_updated": 0,
            "extraction_errors": [],
            "message": "没有待处理的内容",
        }

    triplet_count = 0
    errors: list[str] = []
    done_ids: list[int] = []
    total = len(chunks)
    async with jobs.EXTRACT_LOCK:
        for i, cr in enumerate(chunks):
            _p(done=i, total=total, stage="LLM 抽取中", current=f"chunk #{cr['chunk_id']}")
            crawl_time = str(cr.get("crawl_time") or graph_store.now_iso())
            err_before = len(errors)
            n = await _extract_chunk_to_graph(
                company_id=company_id,
                snapshot_id=cr["snapshot_id"],
                crawl_time=crawl_time,
                chunk_id=int(cr["chunk_id"]),
                text=cr["text"] or "",
                source_url=cr["source_url"],
                source_title=cr["source_title"],
                source_kind=cr["source_kind"],
                errors=errors,
            )
            if len(errors) > err_before:
                continue  # 失败的 chunk 保持待处理，下次可重试
            triplet_count += n
            done_ids.append(int(cr["chunk_id"]))

    _p(done=total, total=total, stage="重建 Wiki 页面", current="")
    graph_store.mark_chunks_processed(done_ids)

    # 快照三元组计数回填 + Wiki 重建
    for sid in {c["snapshot_id"] for c in chunks}:
        with graph_store.connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM triplets WHERE company_id=? AND snapshot_id=?",
                (company_id, sid),
            ).fetchone()
            conn.execute(
                "UPDATE snapshots SET triplet_count=? WHERE snapshot_id=?", (int(n["n"]), sid)
            )
    latest = graph_store.latest_snapshot_id(company_id) or chunks[0]["snapshot_id"]
    wiki_stats = await regenerate_wiki_pages(company_slug, company_name, company_id, latest, with_summary=True)

    wiki.append_log(
        company_slug,
        f"处理新增内容：抽取 chunk {len(done_ids)}/{len(chunks)}，新增三元组 {triplet_count}，"
        f"更新实体页 {wiki_stats['pages_written']}"
        + (f"；抽取错误 {len(errors)} 条（保留待处理，可重试）" if errors else ""),
    )
    wiki.rebuild_index(company_slug, company_name)

    return {
        "ok": True,
        "company": company_name,
        "company_slug": company_slug,
        "chunk_count": len(done_ids),
        "triplet_count": triplet_count,
        "wiki_pages_updated": wiki_stats["pages_written"],
        "remaining": graph_store.count_unprocessed_chunks(company_id),
        "extraction_errors": errors[:10],
    }


# ────────────────────────── 重新抽取（用户选中的已有内容） ──────────────────────────

def _triplet_rows(
    *,
    company_id: int,
    snapshot_id: str,
    crawl_time: str,
    chunk_id: int | None,
    triplets: list[dict],
    source_url: str,
    source_title: str,
    source_kind: str,
) -> list[dict]:
    """把抽取结果转成可直接写库的行（顺带 upsert 实体），不做任何删除动作。

    同一 chunk 内 (src, dst, relation) 完全相同的重复三元组只保留第一条，
    避免 LLM 对重复罗列的原文抽出重复边把图谱画乱（跨 chunk 的重复不算，
    那是多来源佐证同一事实，属正常现象）。
    """
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for t in triplets:
        if not is_meaningful_triplet(t):
            continue
        src_ekey = graph_store.upsert_entity(
            company_id, t["head"], t["head_type"], t["head_props"], snapshot_id
        )
        dst_ekey = graph_store.upsert_entity(
            company_id, t["tail"], t["tail_type"], t["tail_props"], snapshot_id
        )
        if not src_ekey or not dst_ekey:
            continue
        dedup_key = (str(src_ekey), str(dst_ekey), str(t["relation"]))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        rows.append(
            {
                "snapshot_id": snapshot_id,
                "crawl_time": crawl_time,
                "src_ekey": src_ekey,
                "dst_ekey": dst_ekey,
                "src_name": t["head"],
                "dst_name": t["tail"],
                "src_type": t["head_type"],
                "dst_type": t["tail_type"],
                "relation": t["relation"],
                "props": {**t["relation_props"]},
                "source_url": source_url,
                "source_title": source_title,
                "source_kind": source_kind,
                "chunk_id": chunk_id,
            }
        )
    return rows


async def run_reextract(payload: dict, progress=None) -> dict:
    """对用户选中的 chunk 重新抽取 —— **非破坏式**。

    关键顺序（这是「重新抽取把数据抽没了」的修复点）：
      1. 先把所有选中分块跑完 LLM 抽取，结果只放在内存里，旧数据一条都不动；
      2. 只有某个分块确实抽出了新三元组，才在**同一个事务**里删旧 + 插新；
      3. 全部失败 → 直接报错，数据库保持原样；
         某个分块抽成空 → 也保留旧数据（只记 warning），绝不出现「旧的没了新的没来」。
    """
    _p = progress or (lambda **_kw: None)
    company_raw = str(payload.get("company") or "").strip()
    if not company_raw:
        raise ValueError("payload.company 不能为空")
    company = graph_store.get_company_by_slug(graph_store.slugify(company_raw))
    if not company:
        raise ValueError(f"公司不存在：{company_raw}")
    company_id = int(company["id"])
    company_name = str(company["name"])
    company_slug = str(company["slug"])

    chunk_ids = [int(i) for i in (payload.get("chunk_ids") or []) if str(i).strip()]
    if not chunk_ids:
        raise ValueError("chunk_ids 不能为空（请在页面上勾选要重新抽取的内容）")

    chunks = graph_store.get_chunks_by_ids(company_id, chunk_ids)
    if not chunks:
        raise ValueError("选中的内容不存在，或不属于该公司")
    missing = sorted(set(chunk_ids) - {int(c["chunk_id"]) for c in chunks})

    all_ids = [int(c["chunk_id"]) for c in chunks]
    old_counts = graph_store.triplet_counts_by_chunk(company_id, all_ids)
    covered = graph_store.sources_fully_covered(company_id, chunks)
    total = len(chunks)

    errors: list[str] = []
    warnings: list[str] = []
    staged: list[tuple[dict, list[dict]]] = []

    async with jobs.EXTRACT_LOCK:
        # ── 阶段 1：只抽取，不写库 ──
        for i, cr in enumerate(chunks):
            cid = int(cr["chunk_id"])
            _p(
                done=i,
                total=total,
                stage="LLM 抽取中（旧数据尚未改动）",
                current=f"chunk #{cid}",
            )
            res = await extract_from_chunk(cr["text"] or "")
            if res.get("error"):
                errors.append(f"chunk {cid}: {res['error']}")
                continue
            staged.append((cr, res["triplets"]))

        if not staged:
            _p(done=0, total=total, stage="全部失败，已保留原有数据")
            raise ValueError(
                "全部内容抽取失败，原有数据已完整保留（未删除任何三元组）。"
                + (f"首个错误：{errors[0]}" if errors else "")
            )

        # ── 阶段 2：有结果才替换（同事务） ──
        replaced_ids: list[int] = []
        empty_kept: list[int] = []
        attempted_ids: list[int] = []
        new_total = 0
        deleted_total = 0
        for i, (cr, triplets) in enumerate(staged):
            cid = int(cr["chunk_id"])
            _p(
                done=total - len(staged) + i,
                total=total,
                stage="写回图谱（同事务替换）",
                current=f"chunk #{cid}",
            )
            sid = str(cr["snapshot_id"])
            crawl_time = str(cr.get("crawl_time") or graph_store.now_iso())
            rows = _triplet_rows(
                company_id=company_id,
                snapshot_id=sid,
                crawl_time=crawl_time,
                chunk_id=cid,
                triplets=triplets,
                source_url=cr["source_url"],
                source_title=cr["source_title"],
                source_kind=cr["source_kind"],
            )
            if not rows:
                if old_counts.get(cid):
                    # 新结果为空但旧数据有内容 → 保留旧数据，宁可不动
                    empty_kept.append(cid)
                    warnings.append(
                        f"chunk {cid}: 本次抽取结果为空，已保留原有 {old_counts[cid]} 条三元组"
                    )
                else:
                    attempted_ids.append(cid)
                continue
            info = graph_store.replace_chunk_triplets(
                company_id,
                cr,
                rows,
                purge_null_source=(sid, str(cr["source_url"] or "")) in covered,
            )
            deleted_total += int(info["deleted"])
            new_total += int(info["inserted"])
            replaced_ids.append(cid)

    # ── 阶段 3：分块处理状态 + 孤立实体清理 + Wiki 重建 ──
    graph_store.mark_chunks_processed([*replaced_ids, *attempted_ids, *empty_kept])
    failed_ids = [
        int(c["chunk_id"])
        for c in chunks
        if int(c["chunk_id"])
        not in set(replaced_ids) | set(attempted_ids) | set(empty_kept)
    ]
    # 抽取失败的分块：有旧数据就保持「已处理」（数据还在），没数据才标回待处理
    graph_store.mark_chunks_unprocessed([i for i in failed_ids if not old_counts.get(i)])
    graph_store.mark_chunks_processed([i for i in failed_ids if old_counts.get(i)])
    for i in failed_ids:
        if old_counts.get(i):
            warnings.append(f"chunk {i}: 抽取失败，已保留原有 {old_counts[i]} 条三元组")

    # 只有在「全部成功且都有新数据」时才清孤立实体，避免失败分块的实体被误删
    orphan_removed = 0
    if not errors and not empty_kept:
        orphan_removed = graph_store.delete_orphan_entities(
            company_id, keep_ekeys=[graph_store.norm_ekey(company_name)]
        )

    for sid in {str(c["snapshot_id"]) for c in chunks}:
        graph_store.recount_snapshot_triplets(company_id, sid)

    _p(done=total, total=total, stage="重建 Wiki 页面", current="")
    latest = graph_store.latest_snapshot_id(company_id) or str(chunks[0]["snapshot_id"])
    wiki_stats = await regenerate_wiki_pages(
        company_slug, company_name, company_id, latest, with_summary=not errors
    )

    wiki.append_log(
        company_slug,
        f"重新抽取：选中 chunk {len(chunks)} 个（替换成功 {len(replaced_ids)}），"
        f"删除旧三元组 {deleted_total}，新抽取三元组 {new_total}，"
        f"清理孤立实体 {orphan_removed}，更新实体页 {wiki_stats['pages_written']}"
        + (
            f"，清理失效页 {len(wiki_stats.get('pages_removed') or [])}"
            if wiki_stats.get("pages_removed")
            else ""
        )
        + (f"；抽取错误 {len(errors)} 条（旧数据已保留）" if errors else "")
        + (f"；保留旧数据 {len(warnings)} 处" if warnings else ""),
    )
    wiki.rebuild_index(company_slug, company_name)

    return {
        "ok": True,
        "company": company_name,
        "company_slug": company_slug,
        "chunk_count": len(replaced_ids),
        "selected_chunks": len(chunks),
        "deleted_triplets": deleted_total,
        "triplet_count": new_total,
        "orphan_entities_removed": orphan_removed,
        "wiki_pages_updated": wiki_stats["pages_written"],
        "remaining": graph_store.count_unprocessed_chunks(company_id),
        "errors": errors[:10],
        "warnings": warnings[:10],
        "preserved_chunks": [*failed_ids, *empty_kept],
        "missing_chunk_ids": missing,
    }


# ────────────────────────── Wiki 页面生成 ──────────────────────────

def _relations_of_entity(company_id: int, ekey: str, snapshot_id: str | None) -> list[dict]:
    triplets = graph_store.select_triplets(company_id, snapshot_id or "all")
    rels = []
    seen = set()
    for t in triplets:
        if t["src_ekey"] == ekey:
            key = ("out", t["relation"], t["dst_ekey"])
            if key in seen:
                continue
            seen.add(key)
            rels.append(
                {
                    "direction": "→",
                    "relation": t["relation"],
                    "other": t["dst_name"],
                    "props": json.loads(t["props_json"] or "{}"),
                    "snapshot_id": t["snapshot_id"],
                }
            )
        elif t["dst_ekey"] == ekey:
            key = ("in", t["relation"], t["src_ekey"])
            if key in seen:
                continue
            seen.add(key)
            rels.append(
                {
                    "direction": "←",
                    "relation": t["relation"],
                    "other": t["src_name"],
                    "props": json.loads(t["props_json"] or "{}"),
                    "snapshot_id": t["snapshot_id"],
                }
            )
    return rels


def _sources_of_entity(company_id: int, ekey: str) -> list[dict]:
    from . import graph_store as gs

    with gs.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT source_url, source_title, crawl_time FROM triplets"
            " WHERE company_id=? AND (src_ekey=? OR dst_ekey=?) AND source_url != '' LIMIT 30",
            (company_id, ekey, ekey),
        ).fetchall()
        return [dict(r) for r in rows]


async def _llm_summary(company_name: str, entity: dict, relations: list[dict]) -> str | None:
    if not config.WIKI_SUMMARY_ENABLED:
        return None
    try:
        rel_lines = []
        for r in relations[:25]:
            p = r.get("props") or {}
            ps = "，".join(f"{k}={v}" for k, v in list(p.items())[:6])
            rel_lines.append(f"- {r['direction']}{r['relation']} {r['other']}" + (f"（{ps}）" if ps else ""))
        props_lines = [f"- {k}: {v}" for k, v in list((entity.get('props') or {}).items())[:20]]
        prompt = (
            "你是公司信息知识库（LLM Wiki）的维护者。请根据以下图谱三元组和属性，"
            "为该实体写一段 150 字以内的中文摘要，客观陈述事实，不要编造信息，不要使用列表。\n\n"
            f"实体：{entity.get('name')}（类型：{entity.get('type') or '未知'}）\n"
            f"所属公司知识库：{company_name}\n\n"
            "属性：\n" + ("\n".join(props_lines) or "（无）") + "\n\n"
            "关系：\n" + ("\n".join(rel_lines) or "（无）") + "\n\n"
            "直接输出摘要正文："
        )
        # 双保险：即便 LLM 配置了长超时，摘要也不阻塞 ingest 主流程
        resp = await asyncio.wait_for(get_llm().acomplete(prompt), timeout=min(120, max(30, config.LLM_REQUEST_TIMEOUT)))
        text = str(resp.text or "").strip()
        return text[:600] or None
    except Exception:
        return None


def resolve_company_ekey(company_id: int, company_name: str) -> str:
    """找到「公司主体」对应的实体键。

    LLM 抽出的实体名常和公司记录名不完全一致（记录是「新华智云公司」，
    抽出来的是「新华智云」）。直接用公司名查会查不到，公司总览页就变空白 ——
    这里加一层回退：名称互为前缀的实体里，取度数（关系数）最高的那个。
    """
    target = graph_store.norm_ekey(company_name)
    entities = graph_store.entities_for_company(company_id)
    if not entities:
        return target
    if any(e["ekey"] == target for e in entities):
        return target

    name = str(company_name)
    cands = [
        e
        for e in entities
        if e["name"] and (name.startswith(str(e["name"])) or str(e["name"]).startswith(name))
    ]
    if not cands:
        return target

    deg: dict[str, int] = {}
    with graph_store.connect() as conn:
        for r in conn.execute(
            "SELECT src_ekey AS k, COUNT(*) AS n FROM triplets WHERE company_id=? GROUP BY src_ekey",
            (company_id,),
        ):
            deg[r["k"]] = deg.get(r["k"], 0) + int(r["n"])
        for r in conn.execute(
            "SELECT dst_ekey AS k, COUNT(*) AS n FROM triplets WHERE company_id=? GROUP BY dst_ekey",
            (company_id,),
        ):
            deg[r["k"]] = deg.get(r["k"], 0) + int(r["n"])
    cands.sort(key=lambda e: deg.get(e["ekey"], 0), reverse=True)
    return str(cands[0]["ekey"])


def _existing_summary(company_slug: str, page_name: str) -> str | None:
    """读取已有页面里的「## 摘要」段。

    重建页面时若这次没生成摘要（LLM 关了 / 抽取有错误 / 预算用完），就复用旧摘要 ——
    避免重建一次就把历史摘要清空。
    """
    try:
        text = wiki.read_page(company_slug, page_name) or ""
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"## 摘要\s*\n+(.+?)\n+## ", text, re.S)
    return m.group(1).strip() if m else None


async def regenerate_wiki_pages(
    company_slug: str,
    company_name: str,
    company_id: int,
    snapshot_id: str,
    with_summary: bool = True,
) -> dict:
    """按图谱当前数据重建公司总览页 + 各实体页（ingest 与关系分析一体的一部分）。"""
    entities = graph_store.entities_for_company(company_id)
    latest = graph_store.latest_snapshot_id(company_id) or snapshot_id

    # 统计每个实体的三元组数，决定哪些实体页需要 LLM 摘要
    triplets = graph_store.select_triplets(company_id, "all")
    deg: dict[str, int] = {}
    for t in triplets:
        deg[t["src_ekey"]] = deg.get(t["src_ekey"], 0) + 1
        deg[t["dst_ekey"]] = deg.get(t["dst_ekey"], 0) + 1

    summary_budget = config.WIKI_SUMMARY_MAX_ENTITIES
    summaries_done = 0
    pages_written = 0

    # 公司总览页
    comp_ekey = resolve_company_ekey(company_id, company_name)
    comp_rel = _relations_of_entity(company_id, comp_ekey, None)
    comp_ent_row = next((e for e in entities if e["ekey"] == comp_ekey), None)
    comp_entity = {
        "name": company_name,
        "type": (comp_ent_row["type"] if comp_ent_row else "公司"),
        "types": json.loads(comp_ent_row["types_json"] or "[]") if comp_ent_row else ["公司"],
        "props": (json.loads(comp_ent_row["props_json"] or "{}") if comp_ent_row else {}),
        "sources": _sources_of_entity(company_id, comp_ekey),
    }
    summary = await _llm_summary(company_name, comp_entity, comp_rel) if with_summary else None
    if summary:
        summaries_done += 1
    else:
        summary = _existing_summary(company_slug, "_company")
    page_md = wiki.render_entity_page_markdown(comp_entity, comp_rel, summary)
    wiki.write_entity_page(company_slug, "_company", page_md)
    pages_written += 1

    for e in entities:
        if e["ekey"] == comp_ekey:
            continue
        rels = _relations_of_entity(company_id, e["ekey"], None)
        entity_dict = {
            "name": e["name"],
            "type": e["type"],
            "types": json.loads(e["types_json"] or "[]"),
            "props": json.loads(e["props_json"] or "{}"),
            "sources": _sources_of_entity(company_id, e["ekey"]),
        }
        summary = None
        if with_summary and summary_budget > 0 and deg.get(e["ekey"], 0) >= 3:
            summary = await _llm_summary(company_name, entity_dict, rels)
            if summary:
                summaries_done += 1
                summary_budget -= 1
        if not summary:
            summary = _existing_summary(company_slug, e["name"])
        page_md = wiki.render_entity_page_markdown(entity_dict, rels, summary)
        wiki.write_entity_page(company_slug, e["name"], page_md)
        pages_written += 1

    # 清理"幽灵页"：图谱里已不存在的实体的旧页面（重抽变严/实体拆分后遗留）
    removed_pages = wiki.remove_stale_pages(
        company_slug, ["_company"] + [e["name"] for e in entities]
    )

    return {
        "pages_written": pages_written,
        "summaries": summaries_done,
        "pages_removed": removed_pages,
    }
