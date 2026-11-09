"""后台任务（job）管理：长耗时抽取脱离 HTTP 请求生命周期。

为什么需要：
  「重新抽取」「处理新增内容」要跑本地 LLM，动辄十几分钟。如果放在请求处理函数里，
  前端一关页面/一刷新就可能中断，用户也只能干等。这里改成：
    POST 接口 → 创建 job → asyncio.create_task 后台跑 → 立刻返回 job_id
  前端轮询 /api/jobs/{id} 拿进度，关掉页面任务照样继续跑；重新打开页面再查一次即可。

注意：任务状态保存在进程内存里，服务重启会丢失（重启后不会再继续跑），
      但已完成的数据已经落库（每个 chunk 都是原子替换），不会出现「抽一半把数据删了」。
"""
from __future__ import annotations

import asyncio
import time
import traceback
import uuid
from typing import Any, Awaitable, Callable

_MAX_JOBS = 100

_jobs: dict[str, dict] = {}
_tasks: dict[str, asyncio.Task] = {}

# 同一时刻只允许一个抽取任务碰 LLM / 图谱，避免并发写库互相踩
EXTRACT_LOCK = asyncio.Lock()

RUNNING = ("queued", "running")


def _now() -> float:
    return time.time()


def snapshot(job: dict) -> dict:
    """返回给前端的可序列化副本（不含内部私有字段）。"""
    return {k: v for k, v in job.items() if not k.startswith("_")}


def get(job_id: str) -> dict | None:
    job = _jobs.get(str(job_id or ""))
    return snapshot(job) if job else None


def list_jobs(company: str | None = None, kind: str | None = None, limit: int = 20) -> list[dict]:
    items = list(_jobs.values())
    if company:
        items = [j for j in items if str(j.get("company") or "") == str(company)]
    if kind:
        items = [j for j in items if j.get("kind") == kind]
    items.sort(key=lambda j: j.get("created_at") or 0, reverse=True)
    return [snapshot(j) for j in items[: int(limit)]]


def running_for(company: str, kind: str | None = None) -> dict | None:
    for j in _jobs.values():
        if j.get("status") in RUNNING and str(j.get("company") or "") == str(company):
            if kind is None or j.get("kind") == kind:
                return snapshot(j)
    return None


def _trim() -> None:
    if len(_jobs) <= _MAX_JOBS:
        return
    done = sorted(
        (j for j in _jobs.values() if j.get("status") not in RUNNING),
        key=lambda j: j.get("created_at") or 0,
    )
    for j in done[: max(0, len(_jobs) - _MAX_JOBS)]:
        _jobs.pop(j.get("id"), None)


def update(job: dict, **fields: Any) -> None:
    job.update(fields)


def progress_setter(job: dict) -> Callable[..., None]:
    """给 ingest 层用的进度回调：progress(done=1, total=2, stage="抽取中", current="#13")。"""

    def _set(**kw: Any) -> None:
        p = job.setdefault("progress", {})
        for k, v in kw.items():
            if v is not None:
                p[k] = v
        job["updated_at"] = _now()

    return _set


def spawn(
    *,
    kind: str,
    company: str,
    runner: Callable[[dict], Awaitable[dict]],
    meta: dict | None = None,
    total: int = 0,
) -> dict:
    """创建并启动后台任务，立即返回 job 快照（status=queued）。"""
    job: dict = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "company": company,
        "status": "queued",
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "elapsed": 0.0,
        "progress": {"done": 0, "total": int(total or 0), "stage": "排队中", "current": ""},
        "meta": meta or {},
        "result": None,
        "error": None,
        "warnings": [],
    }
    _jobs[job["id"]] = job
    _trim()

    async def _run() -> None:
        job["status"] = "running"
        job["started_at"] = _now()
        job["progress"]["stage"] = "启动中"
        try:
            job["result"] = await runner(job)
            job["status"] = "done"
            job["progress"]["stage"] = "完成"
        except Exception as e:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
            job["progress"]["stage"] = "失败"
            job["_trace"] = traceback.format_exc()[-3000:]
        finally:
            job["finished_at"] = _now()
            job["started_at"] = job["started_at"] or job["finished_at"]
            job["elapsed"] = round(job["finished_at"] - job["started_at"], 1)
            job["updated_at"] = _now()

    task = asyncio.create_task(_run())
    _tasks[job["id"]] = task
    task.add_done_callback(lambda _t: _tasks.pop(job["id"], None))
    return snapshot(job)
