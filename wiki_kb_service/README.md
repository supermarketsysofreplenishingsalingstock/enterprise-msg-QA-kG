# 公司信息知识库服务（LLM Wiki + DynamicLLMPathExtractor）

爬虫回传的页面与文件 → LLM Wiki 知识库 + 开放式知识图谱，一体化 ingest。

## 功能

- **LLM Wiki 三层结构**：`raw/`（原始资料只读存档）→ `pages/`（实体页，含 LLM 摘要 + 属性表 + 关系表）→ `index.md`（内容目录）+ `log.md`（时间线日志）
- **开放式图谱抽取**：`DynamicLLMPathExtractor`（非严格版）
  - 不预设实体/关系 schema（公司网站上什么都可能爬到）
  - **属性必须提取**（`allowed_entity_props=[]` 开放生成任意属性键）
  - **自定义宽松 parse_fn：不丢弃任何可解析三元组**（缺类型补"其他"、缺关系补"关联"）
- **快照版本化**：同一公司不同时间爬取 → 各自快照；`/api/graph/diff` 分析两次爬取的新增/消失/变化
- **图谱可视化**：ECharts 力导向图（参考项目风格），可选公司/快照/类别/搜索过滤，节点点击看属性与关系详情
- **文件挂载爬取**：爬虫侧自动收集并下载网站挂载的 pdf/docx/xlsx/pptx/zip 等，文本回传后参与 ingest

## 启动

```bash
# 1) 安装依赖（建议 venv）
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2) 配置 .env（见 .env.example）：三选一 LLM
#    KB_LLM_PROVIDER=local_deepseek | local_ministral | online_deepseek

# 3) 启动（默认端口 13105）
python run.py
```

打开 `http://localhost:13105` 即为图谱 / 快照对比 / Wiki 页面三合一界面。

## 环境变量（前缀 KB_）

| 变量 | 默认 | 说明 |
|---|---|---|
| `KB_LLM_PROVIDER` | `local_deepseek` | `local_deepseek` / `local_ministral` / `online_deepseek` |
| `KB_LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI 兼容端点 |
| `KB_LLM_MODEL` / `KB_LLM_API_KEY` | — | 模型名 / 密钥 |
| `KB_LLM_REQUEST_TIMEOUT` | `600` | LLM 请求超时（秒） |
| `KB_PROPS_MODE` | `any` | 属性开放提取（`none` 则不提取属性，不建议） |
| `KB_MAX_TRIPLETS_PER_CHUNK` | `25` | 每 chunk 最大三元组数 |
| `KB_CHUNK_SIZE` / `KB_CHUNK_OVERLAP` | `1200` / `200` | 分块 |
| `KB_WIKI_SUMMARY_ENABLED` / `KB_WIKI_SUMMARY_MAX_ENTITIES` | `1` / `10` | 实体页 LLM 摘要 |
| `KB_PORT` | `13105` | 服务端口 |

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/ingest` | 爬虫回传统一入库（页面 + 文件），`dry_run:true` 只存原文不抽取 |
| GET | `/api/companies` | 已入库公司列表 |
| GET | `/api/snapshots?company=` | 某公司的快照列表 |
| GET | `/api/categories?company=` | 图谱类别（图例） |
| GET | `/api/graph/data?company=&category=&snapshot=&search=&limit=` | 图谱数据 `{nodes, edges, counts}` |
| GET | `/api/graph/entity/{ekey}?company=` | 实体详情（属性 + 关系 + 来源） |
| GET | `/api/graph/diff?company=&a=&b=` | 两个快照的三元组差异（新增/消失/变化） |
| GET | `/api/wiki/pages` / `page` / `index` | Wiki 页面列表 / 内容 / 索引 |
| GET | `/api/health` | 健康检查（含 LLM 探测） |

### /api/ingest 请求体

```json
{
  "company": "杭州未来科技有限公司",
  "crawl_time": "2026-10-13 19:00:00",
  "note": "第2次全站爬取",
  "pages":  [{ "url": "...", "title": "...", "text": "页面正文" }],
  "files":  [{ "url": "...", "title": "...", "path": "已下载文件路径", "content_type": "application/pdf" }]
}
```

`Extractor_Agent`（Nuxt 爬虫）在**公司爬取**结束后自动回调本接口（`KB_AUTO_INGEST=1`，默认开启；服务地址 `KB_SERVICE_URL`，默认 `http://127.0.0.1:13105`）。

## 测试

```bash
# 端到端 mock 测试（伪造 LLM，验证 抽取→入库→图谱→diff→Wiki 全链路，不依赖真实 LLM）
python test_e2e_mock.py
```

## 数据落盘

```
data/
  kb.sqlite                # 图谱（实体/三元组/快照/chunk/来源）
  wiki/<公司slug>/
    raw/<快照>/NNN_标题.md  # 原始资料层（只读存档）
    pages/公司名.md         # 实体页（LLM 摘要 + 属性表 + 关系表 + 来源）
    index.md               # 内容目录
    log.md                 # 时间线日志
```
