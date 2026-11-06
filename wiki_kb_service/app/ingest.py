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

from . import config, graph_store, wiki
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

    # 3) 分 chunk 登记
    chunk_rows: list[dict] = []
    ord_i = 0
    for s in sources:
        for piece in chunk_text(s["text"]):
            cid = graph_store.insert_chunk(company_id, snapshot_id, s["url"], s["title"], s["kind"], ord_i, piece)
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
    if not dry_run:
        for cr in chunk_rows:
            res = await extract_from_chunk(cr["text"])
            if res.get("error"):
                errors.append(f"chunk {cr['chunk_id']}: {res['error']}")
                continue
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
                    source_url=cr["url"],
                    source_title=cr["title"],
                    source_kind=cr["kind"],
                )
                triplet_count += 1

    file_count = sum(1 for s in sources if s["kind"] == "file")
    page_count = sum(1 for s in sources if s["kind"] == "page")
    graph_store.finalize_snapshot(
        snapshot_id, page_count, file_count, len(chunk_rows), triplet_count
    )

    # 5) Wiki 层更新（与图谱抽取同流程完成；dry_run 不调 LLM）
    wiki_stats = await regenerate_wiki_pages(
        company_slug, company_name, company_id, snapshot_id, with_summary=not dry_run
    )

    wiki.append_log(
        company_slug,
        f"ingest 快照 {snapshot_id}：来源 页面{page_count}/文件{file_count}，chunk {len(chunk_rows)}，"
        f"新抽取三元组 {triplet_count}，更新实体页 {wiki_stats['pages_written']}；LLM={llm_desc}"
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
    comp_ekey = graph_store.norm_ekey(company_name)
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
        page_md = wiki.render_entity_page_markdown(entity_dict, rels, summary)
        wiki.write_entity_page(company_slug, e["name"], page_md)
        pages_written += 1

    return {"pages_written": pages_written, "summaries": summaries_done}
