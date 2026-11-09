"""测试：extract=false 延迟抽取 → process_pending 补做全链路（mock LLM，不依赖真实服务）。"""
import asyncio
import sys

sys.path.insert(0, ".")

COMPANY = "待处理链路测试公司PQ"


async def fake_extract(text: str) -> dict:
    return {
        "error": None,
        "triplets": [
            {
                "head": COMPANY,
                "head_type": "公司",
                "head_props": {"地址": "杭州市西湖区", "状态": "存续"},
                "relation": "生产",
                "relation_props": {"开始年份": "2024"},
                "tail": "智能传感器",
                "tail_type": "产品",
                "tail_props": {},
            },
            {
                "head": COMPANY,
                "head_type": "公司",
                "head_props": {},
                "relation": "位于",
                "relation_props": {},
                "tail": "杭州",
                "tail_type": "城市",
                "tail_props": {},
            },
        ],
    }


async def main():
    from app import graph_store, config
    import app.ingest as ingest

    # 测试不依赖真实 LLM：mock 抽取函数 + 关闭 Wiki 摘要的 LLM 调用
    config.WIKI_SUMMARY_ENABLED = False
    ingest.extract_from_chunk = fake_extract

    pages = [
        {"url": "https://p.com/1", "title": "主页", "text": f"{COMPANY} 位于杭州，生产智能传感器。"},
        {"url": "https://p.com/2", "title": "产品页", "text": f"{COMPANY} 的智能传感器销往全国。"},
    ]

    # ── 场景1：extract=False → 只入库，存为待处理 ──
    r1 = await ingest.run_ingest({
        "company": COMPANY,
        "crawl_time": "2026-11-09 10:00:00",
        "pages": pages,
        "extract": False,
    })
    assert r1["ok"] and r1["extracted"] is False, r1
    assert r1["pending_chunks"] == r1["chunk_count"] > 0, r1
    assert r1["triplet_count"] == 0
    print(f"[1] extract=False 入库：chunk {r1['chunk_count']} 全部待处理，三元组 0 ✓")

    comp = graph_store.get_company_by_slug(r1["company_slug"])
    n = graph_store.count_unprocessed_chunks(int(comp["id"]))
    assert n == r1["chunk_count"], (n, r1["chunk_count"])
    print(f"[2] 待处理计数 = {n} ✓")

    # ── 场景2：process_pending 补做抽取 ──
    r2 = await ingest.run_process_pending({"company": COMPANY})
    assert r2["ok"] and r2["chunk_count"] == n, r2
    assert r2["triplet_count"] == 2 * n, (r2["triplet_count"], "每 chunk 2 条三元组")
    assert r2["remaining"] == 0, r2
    assert r2["wiki_pages_updated"] > 0, r2
    print(f"[3] process_pending：chunk {r2['chunk_count']}，三元组 {r2['triplet_count']}，"
          f"Wiki 页 {r2['wiki_pages_updated']}，剩余 {r2['remaining']} ✓")

    # 图谱数据应已包含新实体
    from app import graph_service
    g = graph_service.get_graph_data(r1["company_slug"], snapshot="all")
    assert g["counts"]["entities"] >= 3, g["counts"]  # 公司+产品+城市
    names = {nd["name"] for nd in g["nodes"]}
    assert "智能传感器" in names and "杭州" in names, names
    print(f"[4] 图谱实体 {g['counts']['entities']}，含抽取出的实体 ✓")

    # 再点一次应为空
    r3 = await ingest.run_process_pending({"company": COMPANY})
    assert r3["chunk_count"] == 0 and r3["triplet_count"] == 0
    print("[5] 重复处理返回「没有待处理的内容」✓")

    # ── 场景3：extract=True（默认自动抽取）→ 即时处理，无待处理 ──
    r4 = await ingest.run_ingest({
        "company": COMPANY,
        "crawl_time": "2026-11-09 11:00:00",
        "pages": pages,
        "extract": True,
    })
    assert r4["ok"] and r4["extracted"] is True and r4["triplet_count"] == 2 * r4["chunk_count"], r4
    assert r4["pending_chunks"] == 0
    comp2 = graph_store.get_company_by_slug(r4["company_slug"])
    assert graph_store.count_unprocessed_chunks(int(comp2["id"])) == 0
    print(f"[6] extract=True 自动抽取：三元组 {r4['triplet_count']}，无待处理 ✓")

    print("\n✅ 待处理机制全链路测试通过")


if __name__ == "__main__":
    asyncio.run(main())
