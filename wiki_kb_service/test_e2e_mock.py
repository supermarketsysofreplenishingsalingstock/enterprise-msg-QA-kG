"""
端到端 mock 测试：伪造 LLM 输出，验证
extractor（宽松解析）→ SQLite 图谱 → 图谱数据 API → 快照 diff → Wiki 页面 全链路。
不依赖真实 LLM，可随时重跑（使用独立测试数据目录）。
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# 使用独立测试数据目录，避免污染正式数据
tmpdir = tempfile.mkdtemp(prefix="kb_test_")
os.environ.update(
    {
        "KB_DATA_DIR": tmpdir,
        "KB_DB_PATH": str(Path(tmpdir) / "kb.sqlite"),
        "KB_LLM_PROVIDER": "local_deepseek",
        "KB_LLM_API_KEY": "sk-mock",
        "KB_LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "KB_LLM_MODEL": "mock",
        "KB_WIKI_SUMMARY_ENABLED": "0",
        "KB_PROPS_MODE": "any",
    }
)

sys.path.insert(0, str(Path(__file__).parent))

MOCK_LLM_OUTPUT = json.dumps(
    [
        {
            "head": "杭州未来科技有限公司",
            "head_type": "公司",
            "head_props": {"成立时间": "2018年", "地址": "杭州市西湖区", "员工数": "500人"},
            "relation": "生产",
            "relation_props": {"起始年份": "2019年"},
            "tail": "工业机器人",
            "tail_type": "产品",
            "tail_props": {"型号": "RB-200"},
        },
        {  # 缺 head_type -> 应补"其他"
            "head": "王明",
            "head_props": {"职务": "总经理"},
            "relation": "担任",
            "relation_props": {},
            "tail": "杭州未来科技有限公司",
            "tail_type": "公司",
            "tail_props": {},
        },
        {  # 缺 relation -> 应补"关联"
            "head": "杭州未来科技有限公司",
            "head_type": "公司",
            "head_props": {},
            "relation_props": {},
            "tail": "浙江大学",
            "tail_type": "高校",
            "tail_props": {},
        },
    ],
    ensure_ascii=False,
)

MOCK_LLM_OUTPUT_2 = json.dumps(
    [  # 第二次快照：新增一条、属性变化
        {
            "head": "杭州未来科技有限公司",
            "head_type": "公司",
            "head_props": {"成立时间": "2018年", "地址": "杭州市滨江区", "员工数": "800人"},
            "relation": "合作",
            "relation_props": {},
            "tail": "上海机械集团",
            "tail_type": "公司",
            "tail_props": {},
        },
    ],
    ensure_ascii=False,
)


from llama_index.core.llms import CustomLLM, LLMMetadata, CompletionResponse


class FakeLLM(CustomLLM):
    """假 LLM：按 prompt 内容返回预置三元组 JSON；满足 pydantic 校验。"""

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(context_window=8000, num_output=4000, model_name="mock")

    def _resp_text(self, prompt: str) -> str:
        if "机器人" in prompt or "科技" in prompt or "产学研" in prompt:
            return MOCK_LLM_OUTPUT
        return MOCK_LLM_OUTPUT_2

    def _complete(self, prompt: str, **kwargs) -> CompletionResponse:
        return CompletionResponse(text=self._resp_text(prompt))

    async def _acomplete(self, prompt: str, **kwargs) -> CompletionResponse:
        return CompletionResponse(text=self._resp_text(prompt))

    def _stream(self, prompt: str, **kwargs):
        yield CompletionResponse(text=self._resp_text(prompt), delta=self._resp_text(prompt))

    async def _astream(self, prompt: str, **kwargs):
        yield CompletionResponse(text=self._resp_text(prompt), delta=self._resp_text(prompt))

    def complete(self, prompt: str, **kwargs) -> CompletionResponse:
        return self._complete(prompt, **kwargs)

    def stream_complete(self, prompt: str, **kwargs):
        return self._stream(prompt, **kwargs)


def main():
    from app import extractor, graph_store, graph_service
    import app.ingest as ingest

    fake = FakeLLM()

    # 替换单例 LLM（在 extractor 首次实例化之前）
    extractor.get_llm = lambda: fake
    ingest.get_llm = lambda: fake

    async def run():
        # ── 快照1：两个页面 ──
        r1 = await ingest.run_ingest(
            {
                "company": "杭州未来科技有限公司",
                "crawl_time": "2026-10-01 10:00:00",
                "pages": [
                    {"url": "https://example.com/about", "title": "公司简介", "text": "杭州未来科技有限公司 成立于2018年，王明担任总经理。公司生产工业机器人 RB-200，与浙江大学有产学研合作。"},
                    {"url": "https://example.com/products", "title": "产品中心", "text": "工业机器人 型号 RB-200，员工数 500人，地址 杭州市西湖区。"},
                ],
            }
        )
        print("== 快照1 ==", json.dumps(r1, ensure_ascii=False))
        assert r1["ok"] and r1["triplet_count"] >= 3, "快照1三元组数量异常"

        # ── 快照2：信息变化 ──
        r2 = await ingest.run_ingest(
            {
                "company": "杭州未来科技有限公司",
                "crawl_time": "2026-10-13 19:30:00",
                "pages": [
                    {"url": "https://example.com/about-2026", "title": "公司简介2026", "text": "公司地址变更为杭州市滨江区，员工增至 800人，与上海机械集团合作。"},
                ],
            }
        )
        print("== 快照2 ==", json.dumps(r2, ensure_ascii=False))

        # ── 图谱数据 ──
        g = graph_service.get_graph_data(r1["company_slug"], category=None, snapshot="latest", search=None, limit=200)
        print("== 图谱(nodes/edges) ==", len(g["nodes"]), "/", len(g["edges"]), "counts:", g["counts"])
        assert len(g["nodes"]) >= 4, "图谱节点数量异常"
        n0 = g["nodes"][0]
        print("  样例节点:", n0["name"], n0["type"], "props:", n0.get("properties"))
        comp_node = next(n for n in g["nodes"] if n["name"] == "杭州未来科技有限公司")
        print("  公司节点 props:", comp_node.get("properties"))
        assert comp_node.get("properties"), "公司节点应带属性（属性合并自最新快照）"
        assert "地址" in comp_node["properties"] and "员工数" in comp_node["properties"], "属性应有：地址/员工数"

        # ── 按类别过滤 ──
        cats = graph_service.categories_of_company(r1["company_slug"])
        print("== 类别 ==", cats)
        g2 = graph_service.get_graph_data(r1["company_slug"], category=cats[0]["category"], snapshot="all", search=None, limit=200)
        print("== 类别过滤后节点 ==", len(g2["nodes"]))

        # ── 搜索（含邻居） ──
        g3 = graph_service.get_graph_data(r1["company_slug"], category=None, snapshot="all", search="王明", limit=200)
        print("== 搜索'王明' ==", [n["name"] for n in g3["nodes"]])
        assert any(n["name"] == "王明" for n in g3["nodes"]), "搜索应命中王明"

        # ── 快照 diff ──
        snaps = graph_store.list_snapshots_by_slug(r1["company_slug"])
        a, b = snaps[0]["snapshot_id"], snaps[-1]["snapshot_id"]
        comp_row = graph_store.get_company_by_slug(r1["company_slug"])
        diff = graph_store.diff_snapshots(int(comp_row["id"]), a, b)
        print("== diff ==", json.dumps({k: len(v) if isinstance(v, list) else v for k, v in diff.items()}, ensure_ascii=False))

        # ── 实体详情 ──
        comp_ekey = graph_store.norm_ekey("杭州未来科技有限公司")
        detail = graph_service.get_entity_detail(r1["company_slug"], comp_ekey)
        print("== 实体详情 ==", detail["name"], detail["type"], "props keys:", list(detail["properties"].keys()), "relations:", len(detail["relations"]))
        assert detail["properties"], "公司实体详情必须带属性"

        # ── Wiki 文件 ──
        wiki_dir = Path(tmpdir) / "wiki" / r1["company_slug"]
        pages = sorted(p.name for p in (wiki_dir / "pages").glob("*.md"))
        raw = sorted(p.name for p in (wiki_dir / "raw").rglob("*.md"))
        print("== wiki pages ==", pages)
        print("== wiki raw ==", raw[:5], f"... 共{len(raw)}个")
        print("== index.md 头10行 ==")
        print("\n".join((wiki_dir / "index.md").read_text(encoding="utf-8").splitlines()[:10]))
        print("== log.md ==")
        print("\n".join((wiki_dir / "log.md").read_text(encoding="utf-8").splitlines()))
        assert "company.md" in pages, "公司总览页缺失"
        assert raw, "原始资料层缺失"

        print("\n✅ 端到端 mock 测试全部通过；数据目录:", tmpdir)

    asyncio.run(run())


if __name__ == "__main__":
    main()
