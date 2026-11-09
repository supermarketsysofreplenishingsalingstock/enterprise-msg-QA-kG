"""
LLM 工厂：根据 .env 的 KB_LLM_PROVIDER 构造 LlamaIndex LLM。

三种提供方均为 OpenAI 兼容协议：
  - local_deepseek   -> 本地 Ollama / LM Studio / vLLM 等暴露的 /v1 端点
  - local_ministral  -> 同上
  - online_deepseek  -> https://api.deepseek.com/v1

针对 deepseek-r1 等推理模型，包装一层 <think>...</think> 剥离，
避免思考内容污染 DynamicLLMPathExtractor 的 JSON 解析。
"""
from __future__ import annotations

import re
import asyncio

from llama_index.core.llms import ChatMessage, CompletionResponse, ChatResponse
from llama_index.llms.openai_like import OpenAILike

from . import config

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_think(text: str) -> str:
    if not isinstance(text, str):
        return text
    return _THINK_RE.sub("", text).strip()


class ThinkStripLLM(OpenAILike):
    """OpenAILike + <think> 思考内容剥离。"""

    def chat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
        resp = super().chat(messages, **kwargs)
        try:
            content = resp.message.content
            if isinstance(content, str):
                resp.message.content = _strip_think(content)
        except Exception:
            pass
        return resp

    async def achat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
        resp = await super().achat(messages, **kwargs)
        try:
            content = resp.message.content
            if isinstance(content, str):
                resp.message.content = _strip_think(content)
        except Exception:
            pass
        return resp

    def complete(self, prompt: str, **kwargs) -> CompletionResponse:
        resp = super().complete(prompt, **kwargs)
        try:
            resp.text = _strip_think(resp.text)
        except Exception:
            pass
        return resp

    async def acomplete(self, prompt: str, **kwargs) -> CompletionResponse:
        resp = await super().acomplete(prompt, **kwargs)
        try:
            resp.text = _strip_think(resp.text)
        except Exception:
            pass
        return resp


_llm_instance: ThinkStripLLM | None = None


def build_llm() -> ThinkStripLLM:
    profile = config.get_llm_profile()
    api_key = profile.get("api_key") or "not-needed"
    # 关键：llama_index OpenAI/OpenAILike 的端点参数名是 api_base（不是 base_url）。
    # base_url 会被落入 **kwargs 被忽略，客户端回落到 https://api.openai.com → Connection error。
    return ThinkStripLLM(
        model=profile["model"],
        api_key=api_key,
        api_base=str(profile["base_url"]),
        is_chat_model=True,
        is_function_calling_model=False,
        temperature=config.LLM_TEMPERATURE,
        timeout=float(config.LLM_REQUEST_TIMEOUT),  # 注意：参数名是 timeout（request_timeout 会被忽略）
        max_retries=2,
        context_window=config.LLM_CONTEXT_WINDOW,
        max_tokens=config.LLM_CONTEXT_WINDOW,
    )


def get_llm() -> ThinkStripLLM:
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = build_llm()
    return _llm_instance


async def llm_health() -> dict:
    """探测当前 LLM 是否可用（一次最小补全）。"""
    llm = get_llm()
    try:
        await asyncio.wait_for(
            llm.acomplete("只回复 OK 两个字母。"), timeout=min(60, config.LLM_REQUEST_TIMEOUT)
        )
        return {"ok": True, **config.describe_llm()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300], **config.describe_llm()}
