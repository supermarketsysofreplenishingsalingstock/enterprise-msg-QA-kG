"""从历史 Wiki 实体页重建图谱数据（数据恢复工具）。

背景：早期「重新抽取」是先删三元组再抽，LLM 一旦失败就会把旧数据带走。
本脚本把 data/wiki/<公司>/pages/*.md 里的关系表反解回图谱（实体 + 三元组），
用于把误删的数据补回来。

用法：
    python restore_from_wiki.py 新华智云公司            # 预览
    python restore_from_wiki.py 新华智云公司 --apply     # 真正写库
    python restore_from_wiki.py 新华智云公司 --apply --split "国家通讯社和阿里巴巴集团=新华通讯社:ORGANIZATION,阿里巴巴集团:COMPANY"

说明：
- 关系表里 `→ REL | 对象` 表示「本页实体 --REL--> 对象」，`← REL | 对象` 表示「对象 --REL--> 本页实体」；
- 三元组按 (src, dst, relation, snapshot) 去重，重复执行不会造重复数据；
- 恢复出来的三元组 chunk_id 为 NULL（来源分块无从还原），后续重新抽取整源时会被整源替换；
- `--split` 可把提示词升级前被熔成一个实体的并列主体拆成多条边。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from app import graph_store, wiki

ROW_RE = re.compile(r"^\|\s*(→|←)\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$")


def parse_page(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    name = ""
    etype = ""
    rows = []
    for line in text.splitlines():
        if not name and line.startswith("# "):
            name = line[2:].strip()
            continue
        if line.startswith("> ") and "类型：" in line:
            m = re.search(r"类型：([^；\s]+)", line)
            if m:
                etype = m.group(1).strip()
            continue
        m = ROW_RE.match(line)
        if m:
            direction, relation, other, props_raw, snap = m.groups()
            props = {}
            for chunk in re.split(r"[；;]", props_raw or ""):
                if "=" in chunk:
                    k, v = chunk.split("=", 1)
                    props[k.strip()] = v.strip()
            rows.append(
                {
                    "direction": direction,
                    "relation": relation.strip(),
                    "other": other.strip(),
                    "props": props,
                    "snapshot_id": snap.strip(),
                }
            )
    return {"name": name or path.stem, "type": etype, "rows": rows, "file": path.name}


def parse_split(spec: str) -> dict[str, list[tuple[str, str]]]:
    """把 '旧名=新名1:类型1,新名2:类型2' 解析成 {旧名: [(新名, 类型)]}。"""
    out: dict[str, list[tuple[str, str]]] = {}
    if not spec:
        return out
    for item in spec.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        old, news = item.split("=", 1)
        pairs = []
        for part in news.split(","):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                n, t = part.split(":", 1)
                pairs.append((n.strip(), t.strip() or "ORGANIZATION"))
            else:
                pairs.append((part, "ORGANIZATION"))
        if pairs:
            out[old.strip()] = pairs
    return out


def _read_summary(path: Path) -> str | None:
    """从已有页面里取出 ## 摘要 段（重建页面时保留，避免丢历史摘要）。"""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    m = re.search(r"## 摘要\s*\n+(.+?)\n+## ", text, re.S)
    return m.group(1).strip() if m else None


def patch_wiki_pages(
    company_slug: str,
    company_name: str,
    names: list[str],
    dropped: list[str],
) -> int:
    """只重写受影响的页面（公司总览 + 指定实体），并删掉被拆掉的旧实体页。

    不像 regenerate_wiki_pages 那样全量重写，避免把其它实体页的 LLM 摘要冲掉。
    摘要段若原页面有则原样保留。
    """
    from app import ingest  # 延迟导入（会拉起 llama_index）

    pages_dir = wiki.company_dir(company_slug) / "pages"
    for name in dropped:
        p = pages_dir / f"{wiki._safe_name(name, 40)}.md"
        if p.exists():
            p.unlink()

    comp = graph_store.get_company_by_slug(company_slug)
    company_id = int(comp["id"])
    written = 0
    targets = [company_name, *names]
    for name in targets:
        ekey = graph_store.norm_ekey(name)
        rows = [e for e in graph_store.entities_for_company(company_id) if e["ekey"] == ekey]
        if not rows:
            continue
        e = rows[0]
        rels = ingest._relations_of_entity(company_id, ekey, None)
        entity = {
            "name": e["name"],
            "type": e["type"],
            "types": json.loads(e["types_json"] or "[]"),
            "props": json.loads(e["props_json"] or "{}"),
            "sources": ingest._sources_of_entity(company_id, ekey),
        }
        page_name = "_company" if name == company_name else name
        old = _read_summary(pages_dir / f"{wiki._safe_name(page_name, 40)}.md")
        md = wiki.render_entity_page_markdown(entity, rels, old)
        wiki.write_entity_page(company_slug, page_name, md)
        written += 1
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description="从 Wiki 实体页重建图谱")
    ap.add_argument("company", help="公司名称或 slug")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只预览）")
    ap.add_argument("--split", default="", help="并列实体拆分，如 'A和B=X:ORGANIZATION,Y:COMPANY'")
    args = ap.parse_args()

    slug = graph_store.slugify(args.company)
    comp = graph_store.get_company_by_slug(slug)
    if not comp:
        print(f"公司不存在：{args.company}（slug={slug}）")
        return 2
    company_id = int(comp["id"])
    company_name = str(comp["name"])
    company_slug = str(comp["slug"])

    pages_dir = wiki.company_dir(company_slug) / "pages"
    if not pages_dir.exists():
        print(f"找不到页面目录：{pages_dir}")
        return 2

    pages = [parse_page(p) for p in sorted(pages_dir.glob("*.md"))]
    types = {p["name"]: p["type"] for p in pages if p["type"]}
    types[company_name] = types.get(company_name) or "COMPANY"
    splits = parse_split(args.split)

    # 反解三元组
    edges: dict[tuple[str, str, str, str], dict] = {}
    for p in pages:
        src_page = p["name"]
        for r in p["rows"]:
            if r["direction"] == "→":
                src, dst = src_page, r["other"]
            else:
                src, dst = r["other"], src_page
            pairs: list[tuple[str, str]]
            if splits.get(dst):
                pairs = [(src, dst_new) for dst_new, _ in splits[dst]]
                for new_name, new_type in splits[dst]:
                    types[new_name] = new_type
            elif splits.get(src):
                pairs = [(new_name, dst) for new_name, _ in splits[src]]
                for new_name, new_type in splits[src]:
                    types[new_name] = new_type
            else:
                pairs = [(src, dst)]
            for s, d in pairs:
                key = (graph_store.norm_ekey(s), graph_store.norm_ekey(d), r["relation"], r["snapshot_id"])
                edges.setdefault(
                    key,
                    {
                        "src": s,
                        "dst": d,
                        "relation": r["relation"],
                        "snapshot_id": r["snapshot_id"],
                        "props": r["props"],
                    },
                )

    # 每个快照的来源信息（从 chunks 取，回填 source_url/title/crawl_time）
    src_info: dict[str, dict] = {}
    with graph_store.connect() as conn:
        for row in conn.execute(
            "SELECT c.snapshot_id, c.source_url, c.source_title, c.source_kind,"
            " COALESCE(s.crawl_time,'') AS crawl_time FROM chunks c"
            " LEFT JOIN snapshots s ON s.snapshot_id=c.snapshot_id"
            " WHERE c.company_id=? ORDER BY c.ord",
            (company_id,),
        ):
            src_info.setdefault(row["snapshot_id"], dict(row))

    entities = sorted({e["src"] for e in edges.values()} | {e["dst"] for e in edges.values()})
    print(f"公司：{company_name}（id={company_id}）")
    print(f"解析页面 {len(pages)} 个 → 实体 {len(entities)} 个，三元组 {len(edges)} 条")
    for name in entities:
        deg = sum(1 for e in edges.values() if e["src"] == name or e["dst"] == name)
        print(f"  - {name}（{types.get(name) or '未知类型'}）度={deg}")
    if splits:
        for old, news in splits.items():
            print(f"拆分：{old} → " + "、".join(f"{n}({t})" for n, t in news))

    if not args.apply:
        print("\n预览模式，未写库。确认无误后加 --apply 执行。")
        return 0

    # 写实体
    for name in entities:
        snap = next(
            (e["snapshot_id"] for e in edges.values() if name in (e["src"], e["dst"])), ""
        )
        graph_store.upsert_entity(company_id, name, types.get(name) or "", {}, snap)

    # 写三元组（幂等：已存在同样边则跳过）
    with graph_store.connect() as conn:
        existing = {
            (r["src_ekey"], r["dst_ekey"], r["relation"], r["snapshot_id"])
            for r in conn.execute(
                "SELECT src_ekey, dst_ekey, relation, snapshot_id FROM triplets WHERE company_id=?",
                (company_id,),
            )
        }
    inserted = 0
    skipped = 0
    for key, e in edges.items():
        if key in existing:
            skipped += 1
            continue
        info = src_info.get(e["snapshot_id"], {})
        snap = e["snapshot_id"] or (graph_store.latest_snapshot_id(company_id) or "")
        if not snap:
            print("警告：没有快照信息，跳过一条边", key)
            continue
        graph_store.upsert_entity(
            company_id, e["src"], types.get(e["src"]) or "", {}, snap
        )
        graph_store.upsert_entity(
            company_id, e["dst"], types.get(e["dst"]) or "", {}, snap
        )
        graph_store.insert_triplet(
            company_id=company_id,
            snapshot_id=snap,
            crawl_time=str(info.get("crawl_time") or graph_store.now_iso()),
            src_ekey=graph_store.norm_ekey(e["src"]),
            dst_ekey=graph_store.norm_ekey(e["dst"]),
            src_name=e["src"],
            dst_name=e["dst"],
            src_type=types.get(e["src"]) or "",
            dst_type=types.get(e["dst"]) or "",
            relation=e["relation"],
            props=e["props"] or {},
            source_url=str(info.get("source_url") or ""),
            source_title=str(info.get("source_title") or ""),
            source_kind=str(info.get("source_kind") or "page"),
            chunk_id=None,
        )
        inserted += 1

    # 清理不再被引用的孤儿实体（例如被拆掉的拼接实体）
    orphans = graph_store.delete_orphan_entities(
        company_id, keep_ekeys=[graph_store.norm_ekey(company_name)]
    )
    for snap in {e["snapshot_id"] for e in edges.values() if e["snapshot_id"]}:
        graph_store.recount_snapshot_triplets(company_id, snap)

    # 只重写受影响页面：公司总览 + 拆分后的新实体，并删掉被拆掉的旧实体页
    split_targets = [n for pairs in splits.values() for n, _ in pairs]
    pages_fixed = patch_wiki_pages(company_slug, company_name, split_targets, list(splits))

    wiki.append_log(
        company_slug,
        f"数据恢复：从 Wiki 页反解重建 三元组 {inserted} 条（已存在跳过 {skipped}），"
        f"实体 {len(entities)} 个，清理孤儿实体 {orphans}，重写页面 {pages_fixed}",
    )
    wiki.rebuild_index(company_slug, company_name)
    print(f"\n完成：写入三元组 {inserted} 条，跳过已存在 {skipped} 条，清理孤儿实体 {orphans} 个，重写页面 {pages_fixed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
