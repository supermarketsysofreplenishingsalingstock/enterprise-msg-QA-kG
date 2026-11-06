"""图谱查询服务：供 API 层组装 {nodes, edges, counts} 数据（与参考项目同构）。"""
from __future__ import annotations

import json
from collections import defaultdict

from . import graph_store

HIDDEN_PROPS = {"embedding", "triplet_source_id"}


def _load_props(s: str | None) -> dict:
    try:
        d = json.loads(s or "{}")
        return {k: v for k, v in d.items() if k not in HIDDEN_PROPS}
    except Exception:
        return {}


def get_graph_data(
    company_slug: str,
    category: str | None = None,
    snapshot: str = "latest",
    search: str | None = None,
    limit: int = 300,
) -> dict:
    comp = graph_store.get_company_by_slug(company_slug)
    if not comp:
        return {"nodes": [], "edges": [], "counts": {"entities": 0, "triplets": 0, "chunks": 0, "snapshots": 0}}
    company_id = int(comp["id"])

    if snapshot in ("latest", "", None):
        snapshot = graph_store.latest_snapshot_id(company_id) or "all"

    entities = graph_store.entities_for_company(company_id)
    ent_rows = {e["ekey"]: e for e in entities}

    triplets = graph_store.select_triplets(company_id, snapshot)

    # 节点筛选
    keep: dict[str, dict] = {}
    if search:
        q = search.lower()
        ekeys = [ek for ek, e in ent_rows.items() if q in str(e["name"]).lower()]
        matched = set(ekeys)
        # 搜索命中的实体保留其直接邻居
        for t in triplets:
            if t["src_ekey"] in matched:
                matched.add(t["dst_ekey"])
            if t["dst_ekey"] in matched:
                matched.add(t["src_ekey"])
    else:
        matched = set(ent_rows.keys())

    for ek in matched:
        e = ent_rows.get(ek)
        if not e:
            continue
        if category and category not in ("__all__", ""):
            types = json.loads(e["types_json"] or "[]")
            all_types = {e["type"] or "", *types}
            if category not in all_types:
                continue
        keep[ek] = {
            "id": ek,
            "name": e["name"],
            "type": e["type"] or "其他",
            "types": json.loads(e["types_json"] or "[]"),
            "properties": _load_props(e["props_json"]),
            "first_snapshot": e["first_snapshot"],
            "last_snapshot": e["last_snapshot"],
        }

    # 边（两端都在保留集合内）
    edges = []
    degree: defaultdict[str, int] = defaultdict(int)
    for t in triplets:
        if t["src_ekey"] not in keep or t["dst_ekey"] not in keep:
            continue
        edges.append(
            {
                "source": t["src_ekey"],
                "target": t["dst_ekey"],
                "relation": t["relation"],
                "properties": _load_props(t["props_json"]),
                "snapshot_id": t["snapshot_id"],
                "crawl_time": t["crawl_time"],
                "source_url": t["source_url"],
                "source_title": t["source_title"],
            }
        )
        degree[t["src_ekey"]] += 1
        degree[t["dst_ekey"]] += 1

    nodes = []
    for ek, n in keep.items():
        n["degree"] = degree.get(ek, 0)
        nodes.append(n)
    nodes.sort(key=lambda x: x["degree"], reverse=True)
    nodes = nodes[: max(1, limit)]
    keep_ids = {n["id"] for n in nodes}
    edges = [e for e in edges if e["source"] in keep_ids and e["target"] in keep_ids]

    with graph_store.connect() as conn:
        chunk_count = int(
            conn.execute("SELECT COUNT(*) c FROM chunks WHERE company_id=?", (company_id,)).fetchone()["c"]
        )

    return {
        "nodes": nodes,
        "edges": edges,
        "counts": {
            "entities": len(ent_rows),
            "triplets": len(triplets),
            "chunks": chunk_count,
            "snapshots": len(graph_store.list_snapshots(company_id)),
            "shown_nodes": len(nodes),
            "shown_edges": len(edges),
        },
        "snapshot": snapshot,
        "company": {"slug": comp["slug"], "name": comp["name"]},
    }


def get_entity_detail(company_slug: str, ekey: str) -> dict:
    comp = graph_store.get_company_by_slug(company_slug)
    if not comp:
        raise KeyError("company not found")
    company_id = int(comp["id"])
    with graph_store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM entities WHERE company_id=? AND ekey=?", (company_id, ekey)
        ).fetchone()
    if not row:
        raise KeyError("entity not found")

    relations = []
    for t in graph_store.select_triplets(company_id, "all"):
        if t["src_ekey"] == ekey:
            relations.append(
                {
                    "other": t["dst_name"],
                    "other_id": t["dst_ekey"],
                    "direction": "out",
                    "relation": t["relation"],
                    "properties": _load_props(t["props_json"]),
                    "snapshot_id": t["snapshot_id"],
                    "crawl_time": t["crawl_time"],
                    "source_url": t["source_url"],
                }
            )
        elif t["dst_ekey"] == ekey:
            relations.append(
                {
                    "other": t["src_name"],
                    "other_id": t["src_ekey"],
                    "direction": "in",
                    "relation": t["relation"],
                    "properties": _load_props(t["props_json"]),
                    "snapshot_id": t["snapshot_id"],
                    "crawl_time": t["crawl_time"],
                    "source_url": t["source_url"],
                }
            )

    return {
        "id": ekey,
        "name": row["name"],
        "type": row["type"],
        "types": json.loads(row["types_json"] or "[]"),
        "properties": _load_props(row["props_json"]),
        "first_snapshot": row["first_snapshot"],
        "last_snapshot": row["last_snapshot"],
        "relations": relations[:100],
    }


def categories_of_company(company_slug: str) -> list[dict]:
    comp = graph_store.get_company_by_slug(company_slug)
    if not comp:
        return []
    company_id = int(comp["id"])
    counter: dict[str, int] = defaultdict(int)
    for e in graph_store.entities_for_company(company_id):
        types = {e["type"] or "", *json.loads(e["types_json"] or "[]")}
        for t in types:
            if t:
                counter[t] += 1
    return [
        {"category": k, "count": v}
        for k, v in sorted(counter.items(), key=lambda x: -x[1])
    ]
