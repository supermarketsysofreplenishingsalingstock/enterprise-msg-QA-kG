"""
开放式图谱抽取：直接使用 LlamaIndex 的 DynamicLLMPathExtractor。

按需求刻意保持"全开放"：
- allowed_entity_types=None   -> LLM 自由判断实体类型（不预设实体）
- allowed_relation_types=None -> LLM 自由判断关系类型（不预设关系）
- allowed_entity_props / allowed_relation_props = [] -> LLM 自由生成任意属性
  （属性提取是硬需求：公司网站上的任何信息都可能成为属性）
- 自定义宽松 parse_fn：不丢弃任何可解析出的三元组
  （默认解析器会丢弃缺 head_type/tail_type/relation 的三元组，不符合需求；
   这里缺类型补"其他"、缺关系补"关联"，全部保留）
- 不做任何 schema 过滤
"""
from __future__ import annotations

import json
import re
from typing import Any, List, Tuple

from llama_index.core.indices.property_graph import DynamicLLMPathExtractor
from llama_index.core.indices.property_graph.transformations.dynamic_llm import (  # noqa: F401
    default_parse_dynamic_triplets,
    default_parse_dynamic_triplets_with_props,
)
from llama_index.core.indices.property_graph.transformations.schema_llm import (
    KG_NODES_KEY,
    KG_RELATIONS_KEY,
)
from llama_index.core.schema import TextNode
from llama_index.core.graph_stores.types import EntityNode, Relation

from . import config
from .llm_factory import get_llm

_extractor_instance: DynamicLLMPathExtractor | None = None

_FALLBACK_ENTITY_TYPE = "其他"
_FALLBACK_RELATION = "关联"


def _norm_props(v: Any) -> dict:
    if isinstance(v, dict):
        return v
    if v is None or v == "":
        return {}
    if isinstance(v, (list, tuple)):
        return {"value": v}
    return {"value": v}


def _make_triplet(
    head: Any,
    head_type: Any,
    head_props: Any,
    relation: Any,
    relation_props: Any,
    tail: Any,
    tail_type: Any,
    tail_props: Any,
) -> Tuple[EntityNode, Relation, EntityNode] | None:
    """构造三元组；缺字段用兜底值补齐，而不是丢弃。只剔除头尾名完全为空的。"""
    head_s = str(head or "").strip()
    tail_s = str(tail or "").strip()
    rel_s = str(relation or "").strip()
    if not head_s or not tail_s:
        return None
    head_node = EntityNode(
        name=head_s,
        label=str(head_type or "").strip() or _FALLBACK_ENTITY_TYPE,
        properties=_norm_props(head_props),
    )
    tail_node = EntityNode(
        name=tail_s,
        label=str(tail_type or "").strip() or _FALLBACK_ENTITY_TYPE,
        properties=_norm_props(tail_props),
    )
    rel_node = Relation(
        source_id=head_node.id,
        target_id=tail_node.id,
        label=rel_s or _FALLBACK_RELATION,
        properties=_norm_props(relation_props),
    )
    return head_node, rel_node, tail_node


def _extract_json_block(llm_output: str) -> str:
    """从可能的 markdown 代码块/前后缀文本中剥出 JSON 数组本体。"""
    text = (llm_output or "").strip()
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    # 找第一个 [ 到最后一个 ]
    i, j = text.find("["), text.rfind("]")
    if i != -1 and j > i:
        return text[i : j + 1]
    return text


def lenient_parse_fn_with_props(llm_output: str) -> List[Tuple[EntityNode, Relation, EntityNode]]:
    """宽松解析：永不丢弃三元组（用户要求保留一切可提取内容）。

    优先整体 JSON 解析；失败时逐条 JSON 解析；再失败用正则逐字段抓取。
    缺 head_type/tail_type -> "其他"，缺 relation -> "关联"。
    """
    triplets: List[Tuple[EntityNode, Relation, EntityNode]] = []
    body = _extract_json_block(llm_output)
    data: Any = None

    # 1) 整体 JSON
    try:
        data = json.loads(body)
    except Exception:
        data = None

    items: list[dict] = []
    if isinstance(data, list):
        items = [x for x in data if isinstance(x, dict)]
    elif isinstance(data, dict):
        items = [data]
    else:
        # 2) 逐条尝试：从原文中按 { ... } 分块解析
        for m in re.finditer(r"\{[^{}]*\}", llm_output or "", re.DOTALL):
            try:
                item = json.loads(m.group(0))
                if isinstance(item, dict):
                    items.append(item)
            except Exception:
                continue

    for item in items:
        t = _make_triplet(
            item.get("head"),
            item.get("head_type"),
            item.get("head_props", {}),
            item.get("relation"),
            item.get("relation_props", {}),
            item.get("tail"),
            item.get("tail_type"),
            item.get("tail_props", {}),
        )
        if t:
            triplets.append(t)

    if triplets:
        return triplets

    # 3) 最后兜底：正则逐字段抓取（容忍字段缺失/乱序引号）
    pattern = re.compile(
        r"['\"]?head['\"]?\s*[:：]\s*['\"](.*?)['\"]\s*,?\s*"
        r"(?:['\"]?head_type['\"]?\s*[:：]\s*['\"](.*?)['\"]\s*,?\s*)?"
        r"(?:['\"]?head_props['\"]?\s*[:：]\s*\{(.*?)\}\s*,?\s*)?"
        r"['\"]?relation['\"]?\s*[:：]\s*['\"](.*?)['\"]\s*,?\s*"
        r"(?:['\"]?relation_props['\"]?\s*[:：]\s*\{(.*?)\}\s*,?\s*)?"
        r"['\"]?tail['\"]?\s*[:：]\s*['\"](.*?)['\"]\s*,?\s*"
        r"(?:['\"]?tail_type['\"]?\s*[:：]\s*['\"](.*?)['\"]\s*,?\s*)?"
        r"(?:['\"]?tail_props['\"]?\s*[:：]\s*\{(.*?)\})?",
        re.DOTALL,
    )
    for match in pattern.finditer(llm_output or ""):
        head, head_type, head_props, relation, relation_props, tail, tail_type, tail_props = (
            match.group(1),
            match.group(2),
            match.group(3),
            match.group(4),
            match.group(5),
            match.group(6),
            match.group(7),
            match.group(8),
        )

        def parse_props(props_str: str | None) -> dict:
            if not props_str:
                return {}
            try:
                return json.loads("{" + props_str.replace("'", '"') + "}")
            except Exception:
                return {}

        t = _make_triplet(
            head, head_type, parse_props(head_props),
            relation, parse_props(relation_props),
            tail, tail_type, parse_props(tail_props),
        )
        if t:
            triplets.append(t)

    return triplets


def get_extractor() -> DynamicLLMPathExtractor:
    global _extractor_instance
    if _extractor_instance is None:
        _extractor_instance = DynamicLLMPathExtractor(
            llm=get_llm(),
            max_triplets_per_chunk=config.MAX_TRIPLETS_PER_CHUNK,
            num_workers=config.NUM_WORKERS,
            # 不预设实体类型与关系类型 —— LLM 现场归纳
            allowed_entity_types=None,
            allowed_relation_types=None,
            # 属性：开放生成（语义见 config.resolve_props_mode 的注释）
            allowed_entity_props=config.resolve_props_mode(),
            allowed_relation_props=config.resolve_props_mode(),
            # 宽松解析：不丢弃任何能解析出的三元组
            parse_fn=lenient_parse_fn_with_props,
            # 关键：默认 False 会把 LLM 调用异常（如连不上）吞掉并返回 0 三元组，
            # 导致分块被误标"已处理"。改为 True 让错误上抛，待处理分块保留可重试。
            raise_on_error=True,
        )
    return _extractor_instance


def chunk_text(text: str) -> list[str]:
    """按字符长度切分（中文友好），带 overlap。"""
    size = max(200, config.CHUNK_SIZE)
    overlap = min(200, max(0, config.CHUNK_OVERLAP))
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    out: list[str] = []
    start = 0
    step = size - overlap
    while start < len(text):
        piece = text[start : start + size]
        if piece.strip():
            out.append(piece.strip())
        if start + size >= len(text):
            break
        start += step
    return out


def make_node(text: str) -> TextNode:
    return TextNode(text=text)


async def extract_from_chunk(text: str) -> dict:
    """对单 chunk 跑开放式抽取，返回标准化的 triplets 列表。

    注意1：DynamicLLMPathExtractor.__call__ 返回 List[BaseNode]，
    三元组写在 node.metadata[KG_NODES_KEY]（实体）/ KG_RELATIONS_KEY（关系）里，
    并不是 (entities, relations) 元组 —— 这里按实际返回结构解析。
    注意2：同步 __call__ 内部用 asyncio.run，在 async 上下文（ingest）里会冲突，
    因此这里直接 await acall()。

    返回元素结构（全部保留，不做过滤）：
      {head, head_type, head_props, relation, relation_props, tail, tail_type, tail_props}
    """
    extractor = get_extractor()
    node = make_node(text)
    try:
        nodes = await extractor.acall([node])
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:400], "triplets": []}

    entities: list = []
    relations: list = []
    for n in nodes or []:
        md = getattr(n, "metadata", None) or {}
        ents = md.get(KG_NODES_KEY) or []
        rels = md.get(KG_RELATIONS_KEY) or []
        if ents:
            entities.extend(ents)
        if rels:
            relations.extend(rels)

    ent_by_id: dict[str, dict] = {}
    for ent in entities:
        eid = str(getattr(ent, "id", None) or getattr(ent, "id_", None) or "")
        ent_by_id[eid] = {
            "name": str(getattr(ent, "name", "") or ""),
            "type": str(getattr(ent, "label", "") or ""),
            "props": dict(getattr(ent, "properties", {}) or {}),
        }

    triplets: list[dict] = []
    for rel in relations:
        src_id = str(getattr(rel, "source_id", "") or "")
        dst_id = str(getattr(rel, "target_id", "") or "")
        src = ent_by_id.get(src_id, {})
        dst = ent_by_id.get(dst_id, {})
        if not src.get("name") or not dst.get("name"):
            continue
        triplets.append(
            {
                "head": src["name"],
                "head_type": src.get("type", ""),
                "head_props": _clean_props(src.get("props")),
                "relation": str(getattr(rel, "label", "") or ""),
                "relation_props": _clean_props(getattr(rel, "properties", {}) or {}),
                "tail": dst["name"],
                "tail_type": dst.get("type", ""),
                "tail_props": _clean_props(dst.get("props")),
            }
        )
    return {"error": None, "triplets": triplets}


_HIDE_PROPS = {"triplet_source_id", "embedding", "id", "source_id", "target_id"}


def _clean_props(props: dict | None) -> dict:
    out = {}
    for k, v in (props or {}).items():
        if k in _HIDE_PROPS or v is None:
            continue
        if isinstance(v, (dict, list)):
            v = json_or_str(v)
        out[str(k)] = v
    return out


def json_or_str(v: Any) -> str:
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return str(v)


def is_meaningful_triplet(t: dict) -> bool:
    """极宽松的有效性检查：只过滤完全空心的三元组（用户要求保留一切可提取内容）。

    只剔除：头尾实体名为空、或同一实体自指且关系为空的噪音。
    """
    head = str(t.get("head") or "").strip()
    tail = str(t.get("tail") or "").strip()
    rel = str(t.get("relation") or "").strip()
    if not head or not tail:
        return False
    if head == tail and not rel:
        return False
    if _only_punct(head) or _only_punct(tail):
        return False
    return True


def _only_punct(s: str) -> bool:
    return not re.search(r"[\w\u4e00-\u9fff]", s)
