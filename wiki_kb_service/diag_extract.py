"""诊断：对新华智云的 chunk 跑一次真实抽取，记录 LLM 原始输出。"""
import asyncio
import json
import sqlite3
import sys

sys.path.insert(0, ".")


async def main():
    conn = sqlite3.connect("data/graph.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, text FROM chunks WHERE company_id=3 ORDER BY id LIMIT 1"
    ).fetchall()
    text = rows[0]["text"]
    print(f"[诊断] chunk {rows[0]['id']}，长度 {len(text)}")
    print(f"[诊断] 文本开头: {text[:120]!r}")

    from app import extractor

    ext = extractor.get_extractor()
    llm = ext.llm

    # 包一层记录 achat 的输入/输出（pydantic 模型需用 object.__setattr__）
    orig_achat = llm.achat
    log = {}

    async def logging_achat(messages, **kwargs):
        resp = await orig_achat(messages, **kwargs)
        log["prompt_head"] = str(messages[0].content if isinstance(messages, list) else messages)[:200]
        log["raw"] = str(resp.message.content or "")
        return resp

    object.__setattr__(llm, "achat", logging_achat)

    res = await extractor.extract_from_chunk(text)
    print(f"[诊断] 返回: error={res.get('error')!r}, triplets={len(res['triplets'])}")
    for t in res["triplets"][:5]:
        print("   ", json.dumps(t, ensure_ascii=False)[:200])
    print("\n[诊断] prompt 开头:", log.get("prompt_head", "")[:150])
    print("\n[诊断] LLM 原始输出（前 1500 字）:")
    print(log.get("raw", "(无记录)")[:1500])
    print("\n[诊断] LLM 原始输出（后 500 字）:")
    print(log.get("raw", "")[-500:])


asyncio.run(main())
