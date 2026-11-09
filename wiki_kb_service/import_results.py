"""把爬虫 Agent 的 crawler_results.json 导入知识库。

用法（在 wiki_kb_service 目录下）：
    python import_results.py <crawler_results.json 路径> [--company 公司名] [--no-extract] [--host http://127.0.0.1:13105]

说明：
- 不传 --company 时自动从任务的 task 文本识别公司名，识别不到就用首个页面域名
- 默认导入后立即抽取（调 LLM）；--no-extract 则只存为待处理，
  之后在图谱页点「处理新增内容」即可完成抽取
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="导入 crawler_results.json 到知识库")
    parser.add_argument("path", help="crawler_results.json 文件路径")
    parser.add_argument("--company", default="", help="公司名（缺省自动识别）")
    parser.add_argument("--no-extract", action="store_true", help="只存为待处理，不立即抽取")
    parser.add_argument("--host", default="http://127.0.0.1:13105", help="KB 服务地址")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(f"[导入] 文件不存在：{path}")
        return 1
    results = json.loads(path.read_text(encoding="utf-8"))
    kb = results.get("kbPages") or []
    att = [f for f in (results.get("attachments") or []) if f.get("ok") and f.get("path")]
    print(f"[导入] 读取 {path.name}：kbPages {len(kb)} 页 / 有效附件 {len(att)} 个")

    payload = {"results": results, "company": args.company, "extract": not args.no_extract}
    req = urllib.request.Request(
        f"{args.host.rstrip('/')}/api/import_results",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=1800) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[导入] 失败：HTTP {e.code} — {e.read().decode('utf-8', 'replace')[:400]}")
        return 1
    except urllib.error.URLError as e:
        print(f"[导入] 失败：连不上 KB 服务（{args.host}）— {e.reason}\n请先启动：python run.py")
        return 1

    if data.get("pending_chunks"):
        print(
            f"[导入] ✓ 已存为待处理：公司「{data['company']}」快照 {data['snapshot_id']}，"
            f"chunk {data['chunk_count']}；打开图谱界面点「处理新增内容」即可抽取"
        )
    else:
        print(
            f"[导入] ✓ 完成：公司「{data['company']}」快照 {data['snapshot_id']}，"
            f"三元组 {data['triplet_count']}，Wiki 页 {data['wiki_pages_updated']}"
        )
    print(f"[导入] 图谱界面：{args.host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
