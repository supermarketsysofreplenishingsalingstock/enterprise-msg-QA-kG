"""图片处理服务（仿「图谱与图片检索增强生成」项目）：

爬虫下载的图片 → 登记（sha1 去重落盘）→ 通道1 OCR → 通道2 视觉简述 → 并入知识图谱。

- 通道1（OCR）：本地 Ollama 的 PaddleOCR-VL 模型识别图中文字（汉字/字母/数字/符号），
  结果存 ocr_text；单次请求结束即卸载模型（keep_alive=0），给描述模型让显存；
- 通道2（简述）：多模态 LLM 看图生成描述，模型由 KB_IMG_DESCRIBE_PROVIDER 选择：
    local_ministral = 本地 Ollama 视觉模型（默认 ministral-3:14b-instruct-2512-q8_0）
    online          = OpenAI 兼容视觉端点（KB_ONLINE_VISION_BASE_URL/_API_KEY/_MODEL）
  本地视觉模型显存不足时自动改纯 CPU 重试一次；online 调用失败回退本地；
- 处理结果存 images 表（SQLite）；「入图」= 把 OCR 文本 + 描述交给与文本抽取同一套
  DynamicLLMPathExtractor 抽三元组（triplets.image_id 标记来源图片，可精确重建）；
- 处理图片很慢（本地 14B 单张可达 30~60 分钟），所以独立成 job：处理新图片 /
  重新处理图片按钮触发，不与文本处理混在一起。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from . import config, graph_store

_OLLAMA_TIMEOUT_DEFAULT = 600


# ────────────────────────── 图片落盘与登记 ──────────────────────────

_MAGIC_EXT = [
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"), (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
]


def _sniff_ext(data: bytes, fallback: str = ".png") -> str:
    for magic, ext in _MAGIC_EXT:
        if data.startswith(magic):
            return ext
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        fmt = (img.format or "").lower()
        return "." + (fmt if fmt in ("png", "jpeg", "gif", "bmp", "webp") else "png")
    except Exception:
        return fallback


def _image_size(data: bytes) -> tuple[int | None, int | None]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            return img.width, img.height
    except Exception:
        return None, None


def register_image_file(
    company_id: int,
    company_slug: str,
    src_path: str | Path,
    url: str,
    title: str,
    snapshot_id: str | None,
    crawl_time: str | None,
) -> str | None:
    """把爬虫下载的图片文件登记进 KB（sha1 去重，复制到 data/images/<slug>/）。

    返回 image_id；过小（图标）或重复登记返回 None。
    """
    src = Path(src_path)
    if not src.is_file():
        return None
    data = src.read_bytes()
    if len(data) < config.IMG_MIN_BYTES:
        return None
    ext = _sniff_ext(data, fallback=src.suffix.lower() or ".png")
    if ext not in config.SUPPORTED_IMAGE_EXTS:
        return None
    width, height = _image_size(data)
    if width and height and min(width, height) < config.IMG_MIN_SIDE:
        return None  # 过滤小图标
    image_id = hashlib.sha1(data).hexdigest()
    rel_dir = config.IMAGE_DIR / company_slug
    rel_dir.mkdir(parents=True, exist_ok=True)
    dest = rel_dir / f"{image_id[:16]}{ext}"
    if not dest.exists():
        shutil.copyfile(src, dest)
    name = (title or "").strip() or src.stem or image_id[:8]
    added = graph_store.insert_image(
        image_id=image_id,
        company_id=company_id,
        name=name[:120],
        url=url,
        file_path=str(dest),
        width=width,
        height=height,
        size=len(data),
        snapshot_id=snapshot_id,
        crawl_time=crawl_time,
    )
    return image_id if added else None


# ────────────────────────── 图片缩放 ──────────────────────────

def _scaled_image_bytes(path: Path) -> bytes:
    """读取图片并缩放到 IMG_MAX_SIDE 以内（不放大），返回 PNG/JPEG 字节。"""
    raw = path.read_bytes()
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        img.load()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        max_side = max(img.width, img.height)
        if max_side > config.IMG_MAX_SIDE:
            ratio = config.IMG_MAX_SIDE / float(max_side)
            img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))))
        buf = io.BytesIO()
        if path.suffix.lower() == ".png":
            img.save(buf, "PNG")
        else:
            img.save(buf, "JPEG", quality=90)
        return buf.getvalue()
    except Exception:
        return raw  # Pillow 处理失败时直接用原始字节


def _to_data_uri(path: Path) -> str:
    data = _scaled_image_bytes(path)
    ext = "png" if path.suffix.lower() == ".png" else "jpeg"
    return f"data:image/{ext};base64,{base64.b64encode(data).decode()}"


# ────────────────────────── 通道调用 ──────────────────────────

def _ollama_chat(
    model: str,
    prompt: str,
    image_path: Path | None = None,
    timeout: int = _OLLAMA_TIMEOUT_DEFAULT,
    num_ctx: int | None = None,
    keep_alive: str | int | None = None,
    num_gpu: int | None = None,
) -> str:
    """调用本地 Ollama /api/chat（可带一张图片），返回纯文本回复。

    模型不支持图片 / 显存不足 / 服务不可达时抛 RuntimeError。
    """
    base = str(config.LOCAL_MINISTRAL_BASE_URL).removesuffix("/v1").removesuffix("/v1/")
    base = base.rstrip("/")
    message: dict = {"role": "user", "content": prompt}
    if image_path is not None:
        message["images"] = [base64.b64encode(_scaled_image_bytes(image_path)).decode()]
    payload: dict = {
        "model": model,
        "messages": [message],
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": num_ctx or 8192},
    }
    if num_gpu is not None and num_gpu >= 0:
        payload["options"]["num_gpu"] = num_gpu
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    req = urllib.request.Request(
        base + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"Ollama 返回 {e.code}: {detail}") from e
    except Exception as e:
        raise RuntimeError(f"无法连接 Ollama（{base}）: {e}") from e
    return ((data.get("message") or {}).get("content") or "").strip()


def _looks_like_oom(err: Exception) -> bool:
    s = str(err).lower()
    return any(k in s for k in ("out-of-memory", "out of memory", "cuda error", "oom"))


def _online_vision_chat(profile: dict, prompt: str, image_path: Path) -> str:
    """OpenAI 兼容视觉端点：POST /chat/completions，图片以 base64 data URI 内嵌。"""
    url = str(profile["base_url"]).rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url.removesuffix("/v1").rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": profile["model"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": _to_data_uri(image_path)}},
                ],
            }
        ],
        "temperature": 0.1,
        "stream": False,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {profile.get('api_key') or 'not-needed'}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=config.IMG_DESCRIBE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"online 视觉端点返回 {e.code}: {detail}") from e
    except Exception as e:
        raise RuntimeError(f"无法连接 online 视觉端点（{url}）: {e}") from e
    return (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()


def ocr_image(path: Path) -> str:
    """通道1：PaddleOCR-VL 识别图中文字；无文字返回空串。"""
    text = _ollama_chat(
        config.IMG_OCR_MODEL,
        config.IMG_OCR_PROMPT,
        image_path=path,
        timeout=config.IMG_OCR_TIMEOUT,
        num_ctx=4096,
        keep_alive=config.IMG_OCR_KEEP_ALIVE,
    )
    return "" if text in ("无文字", "无文字。") else text


def describe_image(path: Path, ocr_text: str = "") -> tuple[str, str, str]:
    """通道2：生成图片简述。返回 (描述, 模型, 模式)。

    online 配置了模型则先走 online，失败回退本地；
    本地视觉模型显存不足时自动改纯 CPU 重试一次（很慢但能跑通）。
    """
    profile = config.get_img_describe_profile()
    errors: list[str] = []

    if profile["provider"] == "online":
        try:
            desc = _online_vision_chat(profile, config.IMG_DESCRIBE_PROMPT, path)
            if desc:
                return desc, profile["model"], "vision"
            errors.append("online 视觉端点返回空描述")
        except Exception as e:
            errors.append(f"online 视觉调用失败: {e}")

    # 本地 Ollama 视觉模型（online 未配置或失败时走这里）
    attempts: list[int | None] = [None]
    if config.IMG_DESCRIBE_CPU_RETRY:
        attempts.append(0)  # 纯 CPU 重试
    last_err: Exception | None = None
    for attempt_idx, num_gpu in enumerate(attempts):
        label = "纯 CPU" if num_gpu == 0 else "GPU/自动"
        try:
            if attempt_idx > 0:
                errors.append(f"显存不足，改用{label}重试视觉模型（可能非常慢）")
            desc = _ollama_chat(
                profile["model"],
                config.IMG_DESCRIBE_PROMPT,
                image_path=path,
                timeout=config.IMG_DESCRIBE_TIMEOUT,
                num_ctx=config.IMG_DESCRIBE_NUM_CTX,
                keep_alive=config.IMG_DESCRIBE_KEEP_ALIVE,
                num_gpu=num_gpu,
            )
            if desc:
                if errors and last_err is not None:
                    desc = desc + f"（视觉调用曾失败：{errors[-1]}）"
                return desc, profile["model"], "vision"
        except Exception as e:
            last_err = e
            errors.append(f"{label} 视觉调用失败: {e}")
            if not _looks_like_oom(e):
                break

    # 兜底：基于 OCR 文本的确定性描述（不再调模型，保证流程不中断）
    kind = "文档截图/图表" if ocr_text else "图片"
    note = f"（视觉模型不可用：{errors[-1] if errors else '未知原因'}）"
    return f"一张{kind}，图中主要文字内容见「图中文字」。{note}", "none", "fallback"


# ────────────────────────── 入图 ──────────────────────────

def build_image_text(row: dict) -> str:
    """把一张图片的两路信息合并为交给图谱抽取的文本。"""
    parts = [f"【图片】{row.get('name') or ''}"]
    if row.get("url"):
        parts.append(f"图片来源：{row['url']}")
    if row.get("description"):
        parts.append(f"图片简述：{row['description']}")
    if row.get("ocr_text"):
        parts.append(f"图中文字：\n{row['ocr_text']}")
    return "\n".join(parts)


async def ingest_image_to_graph(
    *,
    company_id: int,
    snapshot_id: str,
    crawl_time: str,
    row: dict,
    errors: list[str],
) -> int:
    """把已处理完成的图片文本送同一套开放式抽取器，三元组带 image_id 入图。"""
    from .extractor import extract_from_chunk, is_meaningful_triplet

    text = build_image_text(row)
    if not text.strip():
        return 0
    res = await extract_from_chunk(text)
    if res.get("error"):
        errors.append(f"图片 {row.get('name')}: {res['error']}")
        return 0
    n = 0
    for t in res["triplets"]:
        if not is_meaningful_triplet(t):
            continue
        src_ekey = graph_store.upsert_entity(company_id, t["head"], t["head_type"], t.get("head_props") or {}, snapshot_id)
        dst_ekey = graph_store.upsert_entity(company_id, t["tail"], t["tail_type"], t.get("tail_props") or {}, snapshot_id)
        if not src_ekey or not dst_ekey:
            continue
        graph_store.insert_triplet(
            company_id=company_id,
            snapshot_id=snapshot_id,
            crawl_time=crawl_time,
            src_ekey=src_ekey,
            dst_ekey=dst_ekey,
            src_name=t["head"],
            dst_name=t["tail"],
            src_type=t["head_type"],
            dst_type=t["tail_type"],
            relation=t["relation"],
            props={**t.get("relation_props", {})},
            source_url=str(row.get("url") or ""),
            source_title=f"图片：{row.get('name')}",
            source_kind="image",
            image_id=str(row["id"]),
        )
        n += 1
    return n


# ────────────────────────── 主流程（独立 job） ──────────────────────────

async def run_process_images(
    company_id: int,
    company_slug: str,
    company_name: str,
    image_ids: list[str] | None,
    reprocess: bool,
    progress=None,
) -> dict:
    """处理图片：OCR（通道1）→ 简述（通道2）→ 并入图谱。

    image_ids 为空 = 处理全部待处理（pending/failed）图片；
    reprocess=True 时先把选中的图片重置为 pending、删掉旧的三元组再重来。
    """
    from datetime import datetime

    def _p(**kw):
        if progress:
            progress(**kw)

    if reprocess and image_ids:
        old = graph_store.delete_image_triplets(company_id, image_ids)
        for iid in image_ids:
            graph_store.update_image(
                iid, status="pending", error=None, ocr_text="", description=""
            )
    rows = graph_store.list_images(
        company_id, statuses=["pending", "failed", "processing"] if not image_ids else None
    )
    if image_ids:
        want = set(image_ids)
        rows = [r for r in rows if r["id"] in want]
    if not rows:
        return {"ok": True, "processed": 0, "failed": 0, "triplets": 0, "message": "没有待处理的图片"}

    prof = config.describe_image_llm()
    total = len(rows)
    _p(done=0, total=total, ok=0, fail=0, stage="OCR 文字识别", current="")
    errors: list[str] = []
    ocr_done: list[tuple[dict, str]] = []

    # ---- 阶段1：OCR ----
    for i, row in enumerate(rows):
        iid = row["id"]
        path = Path(row["file_path"] or "")
        _p(done=i, total=total, ok=len(ocr_done), fail=0,
           stage="OCR 文字识别", current=row.get("name") or iid[:8])
        graph_store.update_image(iid, status="processing", error=None)
        if not path.is_file():
            graph_store.update_image(iid, status="failed", error="图片文件丢失")
            continue
        try:
            text = ocr_image(path)
            graph_store.update_image(
                iid, ocr_text=text, ocr_model=config.IMG_OCR_MODEL,
                ocr_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            ocr_done.append((row, text))
        except Exception as e:  # noqa: BLE001
            errors.append(f"图片 {row.get('name')}: OCR失败 {e}")
            graph_store.update_image(iid, status="failed", error=f"OCR失败: {e}")

    # ---- 阶段2：简述 ----
    ok = 0
    described: list[tuple[dict, str, str]] = []
    for i, (row, ocr_text) in enumerate(ocr_done):
        iid = row["id"]
        path = Path(row["file_path"] or "")
        _p(done=i, total=total, ok=ok, fail=len(errors),
           stage="视觉模型生成简述", current=row.get("name") or iid[:8])
        try:
            desc, dmodel, dmode = describe_image(path, ocr_text)
            graph_store.update_image(
                iid, description=desc, describe_model=dmodel, describe_mode=dmode,
                described_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                status="processed" if (ocr_text or desc) else "failed",
            )
            if ocr_text or desc:
                ok += 1
                described.append((dict(graph_store.get_image(company_id, iid) or row), ocr_text, desc))
        except Exception as e:  # noqa: BLE001
            errors.append(f"图片 {row.get('name')}: 描述失败 {e}")
            graph_store.update_image(iid, status="failed", error=f"描述失败: {e}")

    # ---- 阶段3：入图（与文本抽取同一套开放式抽取器）----
    triplet_count = 0
    _p(done=total, total=total, ok=ok, fail=len(errors), stage="图片信息入图", current="")
    for row, ocr_text, desc in described:
        try:
            n = await ingest_image_to_graph(
                company_id=company_id,
                snapshot_id=row.get("snapshot_id") or "images",
                crawl_time=row.get("crawl_time")
                or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                row={**row, "ocr_text": ocr_text, "description": desc},
                errors=errors,
            )
            triplet_count += n
            graph_store.mark_image_in_graph(company_id, row["id"], n)
        except Exception as e:  # noqa: BLE001
            errors.append(f"图片 {row.get('name')}: 入图失败 {e}")

    return {
        "ok": True,
        "company": company_name,
        "company_slug": company_slug,
        "processed": ok,
        "failed": total - ok,
        "triplet_count": triplet_count,
        "describe_provider": prof.get("provider"),
        "describe_model": prof.get("model"),
        "ocr_model": config.IMG_OCR_MODEL,
        "extraction_errors": errors[:10],
    }
