"""wiki_kb_service — 公司信息知识库 + LLM Wiki + DynamicLLMPathExtractor 图谱服务"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import config, graph_service, graph_store, image_service, jobs, retrieval, wiki
from .ingest import run_ingest, run_process_pending, run_reextract
from .llm_factory import llm_health

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="wiki_kb_service", version="1.0.0")


class IngestPayload(BaseModel):
    company: str = Field(..., description="公司名称")
    crawl_time: str | None = None
    note: str | None = None
    snapshot_id: str | None = None
    dry_run: bool = False
    extract: bool = Field(True, description="False 时只存原始内容（待处理），不做 LLM 抽取")
    pages: list[dict] = Field(default_factory=list, description="[{url,title,text?,html?,excerpt?}]")
    files: list[dict] = Field(default_factory=list, description="[{url,title,path,content_type,size}]")
    images: list[dict] = Field(default_factory=list, description="[{url,title,path,content_type,size}] 爬虫下载的图片（登记后按自动抽取开关处理）")


class ProcessPendingPayload(BaseModel):
    company: str = Field(..., description="公司名称或 slug")
    limit: int = Field(500, ge=1, le=2000)


class ReextractPayload(BaseModel):
    company: str = Field(..., description="公司名称或 slug")
    chunk_ids: list[int] = Field(..., description="要重新抽取的 chunk id 列表")


class AskPayload(BaseModel):
    company: str = Field(..., description="公司名称或 slug")
    question: str = Field(..., description="用户问题")
    use_llm: bool = Field(True, description="False 时只检索、不调用模型（调试用）")


@app.get("/api/chunks")
async def chunks(
    company: str = Query(...),
    snapshot: str | None = Query(None, description="快照 id；缺省/ all 表示全部"),
    limit: int = Query(1000, ge=1, le=5000),
):
    """列出该公司可重新抽取的内容分块（图谱页「重新抽取」面板用）。"""
    comp = graph_store.get_company_by_slug(_slug(company))
    if not comp:
        return {"company": company, "chunks": []}
    return {
        "company": str(comp["name"]),
        "chunks": graph_store.list_chunks(int(comp["id"]), snapshot_id=snapshot, limit=limit),
    }


@app.post("/api/reextract")
async def reextract(payload: ReextractPayload):
    """对选中的已有内容重新抽取 —— 后台任务，立即返回 job_id。

    * 旧数据只在新结果确实抽出来后才替换（同事务），失败不会丢数据；
    * 任务跑在服务端，浏览器关掉/刷新都不影响，回来用 GET /api/jobs/{id} 查进度。
    """
    comp = graph_store.get_company_by_slug(_slug(payload.company))
    if not comp:
        raise HTTPException(status_code=400, detail=f"公司不存在：{payload.company}")
    chunk_ids = [int(i) for i in payload.chunk_ids if str(i).strip()]
    if not chunk_ids:
        raise HTTPException(status_code=400, detail="chunk_ids 不能为空（请勾选要重新抽取的内容）")
    chunks = graph_store.get_chunks_by_ids(int(comp["id"]), chunk_ids)
    if not chunks:
        raise HTTPException(status_code=400, detail="选中的内容不存在，或不属于该公司")

    running = jobs.running_for(str(comp["name"]))
    if running:
        raise HTTPException(
            status_code=409,
            detail=f"该公司已有任务在运行（{running['kind']}，已耗时 {int((running.get('elapsed') or 0))} 秒），请等它结束再提交",
        )

    job = jobs.spawn(
        kind="reextract",
        company=str(comp["name"]),
        meta={"chunk_ids": [int(c["chunk_id"]) for c in chunks]},
        total=len(chunks),
        runner=lambda j: run_reextract(
            {**payload.model_dump(), "company": str(comp["name"])},
            progress=jobs.progress_setter(j),
        ),
    )
    return JSONResponse(
        {
            "ok": True,
            "job_id": job["id"],
            "status": job["status"],
            "selected_chunks": len(chunks),
            "message": "任务已在后台运行，可以关闭页面，稍后回来查看进度",
        }
    )


@app.get("/api/jobs/{job_id}")
async def job_detail(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或服务已重启")
    return job


@app.get("/api/jobs")
async def job_list(
    company: str | None = Query(None),
    kind: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    name = str(graph_store.get_company_by_slug(_slug(company))["name"]) if company else None
    return {"jobs": jobs.list_jobs(company=name, kind=kind, limit=limit)}


class DiffRequest(BaseModel):
    company: str
    snapshot_a: str
    snapshot_b: str


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "llm": await llm_health(),
        # 当前生效的抽取时间限制（来自 .env，改完需重启服务）
        "limits": config.describe_limits(),
        # 图片链路模型（OCR PaddleOCR-VL + 描述 provider/model）
        "image_llm": config.describe_image_llm(),
        "data_dir": str(config.DATA_DIR),
    }


@app.post("/api/ingest")
async def ingest(payload: IngestPayload):
    if config.INGEST_TOKEN:
        # 简单令牌校验：允许通过 header x-kb-token 或 query 传入（由 main 挂载前处理，这里简化为 body 校验）
        pass
    try:
        result = await run_ingest(payload.model_dump())
        return JSONResponse(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"ingest 失败：{e}")


@app.get("/api/pending")
async def pending(company: str = Query(...)):
    """指定公司尚未抽取（待处理）的 chunk 数量。"""
    comp = graph_store.get_company_by_slug(_slug(company))
    if not comp:
        return {"company": company, "pending": 0}
    return {
        "company": str(comp["name"]),
        "pending": graph_store.count_unprocessed_chunks(int(comp["id"])),
    }


@app.post("/api/process_pending")
async def process_pending(payload: ProcessPendingPayload):
    """处理尚未抽取的内容 —— 同样放进后台任务，关页面不影响。"""
    comp = graph_store.get_company_by_slug(_slug(payload.company))
    if not comp:
        raise HTTPException(status_code=400, detail=f"公司不存在：{payload.company}")
    running = jobs.running_for(str(comp["name"]))
    if running:
        raise HTTPException(
            status_code=409,
            detail=f"该公司已有任务在运行（{running['kind']}），请等它结束再提交",
        )
    pending_n = graph_store.count_unprocessed_chunks(int(comp["id"]))
    job = jobs.spawn(
        kind="process_pending",
        company=str(comp["name"]),
        meta={"limit": payload.limit},
        total=pending_n,
        runner=lambda j: run_process_pending(
            {**payload.model_dump(), "company": str(comp["name"])},
            progress=jobs.progress_setter(j),
        ),
    )
    return JSONResponse(
        {
            "ok": True,
            "job_id": job["id"],
            "status": job["status"],
            "pending": pending_n,
            "message": "任务已在后台运行，可以关闭页面，稍后回来查看进度",
        }
    )


class ImportResultsPayload(BaseModel):
    """crawler_results.json（爬虫 Agent 的落盘结果）导入 KB。"""

    results: dict = Field(..., description="crawler_results.json 的完整内容")
    company: str | None = Field(None, description="公司名；缺省时从 task 文本识别，再退回域名")
    extract: bool = Field(True, description="导入后是否立即抽取；False 则存为待处理")


_COMPANY_PATTERNS = [
    r"(?:爬取|抓取|采集|获取|分析)[：:]?\s*([^\s，。,,.：:]{2,24}?(?:公司|企业|集团|厂))[^，。]*官网?",
    r"([^\s，。,,.：:]{2,20}?(?:公司|企业|集团))(?:的)?(?:官网|网站|主页|信息|资料)",
]


def _company_from_results(results: dict) -> str:
    task = str(results.get("task") or "")
    for p in _COMPANY_PATTERNS:
        m = re.search(p, task)
        if m:
            return m.group(1).strip()
    # 兜底：第一个 kbPage 的域名
    kb = results.get("kbPages") or []
    if kb:
        url = str(kb[0].get("url") or "")
        m = re.match(r"https?://(?:www\.)?([^/]+)", url)
        if m:
            return m.group(1)
    return "未命名导入"


@app.post("/api/import_results")
async def import_results(payload: ImportResultsPayload):
    """把爬虫 Agent 的 crawler_results.json 导入知识库（补齐未经公司流程的爬取）。"""
    r = payload.results or {}
    kb_pages = r.get("kbPages") or []
    pages = [
        {"url": p.get("url"), "title": p.get("title"), "text": p.get("text") or p.get("excerpt") or ""}
        for p in kb_pages
        if (p.get("text") or p.get("excerpt"))
    ]
    files = [
        {
            "url": f.get("url"),
            "title": f.get("filename") or f.get("title"),
            "path": f.get("path"),
            "content_type": f.get("contentType"),
            "size": f.get("size"),
        }
        for f in (r.get("attachments") or [])
        if f.get("ok") and f.get("path")
    ]
    if not pages and not files:
        raise HTTPException(status_code=400, detail="results 中没有可导入的 kbPages / attachments 内容")
    ingest_payload = {
        "company": (payload.company or "").strip() or _company_from_results(r),
        "crawl_time": r.get("finishedAt") or r.get("startedAt"),
        "note": str(r.get("task") or "")[:200],
        "extract": payload.extract,
        "pages": pages,
        "files": files,
    }
    try:
        result = await run_ingest(ingest_payload)
        result["imported"] = {"pages": len(pages), "files": len(files)}
        return JSONResponse(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"导入失败：{e}")


@app.get("/api/companies")
async def companies():
    return {"companies": graph_store.list_companies()}


# ────────────────────────── 图片链路（仿「图谱与图片检索增强生成」） ──────────────────────────

@app.get("/api/images")
async def images(company: str = Query(...), status: str | None = Query(None)):
    """某公司的图片登记列表（含 OCR/描述状态与结果）。"""
    comp = graph_store.get_company_by_slug(_slug(company))
    if not comp:
        return {"company": company, "images": [], "stats": graph_store.image_stats(0)}
    statuses = [s.strip() for s in status.split(",")] if status else None
    return {
        "company": str(comp["name"]),
        "images": graph_store.list_images(int(comp["id"]), statuses),
        "stats": graph_store.image_stats(int(comp["id"])),
    }


@app.get("/api/images/{image_id}/file")
async def image_file(image_id: str, company: str = Query(...)):
    """返回图片原文件（前端缩略图 / 预览用）。"""
    comp = graph_store.get_company_by_slug(_slug(company))
    if not comp:
        raise HTTPException(status_code=404, detail="公司不存在")
    row = graph_store.get_image(int(comp["id"]), image_id)
    if not row or not row.get("file_path") or not Path(row["file_path"]).is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(row["file_path"])


class ProcessImagesPayload(BaseModel):
    company: str = Field(..., description="公司名或 slug")
    image_ids: list[str] = Field(default_factory=list, description="要处理的图片 id；空=全部待处理")


@app.post("/api/process_images")
async def process_images(payload: ProcessImagesPayload):
    """处理新图片（OCR → 视觉描述 → 入图）。后台 job，关页面不影响。"""
    comp = graph_store.get_company_by_slug(_slug(payload.company))
    if not comp:
        raise HTTPException(status_code=400, detail=f"公司不存在：{payload.company}")
    running = jobs.running_for(str(comp["name"]))
    if running:
        raise HTTPException(
            status_code=409,
            detail=f"该公司已有任务在运行（{running['kind']}），请等它结束再提交",
        )
    stats = graph_store.image_stats(int(comp["id"]))
    total = stats["pending"] if not payload.image_ids else len(payload.image_ids)
    job = jobs.spawn(
        kind="process_images",
        company=str(comp["name"]),
        meta={"image_ids": payload.image_ids, "reprocess": False},
        total=total,
        runner=lambda j: image_service.run_process_images(
            company_id=int(comp["id"]),
            company_slug=str(comp["slug"]),
            company_name=str(comp["name"]),
            image_ids=payload.image_ids or None,
            reprocess=False,
            progress=jobs.progress_setter(j),
        ),
    )
    return JSONResponse({"ok": True, "job_id": job["id"], "pending": total,
                         "message": "图片处理已在后台运行（很慢，可关闭页面稍后回来看进度）"})


@app.post("/api/reprocess_images")
async def reprocess_images(payload: ProcessImagesPayload):
    """重新处理选中的图片：先删除旧的三元组与识别结果，再重跑 OCR/描述/入图。"""
    comp = graph_store.get_company_by_slug(_slug(payload.company))
    if not comp:
        raise HTTPException(status_code=400, detail=f"公司不存在：{payload.company}")
    if not payload.image_ids:
        raise HTTPException(status_code=400, detail="重新处理需要先选中图片（image_ids 不能为空）")
    running = jobs.running_for(str(comp["name"]))
    if running:
        raise HTTPException(
            status_code=409,
            detail=f"该公司已有任务在运行（{running['kind']}），请等它结束再提交",
        )
    job = jobs.spawn(
        kind="reprocess_images",
        company=str(comp["name"]),
        meta={"image_ids": payload.image_ids, "reprocess": True},
        total=len(payload.image_ids),
        runner=lambda j: image_service.run_process_images(
            company_id=int(comp["id"]),
            company_slug=str(comp["slug"]),
            company_name=str(comp["name"]),
            image_ids=payload.image_ids,
            reprocess=True,
            progress=jobs.progress_setter(j),
        ),
    )
    return JSONResponse({"ok": True, "job_id": job["id"], "pending": len(payload.image_ids),
                         "message": "重新处理已在后台运行（旧三元组会在新结果产生后替换）"})


@app.get("/api/categories")
async def categories(company: str = Query(...)):
    return {"categories": graph_service.categories_of_company(_slug(company))}


@app.get("/api/snapshots")
async def snapshots(company: str = Query(...)):
    comp = graph_store.get_company_by_slug(_slug(company))
    if not comp:
        return {"snapshots": []}
    return {"snapshots": graph_store.list_snapshots(int(comp["id"]))}


@app.get("/api/graph/data")
async def graph_data(
    company: str = Query(...),
    category: str | None = Query(None),
    snapshot: str = Query("latest"),
    search: str | None = Query(None),
    limit: int = Query(300, ge=10, le=2000),
):
    return graph_service.get_graph_data(
        _slug(company), category=category, snapshot=snapshot, search=search, limit=limit
    )


@app.get("/api/graph/entity/{ekey:path}")
async def graph_entity(ekey: str, company: str = Query(...)):
    try:
        return graph_service.get_entity_detail(_slug(company), ekey)
    except KeyError:
        raise HTTPException(status_code=404, detail="实体不存在")


@app.get("/api/graph/diff")
async def graph_diff(
    company: str = Query(...),
    a: str = Query(...),
    b: str = Query(...),
):
    comp = graph_store.get_company_by_slug(_slug(company))
    if not comp:
        raise HTTPException(status_code=404, detail="公司不存在")
    return graph_store.diff_snapshots(int(comp["id"]), a, b)


@app.get("/api/wiki/pages")
async def wiki_pages(company: str = Query(...)):
    return {"pages": wiki.list_pages(_slug(company))}


@app.get("/api/wiki/page")
async def wiki_page(company: str = Query(...), name: str = Query(...)):
    content = wiki.read_page(_slug(company), name)
    if content is None:
        raise HTTPException(status_code=404, detail="页面不存在")
    return {"name": name, "content": content}


@app.get("/api/wiki/index")
async def wiki_index(company: str = Query(...)):
    return {"index": wiki.read_index(_slug(company)), "log": wiki.read_log(_slug(company))}


@app.post("/api/ask")
async def ask(payload: AskPayload):
    """零向量问答：LLM 查询理解 → 图谱/BM25 多路召回 → 带出处回答。

    全程不依赖向量库：图谱三元组精确作答 + BM25 全文兜底（参考 llm-wiki 通用检索做法）。
    """
    question = str(payload.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    try:
        result = await asyncio.to_thread(
            retrieval.ask, _slug(payload.company), question, payload.use_llm
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="公司不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"问答失败：{type(e).__name__}: {e}")
    return JSONResponse(result)


@app.get("/api/search")
async def search(
    company: str = Query(...),
    q: str = Query(..., description="检索词"),
    use_llm: bool = Query(False, description="是否用 LLM 做查询理解"),
):
    """只做检索、不生成回答：返回三路召回与 RRF 融合结果（调检索用）。"""
    question = str(q or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="检索词不能为空")
    slug = _slug(company)
    try:
        idx = await asyncio.to_thread(retrieval.get_index, slug)
        understood = await asyncio.to_thread(
            retrieval.understand_query, question, idx.company_name, use_llm
        )
        got = await asyncio.to_thread(retrieval.retrieve, slug, question, understood)
    except KeyError:
        raise HTTPException(status_code=404, detail="公司不存在")
    return {
        "understanding": understood,
        "routes": got["routes"],
        "query_terms": got["query_terms"][:40],
        "hits": [
            {
                "kind": h.kind,
                "key": h.key,
                "title": h.doc.title,
                "snippet": h.doc.text[:220],
                "routes": list(h.ranks.keys()),
                "rrf": round(h.score, 5),
            }
            for h in got["hits"][:20]
        ],
    }


def _slug(company: str) -> str:
    """允许前端传公司名或 slug。"""
    raw = str(company or "").strip()
    if graph_store.get_company_by_slug(raw):
        return raw
    return graph_store.slugify(raw)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/static/{path:path}")
async def static_files(path: str):
    target = (STATIC_DIR / path).resolve()
    if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target)
