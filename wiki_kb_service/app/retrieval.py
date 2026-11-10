"""零向量检索链路：LLM 查询理解 + 图谱查询 + BM25 全文，多路召回 + RRF 融合 + 带出处问答。

检索做法参照「公司信息问答项目参考 / llm-wiki-分级」的通用实现（刻意不用向量）：
  - 分词：连续汉字段落切「一元 + 二元」，ASCII 词单独成词，不引入 jieba（部署零外部依赖）；
  - BM25：K1=1.5 / B=0.75，标题加权 1.2，标题被整串命中额外 +5；
  - 一跳扩展：前 4 个种子页各带出邻居，邻居分数 = 种子分 * 0.35（参考项目扩展的是
    [[wiki 链接]]；本项目 wiki 页之间没有链接，改用图谱关系做一跳扩展，语义等价更准）；
  - 上下文拼装：[n] 编号 + 与查询词最相关的摘录 + 来源，回答强制标注 [n]，
    没有命中时明确回答“知识库中未找到相关内容”，禁止用通用知识补齐。

本项目的额外优势（图谱路）：
  - 实体名 / 别名 / 属性命中 → 直接召回三元组事实，并做一跳邻居扩展；
  - 事实型问题（谁投资的、有哪些资质、用了什么技术、属于什么领域）由三元组精确作答，
    比纯 BM25 强，因此在 RRF 融合里给图谱路最高权重。
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import config, graph_store, llm_factory, wiki

# ── BM25 / 检索参数（与参考项目对齐）──
BM25_K1 = 1.5
BM25_B = 0.75
TITLE_BOOST = 1.2
TITLE_EXACT_BONUS = 5.0
EXPAND_WEIGHT = 0.35
EXPAND_SEEDS = 4

# ── 多路召回 ──
RRF_K = 60
TOP_PER_ROUTE = 12
# 图谱路权重最高：本项目主打“关系 + 属性”，事实型问题靠三元组作答
ROUTE_WEIGHTS = {"fact": 1.35, "wiki": 1.0, "chunk": 0.6}
MAX_SOURCES = 14
MAX_CONTEXT_CHARS = 9000
EXCERPT_WIDTH = 700
MAX_FACT_LINES = 10   # 单次回答里最多引用多少条三元组事实（其余名额留给 Wiki/原文，给模型叙述依据）
MAX_NONFACT_LINES = 4 # Wiki 页 + 原文片段至少保留的条数

# ── 分词 ──
_CJK_RUN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")
_ASCII_WORD = re.compile(r"[a-z0-9_][a-z0-9_+\-.#]*")

_STOP = {
    # 中文虚词 / 泛问句词
    "的", "了", "和", "是", "在", "我", "有", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有",
    "看", "好", "自己", "这", "那", "它", "他", "她", "们", "与", "及", "或",
    "并", "而", "但", "呢", "吧", "啊", "吗", "把", "被", "让", "从", "对", "为",
    "以", "于", "之", "其", "此", "该", "如", "则", "什么", "怎么", "如何", "哪些",
    "可以", "需要", "进行", "通过", "以及", "对于", "关于", "请问", "介绍", "一下",
    "公司", "该", "它", "有哪", "哪些", "多少",
    # Wiki 页面模板里的结构词：每页都有，作为检索词只会把整库都召回来
    "关系", "属性", "快照", "来源", "摘要", "页面", "实体", "更新时间", "类型",
    # 英文
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "was", "were",
    "be", "been", "for", "on", "at", "by", "with", "as", "it", "its", "this",
    "that", "these", "those", "i", "you", "he", "she", "we", "they", "do", "does",
    "did", "can", "could", "will", "would", "should", "what", "how", "which",
    "who", "when", "where", "why", "if", "then", "than", "so", "but", "not",
}


def tokenize(text: str) -> list[str]:
    """把文本切成检索词：汉字段落一元 + 二元，ASCII 词保留。

    二元组负责精度（“投资”“资质”），一元组负责召回，BM25 的 IDF 会自动压低
    高频字的权重——这是参考项目在不引入 jieba 前提下保证中文召回的做法。
    """
    if not text:
        return []
    text = str(text).lower()
    tokens: list[str] = []

    for word in _ASCII_WORD.findall(text):
        w = word.strip(".-_")
        if len(w) < 2 or w in _STOP or w.isdigit():
            continue
        tokens.append(w)

    for run in _CJK_RUN.findall(text):
        for ch in run:
            if ch not in _STOP:
                tokens.append(ch)
        for i in range(len(run) - 1):
            bg = run[i : i + 2]
            if bg not in _STOP:
                tokens.append(bg)

    return tokens


def _terms(text: str) -> Counter:
    return Counter(tokenize(text))


# ────────────────────────────── BM25 ──────────────────────────────

@dataclass
class Doc:
    key: str            # 唯一标识
    kind: str           # fact / wiki / chunk
    title: str
    text: str           # 参与打分的正文（也是摘录来源）
    meta: dict = field(default_factory=dict)
    tf: Counter = field(default_factory=Counter)
    dl: int = 0
    title_tf: Counter = field(default_factory=Counter)

    def __post_init__(self) -> None:
        if not self.tf:
            self.tf = _terms(self.text)
        if not self.dl:
            self.dl = sum(self.tf.values())
        if not self.title_tf:
            self.title_tf = _terms(self.title)


class BM25Index:
    """单路索引：语料自带的 df / N / avgdl，实现参考项目同款 BM25 打分。"""

    def __init__(self, docs: Sequence[Doc]):
        self.docs: list[Doc] = list(docs)
        self.df: Counter = Counter()
        for d in self.docs:
            for t in d.tf:
                self.df[t] += 1
        self.n_docs = max(len(self.docs), 1)
        self.avgdl = (sum(d.dl for d in self.docs) / self.n_docs) if self.docs else 1.0

    def _score_text(self, tf: Counter, dl: int, qterms: Sequence[str]) -> float:
        if not tf or not qterms:
            return 0.0
        dl = dl or 1
        avgdl = self.avgdl or 1.0
        score = 0.0
        for t in qterms:
            f = tf.get(t)
            if not f:
                continue
            n = self.df.get(t, 0)
            idf = math.log(1 + (self.n_docs - n + 0.5) / (n + 0.5))
            score += idf * (f * (BM25_K1 + 1)) / (f + BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl))
        return score

    def score(self, doc: Doc, qterms: Sequence[str], needle: str = "") -> float:
        s = self._score_text(doc.tf, doc.dl, qterms)
        if s <= 0:
            return 0.0
        s += TITLE_BOOST * self._score_text(doc.title_tf, doc.dl, qterms)
        # 参考项目的“整串命中标题”加成：问句里直接出现页面标题时大幅提升
        if needle and doc.title and needle in doc.title.lower():
            s += TITLE_EXACT_BONUS
        return s

    def search(
        self, qterms: Sequence[str], limit: int = TOP_PER_ROUTE, needle: str = ""
    ) -> list[tuple[float, Doc]]:
        if not qterms:
            return []
        scored = [(self.score(d, qterms, needle), d) for d in self.docs]
        scored = [(s, d) for s, d in scored if s > 0]
        scored.sort(key=lambda x: (-x[0], x[1].key))
        return scored[:limit]


# ────────────────────────────── 语料构建 ──────────────────────────────

def _company(company_slug: str):
    comp = graph_store.get_company_by_slug(company_slug)
    if not comp:
        raise KeyError("company not found")
    return comp


def _wiki_docs(company_slug: str) -> list[Doc]:
    docs: list[Doc] = []
    for p in wiki.list_pages(company_slug):
        content = wiki.read_page(company_slug, p["file"].replace(".md", "")) or ""
        if not content.strip():
            continue
        docs.append(
            Doc(
                key=p["file"],
                kind="wiki",
                title=p["title"],
                text=content,
                meta={"page": p["file"], "title": p["title"]},
            )
        )
    return docs


def _chunk_docs(company_id: int) -> list[Doc]:
    docs: list[Doc] = []
    for row in graph_store.chunks_with_text(company_id):
        text = str(row.get("text") or "")
        if not text.strip():
            continue
        title = row.get("source_title") or row.get("source_url") or f"文本块 {row['chunk_id']}"
        docs.append(
            Doc(
                key=f"chunk:{row['chunk_id']}",
                kind="chunk",
                title=str(title),
                text=text,
                meta={
                    "chunk_id": row["chunk_id"],
                    "url": row.get("source_url") or "",
                    "title": str(title),
                    "snapshot_id": row.get("snapshot_id") or "",
                },
            )
        )
    return docs


def _fact_docs(company_id: int, snapshot: str = "all") -> tuple[list[Doc], dict[str, list[Doc]], dict[str, dict]]:
    """三元组事实 → 文档，同时给出「实体 → 事实」邻接表与实体索引。"""
    entities = {e["ekey"]: e for e in graph_store.entities_for_company(company_id)}
    triplets = graph_store.select_triplets(company_id, snapshot)
    docs: list[Doc] = []
    adj: dict[str, list[Doc]] = defaultdict(list)
    for t in triplets:
        src = t.get("src_name") or t.get("src_ekey") or ""
        dst = t.get("dst_name") or t.get("dst_ekey") or ""
        rel = t.get("relation") or ""
        if not (src and dst and rel):
            continue
        try:
            props = json.loads(t.get("props_json") or "{}")
        except Exception:
            props = {}
        props_txt = "；".join(f"{k}={v}" for k, v in props.items() if v not in (None, "", [])) if props else ""
        text = f"{src} —{rel}→ {dst}" + (f"（{props_txt}）" if props_txt else "")
        # 事实文档额外带上两端实体的类型/属性，便于 BM25 命中“国有控股”“资质”等词
        enrich = []
        for ek in (t.get("src_ekey"), t.get("dst_ekey")):
            e = entities.get(ek or "")
            if not e:
                continue
            if _etype(e):
                enrich.append(_etype(e))
            try:
                ep = json.loads(_efield(e, "props_json") or "{}")
            except Exception:
                ep = {}
            enrich.extend(str(v) for v in ep.values() if isinstance(v, (str, int, float)) and str(v))
        doc_text = text + ("\n" + " ".join(enrich) if enrich else "")
        doc = Doc(
            key=f"fact:{t.get('id')}",
            kind="fact",
            title=f"{src} {rel} {dst}",
            text=doc_text,
            meta={
                "src": src,
                "src_ekey": t.get("src_ekey"),
                "relation": rel,
                "dst": dst,
                "dst_ekey": t.get("dst_ekey"),
                "fact": text,
                "url": t.get("source_url") or "",
                "title": t.get("source_title") or "",
                "snapshot_id": t.get("snapshot_id") or "",
                "props": props,
            },
        )
        docs.append(doc)
        if t.get("src_ekey"):
            adj[str(t["src_ekey"])].append(doc)
        if t.get("dst_ekey"):
            adj[str(t["dst_ekey"])].append(doc)
    return docs, adj, entities


# ────────────────────────────── 索引缓存 ──────────────────────────────

@dataclass
class CompanyIndex:
    company_id: int
    company_name: str
    signature: tuple
    wiki_index: BM25Index
    chunk_index: BM25Index
    fact_index: BM25Index
    fact_adj: dict[str, list[Doc]]
    entities: dict[str, dict]
    ent_names: list[tuple[str, str]]      # (lower_name, ekey)
    ent_aliases: list[tuple[str, str]]    # (lower_alias, ekey)


_INDEX_CACHE: dict[str, CompanyIndex] = {}


def _signature(company_slug: str, company_id: int) -> tuple:
    pages = wiki.list_pages(company_slug)
    pm = max((p.get("mtime") or "" for p in pages), default="")
    try:
        db_m = config.GRAPH_DB_PATH.stat().st_mtime
    except OSError:
        db_m = 0.0
    return (len(pages), pm, round(float(db_m), 3), int(company_id))


def get_index(company_slug: str, force: bool = False) -> CompanyIndex:
    comp = _company(company_slug)
    company_id = int(comp["id"])
    sig = _signature(company_slug, company_id)
    cached = _INDEX_CACHE.get(company_slug)
    if cached and not force and cached.signature == sig:
        return cached

    wiki_docs = _wiki_docs(company_slug)
    chunk_docs = _chunk_docs(company_id)
    fact_docs, adj, entities = _fact_docs(company_id, "all")

    ent_names: list[tuple[str, str]] = []
    ent_aliases: list[tuple[str, str]] = []
    for ek, e in entities.items():
        name = str(e["name"] or "").strip().lower()
        if name:
            ent_names.append((name, ek))
        try:
            props = json.loads(e["props_json"] or "{}")
        except Exception:
            props = {}
        for key in ("别名", "alias", "aliases", "别称", "英文名", "简称"):
            v = props.get(key)
            vals = v if isinstance(v, list) else [v]
            for item in vals:
                if isinstance(item, str) and item.strip():
                    ent_aliases.append((item.strip().lower(), ek))
    ent_names.sort(key=lambda x: -len(x[0]))
    ent_aliases.sort(key=lambda x: -len(x[0]))

    idx = CompanyIndex(
        company_id=company_id,
        company_name=str(comp["name"] or company_slug),
        signature=sig,
        wiki_index=BM25Index(wiki_docs),
        chunk_index=BM25Index(chunk_docs),
        fact_index=BM25Index(fact_docs),
        fact_adj=adj,
        entities=entities,
        ent_names=ent_names,
        ent_aliases=ent_aliases,
    )
    _INDEX_CACHE[company_slug] = idx
    return idx


def clear_index_cache(company_slug: str | None = None) -> None:
    if company_slug:
        _INDEX_CACHE.pop(company_slug, None)
    else:
        _INDEX_CACHE.clear()


# ────────────────────────────── 查询理解 ──────────────────────────────

# 类型同义词组：LLM 自拟的类型很散（理念/口号/愿景…），问句里的说法和存储的类型
# 常常对不上，这里做一层“分类归一”，保证分类检索能命中。
_TYPE_SYNONYMS = [
    {"口号", "愿景", "理念", "价值观", "使命", "标语", "slogan", "vision"},
    {"资质", "奖项", "获奖", "荣誉", "认证", "证书", "qualification", "award"},
    {"产品", "平台", "系统", "应用", "解决方案", "product", "platform"},
    {"技术", "算法", "模型", "technology", "tech"},
    {"领域", "行业", "业务", "方向", "赛道"},
    {"合作伙伴", "客户", "合作方", "渠道", "partner", "customer"},
    {"地点", "位于", "地址", "地区", "城市", "location"},
    {"日期", "时间", "成立于", "上线", "date"},
    {"人物", "创始人", "高管", "团队", "person"},
]


def _synonym_group(term: str) -> set[str]:
    t = str(term or "").strip().lower()
    if not t:
        return set()
    for g in _TYPE_SYNONYMS:
        if t in g:
            return g
    return set()


_UNDERSTAND_PROMPT = """你是一个知识库检索助手。知识库是一个「公司信息 Wiki + 知识图谱」，检索方式是关键词（BM25）+ 图谱实体匹配，没有向量检索。

请把用户的提问改写成更容易被关键词命中的形式，并抽取检索要素。只输出一个 JSON 对象，不要任何解释：
{{"关键词": ["..."], "实体": ["..."], "关系": ["..."], "改写问题": "..."}}

要求：
1. 「关键词」3–8 个，尽量使用公司网站/知识库中可能出现的正式名词（公司名、产品名、技术名、领域、资质名、口号），不要输出“什么”“如何”这类虚词。
2. 「实体」是问题里指向的具体对象名（公司、机构、产品、技术、人物、地区）；问题里没出现具体名字就留空数组。
3. 「关系」是问题想查的关系类型，用中文，例如：投资方、产品、资质、获奖、所属领域、采用技术、公司性质、口号、愿景、合作伙伴、位于、成立于。没有就留空数组。
4. 「改写问题」把口语化提问改写成陈述式关键词串（不超过 60 字），保留原意，补上同义词或正式说法。
5. 使用与提问相同的语言。

用户提问：{question}
"""


def understand_query(question: str, company_name: str = "", use_llm: bool = True) -> dict:
    """LLM 查询理解；任何失败都回退成「原始问句 + 分词」，保证检索不会因为模型不可用而中断。"""
    q = str(question or "").strip()
    fallback = {
        "keywords": _dedup(tokenize(q))[:10],
        "entities": [],
        "relations": [],
        "rewritten": q,
        "llm": False,
    }
    if not q or not use_llm:
        return fallback
    try:
        llm = llm_factory.get_llm()
        raw = llm.complete(_UNDERSTAND_PROMPT.format(question=q)).text
    except Exception:
        return fallback
    data = _extract_json(raw)
    if not isinstance(data, dict):
        # 模型没按 JSON 输出时，把整段输出当作改写问句用（仍然优于原句的关键词覆盖）
        txt = str(raw or "").strip()
        if txt and len(txt) <= 120:
            return {**fallback, "rewritten": txt, "llm": False}
        return fallback

    def _as_list(v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [s.strip() for s in re.split(r"[，,、;；/\s]+", v) if s.strip()]
        return []

    out = {
        "keywords": _as_list(data.get("关键词") or data.get("keywords")),
        "entities": _as_list(data.get("实体") or data.get("entities")),
        "relations": _as_list(data.get("关系") or data.get("relations")),
        "rewritten": str(data.get("改写问题") or data.get("rewritten") or q).strip() or q,
        "llm": True,
    }
    if not out["keywords"]:
        out["keywords"] = _dedup(tokenize(out["rewritten"] + " " + q))[:10]
    # 注意：不要把当前公司名塞进「实体」——它会让“问句里是否提到具体实体”这一判断失真，
    # 图谱路就分不清该走“精确实体召回”还是“公司兜底召回”。公司名单独记录。
    out["company"] = company_name
    return out


def _extract_json(text: str) -> Any:
    s = str(text or "").strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _dedup(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        k = str(it).strip()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


# ────────────────────────────── 三路召回 ──────────────────────────────

@dataclass
class Hit:
    doc: Doc
    ranks: dict = field(default_factory=dict)   # route -> rank(1-based)
    score: float = 0.0                          # RRF 融合分

    @property
    def kind(self) -> str:
        return self.doc.kind

    @property
    def key(self) -> str:
        return self.doc.key


def _match_entities(idx: CompanyIndex, question: str, understood: dict) -> list[tuple[float, str]]:
    """图谱路的“实体链接”：问题里的名字/别名 → 图谱实体。

    打分：别名精确命中 3.0 > 全名出现在问句里 2.5 > 问句片段等于实体名 2.0 > 名字片段互相包含 1.2。
    """
    q = str(question or "").lower()
    q_terms = set(understood.get("keywords") or []) | set(tokenize(question))
    hits: dict[str, float] = {}

    def bump(ek: str, s: float) -> None:
        if s > hits.get(ek, 0.0):
            hits[ek] = s

    for name, ek in idx.ent_aliases:
        if len(name) >= 2 and name in q:
            bump(ek, 3.0)
    for name, ek in idx.ent_names:
        if len(name) < 2:
            continue
        if name in q:
            bump(ek, 2.5 + min(len(name), 12) * 0.05)
        elif name in q_terms:
            bump(ek, 2.0)
    # LLM 识别出的实体名做兜底（允许简称 / 部分匹配）
    for hint in understood.get("entities") or []:
        h = str(hint).strip().lower()
        if len(h) < 2:
            continue
        for name, ek in idx.ent_names:
            if len(name) < 2:
                continue
            if name == h:
                bump(ek, 2.6)
            elif (name in h or h in name) and min(len(name), len(h)) >= 3:
                bump(ek, 1.6)
    return sorted(((s, ek) for ek, s in hits.items()), key=lambda x: (-x[0], x[1]))


def graph_route(idx: CompanyIndex, question: str, understood: dict) -> list[tuple[float, Doc]]:
    """图谱路：实体命中 → 直接事实（权重高）+ 一跳邻居事实（衰减）+ 关系词命中加成。

    关系词很关键：“新华智云的投资方是谁”里没有额外的实体，靠 relation=投资方 命中；
    “有哪些资质”靠 relation=资质 命中——这正是本项目图谱相比纯 BM25 的优势。
    """
    seeds = _match_entities(idx, question, understood)
    fallback_seed = not seeds

    # 「协会的电话」「公司的口号」这类问句里的“公司/协会/机构”是泛指词，
    # 图谱里可能正好有个叫「协会」的节点——必须重定向到知识库主体实体。
    subject = _company_ekey(idx)
    if subject:
        redirect: dict[str, str] = {}
        for s, ek in seeds:
            nm = str(_efield(idx.entities.get(ek), "name") or "").strip()
            if nm in _GENERIC_SELF:
                redirect[ek] = subject
        if redirect:
            merged: dict[str, float] = {}
            for s, ek in ((s, redirect.get(k, k)) for s, k in seeds):
                merged[ek] = max(merged.get(ek, 0.0), s)
            seeds = [(s, ek) for ek, s in sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))]

    if fallback_seed:
        # 问句里没出现任何实体名（“公司的口号是什么”）时，用公司实体兜底：
        # 公司知识库里的属性型问题本身就是围绕这家公司问的。
        ek = _company_ekey(idx)
        if not ek:
            return []
        seeds = [(2.0, ek)]

    # 去掉公司自身名称后再分词：否则“公司/新华智云”这类词会命中每一条事实，
    # 让兜底种子把公司全部事实都倒出来（噪声压过真正相关的原文）。
    q_clean = str(question or "").lower().replace(str(idx.company_name or "").lower(), " ")
    ce = _company_ekey(idx)
    if ce:
        ce_row = idx.entities.get(ce)
        ce_name = str(_efield(ce_row, "name") or "").lower()
        if ce_name:
            q_clean = q_clean.replace(ce_name, " ")
    q_terms = {t.lower() for t in tokenize(q_clean)}
    q_terms |= {
        str(k).lower().strip()
        for k in (understood.get("keywords") or [])
        if str(k).strip() and str(k).strip().lower() not in (str(idx.company_name or "").lower(),)
    }
    rel_terms = {str(r).lower().strip() for r in (understood.get("relations") or []) if str(r).strip()}
    rel_terms |= {t for t in q_terms if len(t) >= 2}

    def rel_bonus(doc: Doc) -> float:
        rel = str(doc.meta.get("relation") or "").lower().strip()
        if not rel:
            return 0.0
        best = 0.0
        for t in rel_terms:
            if t == rel:
                best = max(best, 2.4)
            elif len(t) >= 2 and (t in rel or rel in t):
                best = max(best, 1.5)
        return best

    def text_bonus(doc: Doc) -> float:
        """事实文本与问句词的覆盖度：同为“新华智云的事实”，越贴题排越前。"""
        low = str(doc.text or "").lower()
        hit = sum(1 for t in rel_terms if len(t) >= 2 and t in low)
        return 0.12 * min(6, hit)

    scores: dict[str, float] = {}
    order: dict[str, Doc] = {}

    for s, ek in seeds[:6]:
        for doc in idx.fact_adj.get(ek, []):
            rb, tb = rel_bonus(doc), text_bonus(doc)
            # 兜底种子（问句里没有具体实体名）不能把公司全部事实都倒出来，
            # 只保留与问句词沾边的事实，否则会把真正相关的原文片段挤下去。
            if fallback_seed and rb <= 0 and tb <= 0:
                continue
            score = s + rb + tb
            if score > scores.get(doc.key, 0.0):
                scores[doc.key] = score
                order[doc.key] = doc

    # 一跳扩展：邻居实体的事实只保留一部分（对应参考项目的 EXPAND_WEIGHT）
    for s, ek in seeds[:EXPAND_SEEDS]:
        for doc in idx.fact_adj.get(ek, []):
            for nb in (doc.meta.get("src_ekey"), doc.meta.get("dst_ekey")):
                if not nb or nb == ek:
                    continue
                for doc2 in idx.fact_adj.get(nb, []):
                    rb2, tb2 = rel_bonus(doc2), text_bonus(doc2)
                    if fallback_seed and rb2 <= 0 and tb2 <= 0:
                        continue
                    score = s * EXPAND_WEIGHT + rb2 * 0.5 + tb2 * 0.5
                    if score > scores.get(doc2.key, 0.0):
                        scores[doc2.key] = score
                        order[doc2.key] = doc2

    out = [(s, order[k]) for k, s in scores.items()]
    out.sort(key=lambda x: (-x[0], x[1].key))
    return out[:TOP_PER_ROUTE]


def _word_terms(qterms: Sequence[str]) -> set[str]:
    """有效检索词：只要长度 ≥2 的中文二元组 / ASCII 词。

    单字（“方”“是”“谁”）几乎每个页面都能命中，是纯噪声源——只靠单字命中的文档
    必须淘汰，否则问“投资方是谁”会把《试验方案》顶到第一位。
    """
    return {str(t).lower() for t in qterms if len(str(t)) >= 2}


def _has_word(doc: Doc, words: set[str]) -> bool:
    if not words:
        return False
    hay = f"{doc.title}\n{doc.text}".lower()
    return any(w in hay for w in words)


def wiki_route(idx: CompanyIndex, qterms: Sequence[str], question: str = "") -> list[tuple[float, Doc]]:
    """Wiki 路：BM25 + 类型召回 + 一跳扩展。

    参考项目用 [[wiki 链接]] / backlinks 做扩展；本项目页面之间没有链接，
    用图谱关系把命中的实体页连到它的邻居实体页，效果等价（邻居分数乘 0.35）。
    """
    qset = _word_terms(qterms)
    docs = {d.key: d for d in idx.wiki_index.docs}
    picked: dict[str, float] = {}

    base = idx.wiki_index.search(qterms, limit=TOP_PER_ROUTE * 2, needle=(question or "").strip().lower())
    for s, d in base:
        if s <= 0:
            continue
        # 实体类型命中（问“有哪些资质/口号/愿景”时，对应类型的页面优先）
        tb = _type_bonus(idx, d, qset)
        if tb <= 0 and not _has_word(d, qset):
            continue  # 只靠单字/模板词命中 → 噪声，丢掉
        picked[d.key] = s + tb

    # 分类召回：问句里出现类型词（口号/资质/领域…）时，即使页面正文没写这些字，
    # 也把该类型的页面捞回来——LLM 自拟类型很散，靠同义词组对齐。
    if qset:
        for d in idx.wiki_index.docs:
            if d.key in picked:
                continue
            tb = _type_bonus(idx, d, qset, amount=3.0)
            if tb > 0:
                picked[d.key] = tb

    if not picked:
        return []

    by_title = {d.title.lower(): d for d in idx.wiki_index.docs}
    seeds = sorted(picked.items(), key=lambda kv: -kv[1])[:EXPAND_SEEDS]
    for s, key in seeds:
        d = docs.get(key)
        ek = _ekey_of_page(idx, d) if d else None
        if not ek:
            continue
        for doc in idx.fact_adj.get(ek, []):
            for side in ("src", "dst"):
                name = str(doc.meta.get(side) or "").lower()
                nb = by_title.get(name)
                if not nb or nb.key in picked:
                    continue
                nb_s = s * EXPAND_WEIGHT + idx.wiki_index.score(nb, qterms) * 0.1
                picked[nb.key] = nb_s

    out = [(s, docs[k]) for k, s in picked.items() if k in docs]
    out.sort(key=lambda x: (-x[0], x[1].key))
    return out[:TOP_PER_ROUTE]


def _type_bonus(idx: CompanyIndex, d: Doc, qset: set[str], amount: float = 6.0) -> float:
    ek = _ekey_of_page(idx, d)
    e = idx.entities.get(ek) if ek else None
    if not e:
        return 0.0
    try:
        types = {_etype(e), *json.loads(_efield(e, "types_json") or "[]")}
    except Exception:
        types = {_etype(e)}
    for t in types:
        tl = (t or "").lower()
        if not tl:
            continue
        if tl in qset:
            return amount
        group = _synonym_group(tl)
        if group and (group & qset):
            return amount * 0.8
    return 0.0


def _efield(entity, key: str, default=""):
    """实体行取值：兼容 sqlite3.Row（无 .get）与 dict。"""
    if entity is None:
        return default
    try:
        v = entity[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if v is None else v


def _etype(entity) -> str:
    return str(_efield(entity, "type") or "").strip()


# “知识库主体”可能被抽成公司，也可能被抽成协会/机构/政府机关（按站点不同），
# 兜底选种子时都算候选，避免只认「公司」导致选错实体。
_SUBJECT_TYPES = {
    "公司", "企业", "组织", "机构", "单位", "协会", "行业协会", "事业单位",
    "company", "organization", "organisation", "enterprise",
}

# 问句里的泛指词：不该被当作一个具体实体，而应指向知识库主体
_GENERIC_SELF = {"公司", "企业", "协会", "机构", "单位", "集团", "本公司", "贵公司", "咱们公司"}


def _company_ekey(idx: CompanyIndex) -> str | None:
    """把「新华智云公司」这种记录名对齐到图谱实体「新华智云」（取名称互为前缀且度数最高者）。"""
    name = str(idx.company_name or "").strip().lower()
    if name:
        best: tuple[int, str] | None = None
        for nm, ek in idx.ent_names:
            if nm == name or name.startswith(nm) or nm.startswith(name):
                deg = len(idx.fact_adj.get(ek, []))
                if best is None or deg > best[0]:
                    best = (deg, ek)
        if best:
            return best[1]
    # 退而求其次：先看“主体类”实体里度数最高的，再退回全图度数最高的实体
    # （站点名当公司名时 name 与任何实体名都不匹配，只能用中心节点兜底）。
    for pred in (
        lambda t: t.lower() in _SUBJECT_TYPES,
        lambda t: True,
    ):
        cands = [
            (len(idx.fact_adj.get(ek, [])), ek)
            for ek, e in idx.entities.items()
            if pred(_etype(e))
        ]
        cands = [c for c in cands if c[0] > 0]
        if cands:
            return max(cands)[1]
    return None


def _ekey_of_page(idx: CompanyIndex, d: Doc) -> str | None:
    """Wiki 页标题 → 图谱实体 ekey（页面标题就是实体名）。"""
    name = str(d.meta.get("title") or d.title or "").strip().lower()
    for nm, ek in idx.ent_names:
        if nm == name:
            return ek
    return None


def chunk_route(idx: CompanyIndex, qterms: Sequence[str]) -> list[tuple[float, Doc]]:
    """原文路：BM25 只保留真正含检索词（≥2 字）的片段，避免模板词噪声。"""
    qset = _word_terms(qterms)
    out = [
        (s, d)
        for s, d in idx.chunk_index.search(qterms, limit=TOP_PER_ROUTE * 2)
        if s > 0 and _has_word(d, qset)
    ]
    return out[:TOP_PER_ROUTE]


def fuse(routes: dict[str, list[tuple[float, Doc]]]) -> list[Hit]:
    """RRF 融合：分数不可比的各路只比较名次，score = Σ weight / (K + rank)。"""
    hits: dict[str, Hit] = {}
    for route, items in routes.items():
        w = ROUTE_WEIGHTS.get(route, 1.0)
        for rank, (_s, doc) in enumerate(items, start=1):
            h = hits.get(doc.key)
            if not h:
                h = Hit(doc=doc)
                hits[doc.key] = h
            h.ranks[route] = rank
            h.score += w / (RRF_K + rank)
    out = sorted(hits.values(), key=lambda h: -h.score)
    return out


def retrieve(company_slug: str, question: str, understood: dict | None = None) -> dict:
    """执行三路召回并融合。返回 {hits, routes, index_meta}。"""
    idx = get_index(company_slug)
    u = understood or understand_query(question, idx.company_name, use_llm=False)
    # 检索词 = 改写问句 + 关键词 + 原始问句，保证召回面（关键词由 LLM 补过同义词）
    query_text = " ".join(
        _dedup([u.get("rewritten") or "", " ".join(u.get("keywords") or []), question])
    )
    qterms = _dedup(tokenize(query_text))
    routes = {
        "fact": graph_route(idx, question, u),
        "wiki": wiki_route(idx, qterms, question),
        "chunk": chunk_route(idx, qterms),
    }
    hits = fuse(routes)
    return {
        "hits": hits,
        "routes": {k: len(v) for k, v in routes.items()},
        "query_terms": qterms,
    }


# ────────────────────────────── 上下文拼装 ──────────────────────────────

def _excerpt(text: str, qterms: Sequence[str], width: int = EXCERPT_WIDTH) -> str:
    """截取与查询最相关的一段：按空行切段 → 取命中词最多的一段 → 向后累积到 width。"""
    text = str(text or "").strip()
    if not text:
        return ""
    if len(text) <= width:
        return text
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()] or [text]

    def hit_score(p: str) -> float:
        low = p.lower()
        return float(len({t for t in qterms if len(t) >= 2 and t in low}))

    best_i, best_s = 0, -1.0
    for i, p in enumerate(paras):
        s = hit_score(p)
        if s > best_s:
            best_i, best_s = i, s
    picked: list[str] = []
    total = 0
    for p in paras[best_i:]:
        if total and total + len(p) > width:
            break
        picked.append(p)
        total += len(p)
    if total < 60 and best_i > 0:
        prev = paras[best_i - 1]
        picked.insert(0, prev)
        total += len(prev)
    out = "\n\n".join(picked)
    if len(out) > width:
        pos = min((out.find(t) for t in qterms if t and out.find(t) >= 0), default=-1)
        start = max(0, (pos - width // 3) if pos > 0 else 0)
        out = out[start : start + width]
    if len(out) < len(text):
        out = "…" + out.lstrip() if out else out
        if not out.endswith(("。", ".", "！", "!", "？", "?", "）", ")")):
            out = out.rstrip() + "…"
    return out


def build_context(hits: Sequence[Hit], qterms: Sequence[str]) -> tuple[str, list[dict]]:
    """把融合后的命中拼成 [n] 编号上下文，并返回前端可点击的来源列表。"""
    blocks: list[str] = []
    sources: list[dict] = []
    used = 0
    n = 0
    fact_count = 0

    nonfact_count = 0
    for h in hits:
        if len(sources) >= MAX_SOURCES:
            break
        d = h.doc
        if d.kind == "fact":
            if fact_count >= MAX_FACT_LINES:
                continue
            fact_count += 1
            body = str(d.meta.get("fact") or d.text)
            src_title = str(d.meta.get("title") or "")
            src_url = str(d.meta.get("url") or "")
            head = f"[{n + 1}] 图谱事实：{body}"
            if src_title:
                head += f"（来源：{src_title}）"
            if src_url:
                head += f" {src_url}"
            block = head
            if used + len(block) > MAX_CONTEXT_CHARS:
                break
            used += len(block)
            blocks.append(block)
            n += 1
            sources.append(
                {
                    "index": n,
                    "kind": "fact",
                    "title": f"{d.meta.get('src')} —{d.meta.get('relation')}→ {d.meta.get('dst')}",
                    "snippet": body,
                    "url": src_url,
                    "source_title": src_title,
                    "entity_id": d.meta.get("src_ekey"),
                    "other_id": d.meta.get("dst_ekey"),
                    "page": "",
                    "score": round(h.score, 5),
                    "routes": list(h.ranks.keys()),
                }
            )
            continue

        if nonfact_count >= MAX_NONFACT_LINES:
            continue
        nonfact_count += 1

        excerpt = _excerpt(d.text, qterms)
        if not excerpt.strip():
            continue
        if d.kind == "wiki":
            head = f"[{n + 1}] Wiki 页面《{d.meta.get('title') or d.title}》（页面：{d.meta.get('page')}）"
            kind = "wiki"
        else:
            head = f"[{n + 1}] 原文片段《{d.meta.get('title')}》"
            if d.meta.get("url"):
                head += f" {d.meta['url']}"
            kind = "chunk"
        block = f"{head}\n{excerpt}"
        if used + len(block) > MAX_CONTEXT_CHARS:
            if used == 0:
                block = block[:MAX_CONTEXT_CHARS]
            else:
                break
        used += len(block)
        blocks.append(block)
        n += 1
        sources.append(
            {
                "index": n,
                "kind": kind,
                "title": d.meta.get("title") or d.title,
                "snippet": excerpt,
                "url": d.meta.get("url") or "",
                "source_title": d.meta.get("title") or d.title,
                "entity_id": None,
                "other_id": None,
                "page": d.meta.get("page") or "",
                "score": round(h.score, 5),
                "routes": list(h.ranks.keys()),
            }
        )

    return "\n\n---\n\n".join(blocks), sources


# ────────────────────────────── 问答 ──────────────────────────────

ASK_PROMPT = """你是一个严谨的公司知识库问答助手。知识库由「公司知识图谱（三元组事实）」和「Wiki 页面 / 网页原文片段」组成，下面给出的就是检索到的全部资料。

要求：
1. 只使用下面资料中的信息作答，**优先使用图谱事实**（它是从官网结构化抽取出来的，最准确）。资料里没有的内容，直接回答“知识库中未找到相关内容”，不要用通用常识补齐，不要猜测。
2. 每个结论句末尾用方括号标注来源编号，例如 [1] 或 [2][3]；编号必须与资料编号严格对应，不要引用不存在的编号。
3. 涉及关系、属性（投资方、产品、资质、获奖、所属领域、采用技术、公司性质、口号、愿景、价值观、合作伙伴、所在地、成立时间等）时，用“主体—关系—客体”的方式把事实说清楚，不要模糊概括。
4. 如果有多个来源支持同一结论，可以同时标注多个编号；来源之间冲突时如实指出。
5. 用与提问相同的语言回答，条理清晰，可用要点列表，不要输出“根据资料”之类的套话堆砌。
6. 你对知识库是只读的：不要声称自己新建、修改或删除了任何内容。

资料：
{context}

用户问题：{question}

回答（记得在句末标注 [编号]）："""


def ask(
    company_slug: str,
    question: str,
    use_llm: bool = True,
    snapshot: str = "all",
) -> dict:
    """零向量问答：查询理解 → 三路召回 → 上下文 → LLM 生成（带出处）。"""
    t0 = time.time()
    q = str(question or "").strip()
    if not q:
        raise ValueError("问题不能为空")

    idx = get_index(company_slug)
    if not idx.fact_index.docs and not idx.wiki_index.docs and not idx.chunk_index.docs:
        return {
            "answer": "这个公司还没有任何可检索的内容。请先在爬虫端爬取并完成抽取。",
            "sources": [],
            "rewritten": q,
            "understanding": {},
            "stats": {"routes": {}, "sources": 0, "elapsed": 0.0},
        }

    understood = understand_query(q, idx.company_name, use_llm=use_llm)
    retrieved = retrieve(company_slug, q, understood)
    hits = retrieved["hits"]
    context, sources = build_context(hits, retrieved["query_terms"])

    stats = {
        "routes": retrieved["routes"],
        "sources": len(sources),
        "elapsed": round(time.time() - t0, 2),
        "index": {
            "facts": len(idx.fact_index.docs),
            "wiki_pages": len(idx.wiki_index.docs),
            "chunks": len(idx.chunk_index.docs),
        },
    }
    base = {
        "sources": sources,
        "rewritten": understood.get("rewritten") or q,
        "understanding": understood,
        "stats": stats,
    }

    if not context:
        return {
            **base,
            "answer": f"知识库中没有检索到与「{q}」相关的内容。可以换个说法再问，或到「Wiki 页面」里用关键词确认一下。",
        }

    if not use_llm:
        # 无模型时给出可读的“检索结果式回答”，仍然带出处
        lines = [f"（未调用模型，以下为检索到的最相关资料，共 {len(sources)} 条）", ""]
        for s in sources:
            lines.append(f"[{s['index']}] {s['snippet'][:200]}")
        return {**base, "answer": "\n\n".join(lines)}

    try:
        llm = llm_factory.get_llm()
        answer = llm.complete(ASK_PROMPT.format(context=context, question=q)).text
    except Exception as e:  # noqa: BLE001
        return {
            **base,
            "answer": "",
            "error": f"调用模型失败（{type(e).__name__}: {e}）。请确认 Ollama 已启动并已拉取模型；检索结果仍可在下方来源中查看。",
        }

    stats["elapsed"] = round(time.time() - t0, 2)
    return {**base, "answer": _append_references(str(answer or "").strip(), sources)}


def _append_references(answer: str, sources: Sequence[dict]) -> str:
    """把被引用到的来源附在回答末尾，保证前端一定能看到出处。"""
    used: list[int] = []
    for m in re.finditer(r"\[(\d{1,3})\]", answer or ""):
        i = int(m.group(1))
        if i not in used:
            used.append(i)
    if not used:
        used = [s["index"] for s in sources[:5]]
    lines = ["", "---", "**出处**"]
    for i in used:
        s = next((x for x in sources if x["index"] == i), None)
        if not s:
            continue
        label = {
            "fact": "图谱",
            "wiki": "Wiki",
            "chunk": "原文",
        }.get(s["kind"], "资料")
        tail = s["url"] or s.get("page") or ""
        lines.append(f"- [{i}]（{label}）{s['title']}" + (f" — {tail}" if tail else ""))
    if not answer:
        return "\n".join(lines).strip()
    return answer + ("\n".join(lines) if used else "")
