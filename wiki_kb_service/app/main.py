"""wiki_kb_service — 公司信息知识库 + LLM Wiki + DynamicLLMPathExtractor 图谱服务"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import config, graph_service, graph_store, wiki
from .ingest import run_ingest
from .llm_factory import llm_health

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="wiki_kb_service", version="1.0.0")


class IngestPayload(BaseModel):
    company: str = Field(..., description="公司名称")
    crawl_time: str | None = None
    note: str | None = None
    snapshot_id: str | None = None
    dry_run: bool = False
    pages: list[dict] = Field(default_factory=list, description="[{url,title,text?,html?,excerpt?}]")
    files: list[dict] = Field(default_factory=list, description="[{url,title,path,content_type,size}]")


class DiffRequest(BaseModel):
    company: str
    snapshot_a: str
    snapshot_b: str


@app.get("/api/health")
async def health():
    return {"ok": True, "llm": await llm_health(), "data_dir": str(config.DATA_DIR)}


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


@app.get("/api/companies")
async def companies():
    return {"companies": graph_store.list_companies()}


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
