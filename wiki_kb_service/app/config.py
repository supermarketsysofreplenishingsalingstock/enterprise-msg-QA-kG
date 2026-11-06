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
LLM_REQUEST_TIMEOUT = _env_int("KB_LLM_REQUEST_TIMEOUT", 600)
LLM_CONTEXT_WINDOW = _env_int("KB_LLM_CONTEXT_WINDOW", 16384)

# ── 抽取参数（全部开放式，不预设 schema）──
MAX_TRIPLETS_PER_CHUNK = _env_int("KB_MAX_TRIPLETS_PER_CHUNK", 25)
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
