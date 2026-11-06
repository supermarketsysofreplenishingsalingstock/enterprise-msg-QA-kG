"""
LLM Wiki 文件管理（Karpathy LLM Wiki 模式的落地实现）：

目录结构（data/wiki/<公司slug>/）：
  raw/<snapshot_id>/<序号>_<来源名>.md    —— 原始资料层（只读存档，每页/每文件一个 md）
  pages/<实体slug>.md                     —— Wiki 层（LLM 维护的实体页面，随 ingest 更新）
  pages/_company.md                       —— 公司总览页
  index.md                                —— 面向内容的目录（所有实体页 + 快照清单）
  log.md                                  —— 面向时间的追加记录（每次 ingest 的操作日志）

ingest 与图谱抽取（关系分析）在同一流程中一起完成：见 ingest.py。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from . import config, graph_store

META_KEYS = ["url", "title", "kind", "crawl_time", "snapshot_id"]


def company_dir(company_slug: str) -> Path:
    d = config.WIKI_DIR / company_slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "raw").mkdir(exist_ok=True)
    (d / "pages").mkdir(exist_ok=True)
    return d


def _front_matter(meta: dict) -> str:
    lines = ["---"]
    for k in META_KEYS:
        if meta.get(k):
            lines.append(f"{k}: {meta[k]}")
    lines.append("---")
    return "\n".join(lines)


def _safe_name(s: str, max_len: int = 60) -> str:
    s = re.sub(r"[\\/:*?\"<>|\s]+", "_", str(s or "")).strip("._")
    return (s[:max_len] or "unnamed")


def save_raw_source(
    company_slug: str,
    snapshot_id: str,
    kind: str,
    url: str,
    title: str,
    text: str,
    crawl_time: str,
) -> Path:
    """原始资料层：一篇页面 / 一个文件 -> 一个 md（只追加，不修改历史）。"""
    d = company_dir(company_slug)
    snap_dir = d / "raw" / snapshot_id
    snap_dir.mkdir(parents=True, exist_ok=True)
    existing = list(snap_dir.glob("*.md"))
    idx = len(existing) + 1
    fname = f"{idx:03d}_{_safe_name(title or url)}.md"
    path = snap_dir / fname
    meta = {
        "url": url,
        "title": title,
        "kind": kind,
        "crawl_time": crawl_time,
        "snapshot_id": snapshot_id,
    }
    body = str(text or "").strip() or "（无正文内容）"
    path.write_text(f"{_front_matter(meta)}\n\n# {title or url}\n\n{body}\n", encoding="utf-8")
    return path


# ────────────────────────── 实体页面 ──────────────────────────

def render_entity_page_markdown(
    entity: dict,
    relations: list[dict],
    summary: str | None,
) -> str:
    """实体 Wiki 页：LLM 摘要（可选）+ 属性表 + 关系表 + 来源。全部来自图谱实际数据。"""
    lines = [f"# {entity['name']}", ""]
    meta_bits = []
    if entity.get("type"):
        meta_bits.append(f"类型：{entity['type']}")
    if entity.get("types"):
        extra = [t for t in entity["types"] if t and t != entity.get("type")]
        if extra:
            meta_bits.append("出现过类型：" + "、".join(extra))
    if meta_bits:
        lines.append("> " + "；".join(meta_bits))
        lines.append("")
    if summary:
        lines += ["## 摘要", "", summary.strip(), ""]

    props = entity.get("props") or {}
    if props:
        lines += ["## 属性", "", "| 属性 | 值 |", "| --- | --- |"]
        for k, v in props.items():
            if k in ("name", "type"):
                continue
            vv = v if not isinstance(v, (dict, list)) else json.dumps(v, ensure_ascii=False)
            lines.append(f"| {k} | {vv} |")
        lines.append("")

    if relations:
        lines += ["## 关系", "", "| 关系 | 对象 | 关系属性 | 快照 |", "| --- | --- | --- | --- |"]
        for r in relations[:80]:
            p = r.get("props") or {}
            ps = "；".join(f"{k}={v}" for k, v in p.items()) if p else ""
            lines.append(
                f"| {r.get('direction','→')} {r['relation']} | {r['other']} | {ps} | {r.get('snapshot_id','')} |"
            )
        lines.append("")

    sources = entity.get("sources") or []
    if sources:
        lines += ["## 来源", ""]
        for s in sources[:30]:
            title = s.get("title") or s.get("url") or ""
            lines.append(f"- [{title}]({s.get('url','')})（{s.get('crawl_time','')}）")
        lines.append("")

    lines += [
        "---",
        f"_更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}；来源：ingest 自动维护（ingest 与图谱关系分析一体完成）_",
    ]
    return "\n".join(lines)


def write_entity_page(company_slug: str, entity_name: str, content: str) -> Path:
    d = company_dir(company_slug)
    page_path = d / "pages" / f"{_safe_name(entity_name, 40)}.md"
    page_path.write_text(content, encoding="utf-8")
    return page_path


def read_page(company_slug: str, page_name: str) -> str | None:
    p = company_dir(company_slug) / "pages" / f"{_safe_name(page_name, 40)}.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return None


def list_pages(company_slug: str) -> list[dict]:
    d = company_dir(company_slug) / "pages"
    out = []
    for p in sorted(d.glob("*.md")):
        first = ""
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.startswith("# "):
                    first = line[2:].strip()
                    break
        except Exception:
            pass
        out.append({"file": p.name, "title": first or p.stem, "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
    return out


# ────────────────────────── index.md / log.md ──────────────────────────

def rebuild_index(company_slug: str, company_name: str) -> Path:
    d = company_dir(company_slug)
    pages = list_pages(company_slug)
    snaps = graph_store.list_snapshots_by_slug(company_slug)
    lines = [
        f"# {company_name} 知识库索引",
        "",
        "_LLM Wiki 面向内容的目录：所有实体页 + 快照时间线。ingest 时自动更新。_",
        "",
        "## 实体页面",
        "",
    ]
    for p in pages:
        lines.append(f"- [{p['title']}](pages/{p['file']})（更新 {p['mtime']}）")
    lines += ["", "## 快照时间线", ""]
    for s in snaps:
        lines.append(
            f"- **{s['snapshot_id']}** 爬取时间 {s.get('crawl_time','')}；"
            f"页面 {s.get('page_count',0)} / 文件 {s.get('file_count',0)} / 三元组 {s.get('triplet_count',0)}"
        )
    path = d / "index.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def append_log(company_slug: str, message: str) -> Path:
    d = company_dir(company_slug)
    path = d / "log.md"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"- [{ts}] {message}\n")
    return path


def read_log(company_slug: str, tail: int = 100) -> str:
    path = company_dir(company_slug) / "log.md"
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[-tail:])


def read_index(company_slug: str) -> str:
    path = company_dir(company_slug) / "index.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""
