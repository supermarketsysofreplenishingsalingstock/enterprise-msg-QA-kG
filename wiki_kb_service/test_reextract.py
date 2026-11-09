"""测试：重新抽取（mock LLM，不依赖真实服务）+ 后台任务。

覆盖：
1) 正常替换：旧拼接实体被拆成两个独立实体，快照计数回填
2) 抽取失败 → 抛错且**旧数据完全保留**（这是「重新抽取把数据抽没了」的回归测试）
3) 抽取结果为空 → 保留旧数据，只记 warning，不删数据
4) 部分失败 → 成功的那条替换、失败的那条原样保留
5) 兼容历史数据：chunk_id 为 NULL 的旧三元组在整源覆盖时替换
6) 后台任务（jobs）：立即返回、后台跑完、进度可查
"""
import asyncio
import sys

sys.path.insert(0, ".")

COMPANY = "重新抽取测试公司RX"
URL1 = "https://rx.com/about"
URL2 = "https://rx.com/product"

# 旧提示词下的错误结果：两个投资方被熔成一个实体
OLD_TRIPLETS = [
    {
        "head": COMPANY, "head_type": "COMPANY", "head_props": {"成立时间": "2017年6月"},
        "relation": "INVESTED_BY", "relation_props": {"金额": "10亿元"},
        "tail": "国家通讯社和阿里巴巴集团", "tail_type": "INVESTMENT_GROUP", "tail_props": {},
    },
]
# 新提示词下的正确结果：拆成两个独立实体
NEW_TRIPLETS = [
    {
        "head": COMPANY, "head_type": "COMPANY", "head_props": {"成立时间": "2017年6月"},
        "relation": "INVESTED_BY", "relation_props": {"金额": "10亿元"},
        "tail": "新华通讯社", "tail_type": "ORGANIZATION", "tail_props": {"alias": "国家通讯社"},
    },
    {
        "head": COMPANY, "head_type": "COMPANY", "head_props": {},
        "relation": "INVESTED_BY", "relation_props": {"金额": "10亿元"},
        "tail": "阿里巴巴集团", "tail_type": "COMPANY", "tail_props": {},
    },
]


def make_fake(triplets, fail_when=None):
    async def fake(text: str) -> dict:
        if fail_when and fail_when in text:
            return {"error": "APIConnectionError: Connection error.", "triplets": []}
        return {"error": None, "triplets": [dict(t) for t in triplets]}

    return fake


def count_triplets(cid: int) -> int:
    from app import graph_store

    with graph_store.connect() as conn:
        return int(
            conn.execute("SELECT COUNT(*) AS n FROM triplets WHERE company_id=?", (cid,)).fetchone()["n"]
        )


def dst_names(cid: int) -> list[str]:
    from app import graph_store

    with graph_store.connect() as conn:
        return sorted(
            r["dst_name"]
            for r in conn.execute("SELECT dst_name FROM triplets WHERE company_id=?", (cid,))
        )


async def main():
    from app import config, graph_store
    import app.ingest as ingest
    from app import jobs

    config.WIKI_SUMMARY_ENABLED = False  # 不真调 LLM

    # ── 1) 用"旧规则"结果入库（两个页面 → 两个 chunk）──
    ingest.extract_from_chunk = make_fake(OLD_TRIPLETS)
    r = await ingest.run_ingest({
        "company": COMPANY,
        "crawl_time": "2026-11-01 10:00:00",
        "pages": [
            {"url": URL1, "title": "关于我们", "text": f"{COMPANY} 由国家通讯社和阿里巴巴集团共同投资组建。"},
            {"url": URL2, "title": "产品", "text": f"{COMPANY} 的产品线介绍。"},
        ],
        "extract": True,
    })
    assert r["ok"] and r["triplet_count"] == 2, r
    comp = graph_store.get_company_by_slug(r["company_slug"])
    cid = int(comp["id"])
    chunks = graph_store.list_chunks(cid)
    assert len(chunks) == 2 and count_triplets(cid) == 2, chunks
    print(f"[1] 旧规则入库：2 个 chunk / 2 条三元组，拼接实体「国家通讯社和阿里巴巴集团」✓")

    # ── 2) 用"新规则"重新抽取两个分块 ──
    ingest.extract_from_chunk = make_fake(NEW_TRIPLETS)
    rr = await ingest.run_reextract({"company": COMPANY, "chunk_ids": [c["chunk_id"] for c in chunks]})
    assert rr["chunk_count"] == 2 and rr["triplet_count"] == 4, rr
    assert rr["deleted_triplets"] == 2, rr
    assert dst_names(cid).count("阿里巴巴集团") == 2, dst_names(cid)
    assert "国家通讯社和阿里巴巴集团" not in dst_names(cid)
    print(f"[2] 正常替换：删除 {rr['deleted_triplets']} 条旧数据，写入 {rr['triplet_count']} 条新数据，实体已拆分 ✓")

    with graph_store.connect() as conn:
        ents = {row["name"] for row in conn.execute("SELECT name FROM entities WHERE company_id=?", (cid,))}
    assert "国家通讯社和阿里巴巴集团" not in ents, ents
    print(f"[3] 孤立拼接实体已清理，实体：{sorted(ents)} ✓")

    # ── 3) 抽取失败：不该动数据，且要抛错 ──

    async def fake_fail(text: str) -> dict:
        return {"error": "APIConnectionError: Connection error.", "triplets": []}

    ingest.extract_from_chunk = fake_fail
    before = count_triplets(cid)
    raised = False
    try:
        await ingest.run_reextract({"company": COMPANY, "chunk_ids": [c["chunk_id"] for c in chunks]})
    except ValueError as e:
        raised = True
        print(f"[4] 抽取全部失败 → 抛错：{str(e)[:60]}…")
    assert raised, "全部失败时应抛 ValueError，让前端明确提示"
    assert count_triplets(cid) == before, f"失败后三元组数变了：{before} → {count_triplets(cid)}"
    assert graph_store.count_unprocessed_chunks(cid) == 0, "旧数据还在，就不该标成待处理"
    print(f"[5] ★ 失败后旧数据完好：{before} 条三元组一条没少 ✓")

    # ── 4) 抽取结果为空：保留旧数据（不能「抽成空就删掉」）──

    async def fake_empty(text: str) -> dict:
        return {"error": None, "triplets": []}

    ingest.extract_from_chunk = fake_empty
    rr2 = await ingest.run_reextract({"company": COMPANY, "chunk_ids": [c["chunk_id"] for c in chunks]})
    assert rr2["triplet_count"] == 0 and rr2["warnings"], rr2
    assert count_triplets(cid) == before, "结果为空不应删除旧数据"
    print(f"[6] ★ 结果为空：保留旧数据 {before} 条，仅记录 {len(rr2['warnings'])} 条提示 ✓")

    # ── 5) 部分失败：成功替换、失败保留 ──
    ingest.extract_from_chunk = make_fake(OLD_TRIPLETS, fail_when="产品")
    rr3 = await ingest.run_reextract({"company": COMPANY, "chunk_ids": [c["chunk_id"] for c in chunks]})
    assert rr3["chunk_count"] == 1 and rr3["errors"], rr3
    assert count_triplets(cid) == 3, count_triplets(cid)  # 成功 1 条（旧拼接实体）+ 失败那条的 2 条旧数据
    print(f"[7] ★ 部分失败：成功分块已替换（1 条），失败分块保留旧数据（2 条），共 {count_triplets(cid)} 条 ✓")

    # ── 6) 兼容历史数据：chunk_id 为 NULL 的三元组，整源覆盖时被替换 ──
    with graph_store.connect() as conn:
        conn.execute("UPDATE triplets SET chunk_id=NULL WHERE company_id=?", (cid,))
    ingest.extract_from_chunk = make_fake(NEW_TRIPLETS)
    rr4 = await ingest.run_reextract({"company": COMPANY, "chunk_ids": [c["chunk_id"] for c in chunks]})
    assert rr4["deleted_triplets"] == 3, rr4
    assert count_triplets(cid) == 4, count_triplets(cid)
    print(f"[8] 历史数据兼容：无 chunk_id 的 {rr4['deleted_triplets']} 条被整源替换，现 {count_triplets(cid)} 条 ✓")

    # ── 7) 后台任务：立即返回、后台跑完、进度可查 ──
    async def slow_runner(job):
        jobs.progress_setter(job)(done=0, total=2, stage="跑第一步")
        await asyncio.sleep(0.05)
        jobs.progress_setter(job)(done=1, total=2, stage="跑第二步")
        await asyncio.sleep(0.05)
        return {"ok": True, "triplet_count": 4}

    job = jobs.spawn(kind="reextract", company=COMPANY, runner=slow_runner, total=2)
    assert job["status"] == "queued" and jobs.running_for(COMPANY), job
    snap = jobs.get(job["id"])
    assert snap is not None and snap["id"] == job["id"]
    for _ in range(50):
        await asyncio.sleep(0.02)
        snap = jobs.get(job["id"])
        if snap["status"] in ("done", "error"):
            break
    assert snap["status"] == "done", snap
    assert snap["result"]["triplet_count"] == 4 and snap["progress"]["done"] == 1, snap
    assert jobs.running_for(COMPANY) is None
    print(f"[9] 后台任务：提交即返回，进度 {snap['progress']}，状态 {snap['status']}，耗时 {snap['elapsed']}s ✓")

    # ── 8) 清理测试数据 ──
    with graph_store.connect() as conn:
        for t in ("triplets", "entities", "chunks", "raw_sources"):
            conn.execute(f"DELETE FROM {t} WHERE company_id=?", (cid,))
        conn.execute("DELETE FROM snapshots WHERE company_id=?", (cid,))
        conn.execute("DELETE FROM companies WHERE id=?", (cid,))
    print("\n✅ 重新抽取 + 后台任务测试全部通过（测试数据已清理）")


if __name__ == "__main__":
    asyncio.run(main())
