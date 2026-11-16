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
| `KB_LOCAL_DEEPSEEK_BASE_URL` / `_MODEL` / `_API_KEY` | `http://localhost:11434/v1` / `deepseek-r1:14b` / `ollama` | 本地 DeepSeek（OpenAI 兼容端点） |
| `KB_LOCAL_MINISTRAL_BASE_URL` / `_MODEL` / `_API_KEY` | `http://localhost:11434/v1` / `ministral-8b` / `ollama` | 本地 Ministral |
| `KB_ONLINE_DEEPSEEK_BASE_URL` / `_MODEL` / `_API_KEY` | `https://api.deepseek.com/v1` / `deepseek-chat` / — | 线上 DeepSeek |
| `KB_LLM_REQUEST_TIMEOUT` | `600` | **单次 LLM 请求超时（秒）**——改它即改抽取运行时间上限 |
| `KB_LLM_MAX_RETRIES` | `2` | 失败重试次数（`0`=不重试），与超时相乘决定单 chunk 最坏耗时 |
| `KB_PROPS_MODE` | `any` | 属性开放提取（`none` 则不提取属性，不建议） |
| `KB_MAX_TRIPLETS_PER_CHUNK` | `90` | 每 chunk 最大三元组数 |
| `KB_CHUNK_SIZE` / `KB_CHUNK_OVERLAP` | `1200` / `150` | 分块 |
| `KB_NUM_WORKERS` | `4` | 并行抽取 worker 数（本地单卡跑大模型建议 1–2） |
| `KB_WIKI_SUMMARY_ENABLED` / `KB_WIKI_SUMMARY_MAX_ENTITIES` | `1` / `10` | 实体页 LLM 摘要 |
| `KB_PORT` | `13105` | 服务端口 |

**抽取时间上限的算法**：一个 chunk 要跑 2 次 LLM 调用（关系轮 + 属性轮），每次失败重试 `KB_LLM_MAX_RETRIES` 次，
故 `单 chunk 最坏耗时 ≈ KB_LLM_REQUEST_TIMEOUT × (1 + KB_LLM_MAX_RETRIES) × 2`（默认 600×3×2 = 3600s）。
改 `.env` 后**必须重启服务**（`python run.py --force`）才生效，启动日志会打印当前生效值：

```
[run] 抽取时间限制：单次请求超时 600s，重试 2 次，并发 4 → 单 chunk 最坏 60 分钟
```

也可随时查 `GET /api/health` 的 `limits` 字段核对。注意爬虫侧推送 `/api/ingest` 的请求超时固定 900s，
超出后爬虫日志报失败但知识库仍在后台继续抽取。

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

## 延迟抽取 / 待处理机制（2026-11-09 新增）

爬虫端「爬取后自动抽取」开关不勾选时，内容仍会推送到 KB 服务，但**只存原始资料 + chunk（标记待处理），不做 LLM 抽取**。之后在图谱页点「处理新增内容」补做。

### 相关 API

| 端点 | 说明 |
|---|---|
| `POST /api/ingest` | 新增字段 `extract`（默认 `true`）。`false` 时只存待处理内容 |
| `GET /api/pending?company=` | 返回 `{pending: n}`——该公司尚未抽取的 chunk 数 |
| `POST /api/process_pending` | body `{company, limit?}`，处理全部待处理 chunk：抽取三元组 → 入库 → 重建 Wiki → 回填快照计数。抽取失败的 chunk 保留待处理状态，可重试 |

### 图谱页按钮

工具栏「处理新增内容」按钮：显示当前公司待处理数量（如「处理新增内容（12 条待抽取）」，有待处理时高亮）；点击后调用 LLM 抽取并刷新图谱。

### 测试

```bash
python test_pending.py   # mock LLM 全链路：extract=false 入库 → process_pending → 图谱/Wiki 校验
```
