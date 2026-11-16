"""
wiki_kb_service 配置：全部从 .env 读取，前缀 KB_。

LLM 三选一（KB_LLM_PROVIDER）：
  - local_deepseek   本地 DeepSeek（Ollama / LM Studio / vLLM 等任意 OpenAI 兼容端点）
  - local_ministral  本地 Ministral（同上，OpenAI 兼容端点）
  - online_deepseek  线上 DeepSeek API（https://api.deepseek.com）
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# 优先读服务目录下的 .env
load_dotenv(BASE_DIR / ".env")
# 兜底再读一次 Extractor_Agent 的 .env（若存在，方便共用密钥）
load_dotenv(BASE_DIR.parent / "Extractor_Agent" / ".env")


def _env(key: str, default: str = "") -> str:
    return str(os.getenv(key, default) or default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(float(os.getenv(key, str(default))))
    except (TypeError, ValueError):
        return default


# ── 服务 ──
HOST = _env("KB_HOST", "127.0.0.1")
PORT = _env_int("KB_PORT", 13105)
DATA_DIR = Path(_env("KB_DATA_DIR", str(BASE_DIR / "data")))
WIKI_DIR = DATA_DIR / "wiki"
GRAPH_DB_PATH = DATA_DIR / "graph.db"
DATA_DIR.mkdir(parents=True, exist_ok=True)
WIKI_DIR.mkdir(parents=True, exist_ok=True)

# ── LLM 选择 ──
LLM_PROVIDER = _env("KB_LLM_PROVIDER", "local_deepseek").lower()

LOCAL_DEEPSEEK_BASE_URL = _env("KB_LOCAL_DEEPSEEK_BASE_URL", "http://localhost:11434/v1")
LOCAL_DEEPSEEK_MODEL = _env("KB_LOCAL_DEEPSEEK_MODEL", "deepseek-r1:14b")
LOCAL_DEEPSEEK_API_KEY = _env("KB_LOCAL_DEEPSEEK_API_KEY", "ollama")

LOCAL_MINISTRAL_BASE_URL = _env("KB_LOCAL_MINISTRAL_BASE_URL", "http://localhost:11434/v1")
LOCAL_MINISTRAL_MODEL = _env("KB_LOCAL_MINISTRAL_MODEL", "ministral-8b")
LOCAL_MINISTRAL_API_KEY = _env("KB_LOCAL_MINISTRAL_API_KEY", "ollama")

ONLINE_DEEPSEEK_BASE_URL = _env("KB_ONLINE_DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
ONLINE_DEEPSEEK_API_KEY = _env("KB_ONLINE_DEEPSEEK_API_KEY", "")
ONLINE_DEEPSEEK_MODEL = _env("KB_ONLINE_DEEPSEEK_MODEL", "deepseek-chat")

LLM_TEMPERATURE = float(_env("KB_LLM_TEMPERATURE", "0.1") or 0.1)
# 单次 LLM 请求超时（秒）。改这里就能改「抽取运行时间限制」：
# 一个 chunk 要跑 2 次 LLM 调用（关系轮 + 属性轮），
# 单 chunk 最坏耗时 ≈ LLM_REQUEST_TIMEOUT × (1 + LLM_MAX_RETRIES) × 2
LLM_REQUEST_TIMEOUT = _env_int("KB_LLM_REQUEST_TIMEOUT", 600)
# 失败重试次数（0=只试一次）。它与超时相乘，是「一个 chunk 卡多久」的另一个乘数
LLM_MAX_RETRIES = _env_int("KB_LLM_MAX_RETRIES", 2)
LLM_CONTEXT_WINDOW = _env_int("KB_LLM_CONTEXT_WINDOW", 16384)

# ── 抽取参数（全部开放式，不预设 schema）──
# 默认 90：开放体系下要抽的类别变多（组织/客户/产品细节/技术/里程碑等），60 容易截断
MAX_TRIPLETS_PER_CHUNK = _env_int("KB_MAX_TRIPLETS_PER_CHUNK", 90)
CHUNK_SIZE = _env_int("KB_CHUNK_SIZE", 1200)
CHUNK_OVERLAP = _env_int("KB_CHUNK_OVERLAP", 150)
# 属性生成模式：any=LLM 自由生成任意属性；none=关闭属性生成；或逗号分隔的属性名列表
PROPS_MODE = _env("KB_PROPS_MODE", "any").lower()
# 抽取并发 worker
NUM_WORKERS = _env_int("KB_NUM_WORKERS", 4)

# ── Wiki 页面生成 ──
WIKI_SUMMARY_MAX_ENTITIES = _env_int("KB_WIKI_SUMMARY_MAX_ENTITIES", 10)
WIKI_SUMMARY_ENABLED = _env("KB_WIKI_SUMMARY_ENABLED", "1") not in ("0", "false", "False")

# ── 爬虫回传 ──
INGEST_TOKEN = _env("KB_INGEST_TOKEN", "")

# ── 图片链路（仿「图谱与图片检索增强生成」：OCR 通道 + 视觉描述通道 + 入图）──
IMAGE_DIR = DATA_DIR / "images"          # 图片落盘根目录（按公司 slug 分子目录）
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

# 通道1 OCR：本地 Ollama 里的 PaddleOCR-VL 视觉模型（支持汉字/字母/数字/符号）
IMG_OCR_MODEL = _env("KB_IMG_OCR_MODEL", "enaval/PaddleOCR-VL-1.6-0.9B-Backup:latest")
IMG_OCR_TIMEOUT = _env_int("KB_IMG_OCR_TIMEOUT", 600)
IMG_OCR_KEEP_ALIVE = _env("KB_IMG_OCR_KEEP_ALIVE", "0")   # 0=识别完立即卸载，给描述模型让显存
IMG_OCR_PROMPT = _env(
    "KB_IMG_OCR_PROMPT",
    "请识别图片中的所有文字（包括汉字、字母、数字、符号），按阅读顺序原样输出，"
    "不要翻译、不要总结、不要添加不存在的内容。图中没有文字时只输出：无文字。",
)

# 通道2 图片简述：local_ministral（本地 Ollama 视觉）或 online（OpenAI 兼容视觉端点，用户可选）
IMG_DESCRIBE_PROVIDER = _env("KB_IMG_DESCRIBE_PROVIDER", "local_ministral").lower()  # local_ministral | online
IMG_DESCRIBE_MODEL = _env("KB_IMG_DESCRIBE_MODEL", "ministral-3:14b-instruct-2512-q8_0")
IMG_DESCRIBE_TIMEOUT = _env_int("KB_IMG_DESCRIBE_TIMEOUT", 7200)   # 本地 14B 单张可能 30~60 分钟
IMG_DESCRIBE_KEEP_ALIVE = _env("KB_IMG_DESCRIBE_KEEP_ALIVE", "2h")  # 批处理期间驻留，避免每张重新加载
IMG_DESCRIBE_NUM_CTX = _env_int("KB_IMG_DESCRIBE_NUM_CTX", 2048)    # 小上下文减少 KV cache，缓解显存不足
# 视觉调用失败（显存不足）时是否改用纯 CPU 重试一次（很慢但能跑通）
IMG_DESCRIBE_CPU_RETRY = _env("KB_IMG_DESCRIBE_CPU_RETRY", "1") not in ("0", "false", "False")

# online 视觉端点（OpenAI 兼容 /chat/completions，支持 image_url 的 base64 数据）。
# 注意：DeepSeek 官方 API 目前没有视觉模型；需要视觉能力时把 BASE_URL/MODEL 指向
# 任意 OpenAI 兼容视觉服务（如自建 vLLM 部署的 Qwen-VL、OpenRouter 的视觉模型等）。
# 只填 API_KEY 不填 MODEL 时该通道视为未配置，自动回退本地 ministral。
ONLINE_VISION_BASE_URL = _env("KB_ONLINE_VISION_BASE_URL", "https://api.deepseek.com/v1")
ONLINE_VISION_API_KEY = _env("KB_ONLINE_VISION_API_KEY", "")
ONLINE_VISION_MODEL = _env("KB_ONLINE_VISION_MODEL", "")

IMG_DESCRIBE_PROMPT = _env(
    "KB_IMG_DESCRIBE_PROMPT",
    "请用中文简要描述这张图片：先说明它是什么类型的图（照片/文档截图/表格/图表/示意图等），"
    "再概括主要内容与要点，3~5 句话。不要逐字罗列图中文字。",
)

# 送模型前把长边缩放到该值以内（控制显存与耗时）；小图/图标过滤
IMG_MAX_SIDE = _env_int("KB_IMG_MAX_SIDE", 1568)
IMG_MIN_BYTES = _env_int("KB_IMG_MIN_BYTES", 3072)
IMG_MIN_SIDE = _env_int("KB_IMG_MIN_SIDE", 96)
SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}


def get_img_describe_profile() -> dict:
    """图片描述通道配置：provider / model / base_url / api_key。"""
    if IMG_DESCRIBE_PROVIDER == "online" and ONLINE_VISION_MODEL:
        return {
            "provider": "online",
            "model": ONLINE_VISION_MODEL,
            "base_url": ONLINE_VISION_BASE_URL,
            "api_key": ONLINE_VISION_API_KEY or "not-needed",
        }
    if IMG_DESCRIBE_PROVIDER == "online" and not ONLINE_VISION_MODEL:
        # online 未配置模型 → 回退本地
        return {
            "provider": "local_ministral(online未配置回退)",
            "model": IMG_DESCRIBE_MODEL,
            "base_url": LOCAL_MINISTRAL_BASE_URL,
            "api_key": LOCAL_MINISTRAL_API_KEY,
        }
    return {
        "provider": "local_ministral",
        "model": IMG_DESCRIBE_MODEL,
        "base_url": LOCAL_MINISTRAL_BASE_URL,
        "api_key": LOCAL_MINISTRAL_API_KEY,
    }


def describe_image_llm() -> dict:
    """当前图片链路模型信息（健康检查 / 日志用）。"""
    return {
        "ocr_model": IMG_OCR_MODEL,
        **get_img_describe_profile(),
    }


def resolve_props_mode() -> "list[str] | None":
    """返回传给 DynamicLLMPathExtractor 的 props 参数。

    按官方示例语义：`[]` = LLM 自由生成任意属性；`None` = 跳过属性生成。
    """
    if PROPS_MODE in ("none", "off", "skip"):
        return None
    if PROPS_MODE in ("any", "free", ""):
        return []
    return [p.strip() for p in PROPS_MODE.split(",") if p.strip()]


def describe_llm() -> dict:
    return {
        "provider": LLM_PROVIDER,
        "model": get_llm_profile()["model"],
        "base_url": get_llm_profile()["base_url"],
    }


def describe_limits() -> dict:
    """当前生效的「抽取运行时间限制」，用于启动日志 / 健康检查核对 .env 是否生效。"""
    per_chunk_worst = LLM_REQUEST_TIMEOUT * (1 + max(0, LLM_MAX_RETRIES)) * 2
    return {
        "request_timeout": LLM_REQUEST_TIMEOUT,
        "max_retries": LLM_MAX_RETRIES,
        "num_workers": NUM_WORKERS,
        "context_window": LLM_CONTEXT_WINDOW,
        # 单 chunk 最坏耗时（秒）：2 次 LLM 调用 ×（1 次首试 + N 次重试）
        "per_chunk_worst_sec": per_chunk_worst,
    }


def get_llm_profile() -> dict:
    if LLM_PROVIDER == "local_ministral":
        return {
            "base_url": LOCAL_MINISTRAL_BASE_URL,
            "model": LOCAL_MINISTRAL_MODEL,
            "api_key": LOCAL_MINISTRAL_API_KEY,
        }
    if LLM_PROVIDER == "online_deepseek":
        return {
            "base_url": ONLINE_DEEPSEEK_BASE_URL,
            "model": ONLINE_DEEPSEEK_MODEL,
            "api_key": ONLINE_DEEPSEEK_API_KEY,
        }
    # 默认本地 deepseek
    return {
        "base_url": LOCAL_DEEPSEEK_BASE_URL,
        "model": LOCAL_DEEPSEEK_MODEL,
        "api_key": LOCAL_DEEPSEEK_API_KEY,
    }
