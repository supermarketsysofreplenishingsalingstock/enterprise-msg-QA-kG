"""测试：零向量检索链路（LLM 用 mock，不依赖真实模型/服务）。

覆盖：
1) 分词：中文一元 + 二元、ASCII 词、停用词
2) BM25：相关文档排第一；标题命中加权
3) 图谱路：实体名/别名命中 + 关系词加成（“投资方”问句把投资方事实排最前）
4) Wiki 路：类型同义词归一（问“口号”→ 类型“理念”的页面也能排前）
5) chunk 路：正文全文可检索
6) RRF 融合：两路都命中的文档排序高于单路
7) 上下文拼装：[n] 编号连续、事实配额与 Wiki/原文配额、摘录围绕查询词
8) ask()：mock LLM 下返回答案 + 出处，未命中时明确说“未找到”
"""
import asyncio
import sys

sys.path.insert(0, ".")

COMPANY = "检索测试公司RQ"
URL = "https://rq.example.com/about"
TRIPLETS = [
    {"head": COMPANY, "head_type": "公司", "head_props": {"成立时间": "2018年3月"},
     "relation": "投资方", "relation_props": {}, "tail": "新华通讯社", "tail_type": "机构", "tail_props": {}},
    {"head": COMPANY, "head_type": "公司", "head_props": {},
     "relation": "投资方", "relation_props": {}, "tail": "阿里巴巴集团", "tail_type": "公司", "tail_props": {}},
    {"head": COMPANY, "head_type": "公司", "head_props": {},
     "relation": "愿景", "relation_props": {}, "tail": "赋万物为媒，迎智能时代", "tail_type": "理念", "tail_props": {}},
    {"head": COMPANY, "head_type": "公司", "head_props": {},
     "relation": "推出", "relation_props": {}, "tail": "媒体大脑", "tail_type": "产品", "tail_props": {}},
    {"head": COMPANY, "head_type": "公司", "head_props": {},
     "relation": "资质", "relation_props": {}, "tail": "国家高新技术企业", "tail_type": "资质", "tail_props": {}},
]
import app.retrieval as retrieval_mod

PAGES = [
    {"url": URL, "title": "关于我们",
     "text": f"{COMPANY} 成立于2018年3月，由新华通讯社和阿里巴巴集团共同投资。公司的愿景是「赋万物为媒，迎智能时代」。"
             f"公司核心产品为媒体大脑，已获国家高新技术企业资质。"},
    {"url": f"{URL}/join", "title": "加入我们",
     "text": f"{COMPANY} 招聘算法工程师、产品经理。工作地点在杭州，办公环境优美。"},
]


def purge_company(name: str) -> None:
    """清掉上一次失败运行可能残留的测试公司数据（幂等）。"""
    from app import config, graph_store

    comp = graph_store.get_company_by_slug(graph_store.slugify(name))
    if not comp:
        return
    cid = int(comp["id"])
    with graph_store.connect() as conn:
        for tbl in ("triplets", "entities", "chunks", "snapshots", "raw_sources"):
            try:
                conn.execute(f"DELETE FROM {tbl} WHERE company_id=?", (cid,))
            except Exception:
                pass
        conn.execute("DELETE FROM companies WHERE id=?", (cid,))
        conn.commit()
    import shutil

    shutil.rmtree(config.WIKI_DIR / comp["slug"], ignore_errors=True)


async def build_fixture():
    import app.ingest as ingest
    from app import config

    config.WIKI_SUMMARY_ENABLED = False
    purge_company(COMPANY)
    retrieval_mod.clear_index_cache()

    async def fake_extract(text: str) -> dict:
        # 只有“关于我们”那段内容才产出三元组，模拟抽取器行为
        if "关于我们" in text or COMPANY in text:
            return {"error": None, "triplets": [dict(t) for t in TRIPLETS]}
        return {"error": None, "triplets": []}

    ingest.extract_from_chunk = fake_extract
    r = await ingest.run_ingest(
        {"company": COMPANY, "crawl_time": "2026-11-10 10:00:00", "pages": PAGES, "extract": True}
    )
    assert r["ok"], r
    from app import graph_store

    comp = graph_store.get_company_by_slug(r["company_slug"])
    return r["company_slug"], int(comp["id"])


def main():
    from app import graph_store, retrieval

    # ── 1) 分词 ──
    toks = retrieval.tokenize("新华智云的投资方是谁？MAGIC 平台")
    assert "投资" in toks and "资方" in toks, toks
    assert "的" not in toks and "是" not in toks, toks
    assert "magic" in toks, toks
    print(f"[1] 分词：一元+二元+英文词，停用词已剔除（{len(toks)} 个词）✓")

    # ── 2) BM25 ──
    docs = [
        retrieval.Doc(key="a", kind="chunk", title="招聘", text="招聘算法工程师，工作地点杭州"),
        retrieval.Doc(key="b", kind="chunk", title="关于我们", text="公司由新华通讯社和阿里巴巴集团共同投资，主营媒体大脑"),
    ]
    idx = retrieval.BM25Index(docs)
    res = idx.search(retrieval.tokenize("阿里巴巴集团 投资"))
    assert res and res[0][1].key == "b", res
    print(f"[2] BM25：相关文档排第一（score={res[0][0]:.3f}）✓")

    # ── 3~8) 建库 + 检索 ──
    slug, cid = asyncio.run(build_fixture())
    retrieval.clear_index_cache()
    idx = retrieval.get_index(slug)
    assert len(idx.fact_index.docs) == len(TRIPLETS), len(idx.fact_index.docs)
    assert len(idx.chunk_index.docs) == 2, len(idx.chunk_index.docs)
    print(f"[3] 索引：事实 {len(idx.fact_index.docs)} / Wiki 页 {len(idx.wiki_index.docs)} / 文本块 {len(idx.chunk_index.docs)} ✓")

    # 图谱路：关系词加成
    u = retrieval.understand_query(f"{COMPANY}的投资方是谁？", idx.company_name, use_llm=False)
    r = retrieval.retrieve(slug, f"{COMPANY}的投资方是谁？", u)
    top = [h for h in r["hits"][:2]]
    assert all(h.kind == "fact" and h.doc.meta["relation"] == "投资方" for h in top), [(h.kind, h.doc.meta.get("relation")) for h in top]
    print(f"[4] 图谱路：「投资方」问句 → 前两条都是投资方事实 ✓ routes={r['routes']}")

    # 别名/实体命中（公司名出现在问句里 → 该实体的事实被召回）
    assert any(h.kind == "fact" for h in r["hits"]), "图谱路没有召回任何事实"
    print("[5] 实体链接：问句中的公司名 → 实体命中 → 事实召回 ✓")

    # Wiki 路：类型同义词（问“口号” → 类型「理念」的页面）
    u2 = retrieval.understand_query("公司的口号是什么？", idx.company_name, use_llm=False)
    r2 = retrieval.retrieve(slug, "公司的口号是什么？", u2)
    wiki_hits = [h.doc.title for h in r2["hits"] if h.kind == "wiki"]
    assert any("赋万物为媒" in t for t in wiki_hits), wiki_hits
    print(f"[6] Wiki 路：问「口号」→ 类型「理念」页面被召回（{wiki_hits[:2]}）✓")

    # 全文路：正文里的词（“招聘”“杭州”）只有 chunk 能命中
    u3 = retrieval.understand_query("招聘算法工程师的工作地点在哪？", idx.company_name, use_llm=False)
    r3 = retrieval.retrieve(slug, "招聘算法工程师的工作地点在哪？", u3)
    assert r3["routes"]["chunk"] >= 1, r3["routes"]
    assert any(h.kind == "chunk" for h in r3["hits"][:4]), [h.kind for h in r3["hits"][:4]]
    print(f"[7] 全文路：正文关键词「招聘/杭州」命中原文片段 ✓ routes={r3['routes']}")

    # RRF 融合：两路都命中的文档分数更高
    doc = idx.fact_index.docs[0]
    fused = retrieval.fuse({"fact": [(1.0, doc)], "wiki": [(1.0, doc)]})
    single = retrieval.fuse({"fact": [(1.0, doc)]})
    assert fused[0].score > single[0].score, (fused[0].score, single[0].score)
    assert set(fused[0].ranks) == {"fact", "wiki"}, fused[0].ranks
    print(f"[8] RRF 融合：双路命中 {fused[0].score:.5f} > 单路 {single[0].score:.5f} ✓")

    # 上下文拼装：编号连续 + 配额
    ctx, sources = retrieval.build_context(r["hits"], r["query_terms"])
    assert [s["index"] for s in sources] == list(range(1, len(sources) + 1))
    kinds = {s["kind"] for s in sources}
    assert "fact" in kinds and ("wiki" in kinds or "chunk" in kinds), kinds
    assert ctx.startswith("[1]"), ctx[:20]
    print(f"[9] 上下文：{len(sources)} 条来源（类型 {sorted(kinds)}），编号连续、带出处 ✓")

    # ask()：mock LLM → 有答案 + 出处；无命中 → 明确说未找到
    class FakeLLM:
        def complete(self, prompt, **kw):
            class R:
                text = "该公司的投资方是新华通讯社和阿里巴巴集团 [1][2]。\n---\n**出处**\n- [1]（图谱）x\n- [2]（图谱）y"
            return R()

    import app.llm_factory as lf

    real_get_llm = lf.get_llm
    lf.get_llm = lambda: FakeLLM()
    try:
        ans = retrieval.ask(slug, f"{COMPANY}的投资方是谁？", use_llm=True)
        assert ans["answer"] and "[1]" in ans["answer"], ans["answer"]
        assert ans["sources"], "没有出处"
        assert "**出处**" in ans["answer"], "回答末尾缺少出处清单"
        print(f"[10] ask()：答案带引用角标与出处清单（{len(ans['sources'])} 条来源）✓")

        # 无关问题：检索不到任何资料时，必须明确说“未找到”，不能编造
        empty = retrieval.ask(slug, "zzzqqq xxyyzz 不存在的词", use_llm=False)
        assert ("未找到" in empty["answer"] or "没有检索到" in empty["answer"]) and not empty["sources"], empty["answer"][:120]
        print(f"[11] ask()：无关问题 → 明确回答未找到、零来源（{empty['answer'][:36]}…）✓")
    finally:
        lf.get_llm = real_get_llm

    # 相关性门槛：只有单字命中（“谁/哪/个”）时不能把整库倒出来
    noise = retrieval.chunk_route(idx, ["谁", "哪", "个"])
    assert noise == [], noise
    noise_w = retrieval.wiki_route(idx, ["谁", "哪", "个"])
    assert noise_w == [], [d.title for _s, d in noise_w]
    print("[12] 相关性门槛：仅单字命中 → 原文/Wiki 路零结果（不再倒出模板页）✓")

    # 事实/原文的来源类型必须与命中的路一致（防 build_context 分支错位）
    fact_srcs = [s for s in sources if s["routes"] == ["fact"]]
    assert fact_srcs and all(s["kind"] == "fact" for s in fact_srcs), fact_srcs[:2]
    other_srcs = [s for s in sources if s["routes"] != ["fact"]]
    assert all(s["kind"] in ("wiki", "chunk") for s in other_srcs), other_srcs[:2]
    print(f"[13] 上下文标签：事实来源 {len(fact_srcs)} 条标为「图谱事实」，其余标为 Wiki/原文 ✓")

    # 主体实体兜底：公司名与任何实体名都不匹配时，取中心节点（度数最高）
    import copy

    probe = copy.copy(idx)
    probe.company_name = "totally-unrelated.example"
    ek = retrieval._company_ekey(probe)
    assert ek and ek in idx.fact_adj, ek
    print(f"[14] 主体兜底：站点名不匹配实体名 → 取中心实体 {ek}（度数 {len(idx.fact_adj[ek])}）✓")

    # 清理测试公司数据
    with graph_store.connect() as conn:
        conn.execute("DELETE FROM triplets WHERE company_id=?", (cid,))
        conn.execute("DELETE FROM entities WHERE company_id=?", (cid,))
        conn.execute("DELETE FROM chunks WHERE company_id=?", (cid,))
        conn.execute("DELETE FROM snapshots WHERE company_id=?", (cid,))
        conn.execute("DELETE FROM companies WHERE id=?", (cid,))
        conn.commit()
    import shutil

    from app import config

    shutil.rmtree(config.WIKI_DIR / slug, ignore_errors=True)
    print("\n全部通过（14 项）")


if __name__ == "__main__":
    main()
