# Extractor Agent

> **学习文档**：[入门](../docs/Agent学习指南-入门版.md) · [进阶](../docs/Agent学习指南-进阶版.md) · [Extractor 专篇](学习指南.md)

基于 **Nuxt 4 + Nitro + LangGraph** 的网页抓取与结构化抽取 Agent。它不只是“爬页面”，而是把自然语言任务拆成可执行计划，再在 **HTTP、Playwright 浏览器、MCP、Skill** 等通道之间选择最合适的执行方式，最终输出结构化结果并带质量评分。

本目录位于单体仓库 `Extractor_Agent/` 中，对应平台编排里的 `extractor_agent` 服务，默认端口通常为 **13104**。

## 项目定位（面试版）

Extractor Agent 适合展示“如何把采集任务工程化”。它的核心不是单次抓一页，而是：

- 先理解用户要什么字段
- 再规划站点、分页、深度和输出格式
- 然后执行抓取
- 最后根据覆盖率、重复率、数量等指标判断是否需要重试

它和 **Lobster Agent** 的区别很关键：

- **Extractor** 偏静态页面、半静态页面和列表型数据抽取
- **Lobster** 偏浏览器 GUI 操作和复杂交互

**通道说明（2026-06 升级）**：

- **HTTP**：直连 fetch + cheerio，默认首选
- **Playwright**：动态页 / 反爬 / 总管 `seed_urls` 精抓
- **云抓取**（env `MCP_*`，代码内称 cloud_scrape）：Firecrawl 等付费兜底；国内可自托管 `MCP_BASE_URL`
- **异步队列**（`EXTRACTOR_ASYNC_QUEUE=1`）：`POST /api/extract/async` → `GET /api/jobs/{id}`
- **MCP Server**（`EXTRACTOR_MCP_SERVER=1`）：`POST /api/mcp` 暴露 `scrape_url` / `extract_task`
- **总管种子**：`manager_task_json.seed_urls` → Seed-first 模式
- **站点补丁**：`patches/sites/*.json`，换站只加 JSON（见 `patchRegistry.ts`）
- **抽取路径**：patch → template → rule → llm → heuristic（见 `meta.extract_path`）

## 技术栈与实现方式

- **框架**：`Nuxt 4`、`Nitro`
- **Agent 编排**：`LangGraph`、`@langchain/openai`
- **结构化校验**：`zod`
- **页面解析**：`cheerio`、`turndown`
- **浏览器执行**：`playwright` / `playwright-core`
- **抓取冒烟测试**：`scripts/extractor-smoke.mjs`
- **结果输出**：`crawler_results.json`

## 面试者建议：先学什么

1. **静态抓取与动态渲染的区别**
   - 静态页面优先用 `fetch + cheerio`
   - 动态站点再上浏览器

2. **为什么要先规划再执行**
   - 没有计划就容易乱抓
   - 任务计划能明确站点、字段、分页和质量目标

3. **为什么要有质量门禁**
   - 采集结果常见问题是覆盖不全、重复过多、条数不足
   - 通过评分和重试，可以让采集更稳定

4. **为什么要管 robots 和限速**
   - 这是爬虫工程化的基本边界
   - 能减少法律和稳定性风险

## 如何实现这个 Agent

推荐按这个路径理解或复现：

1. **单页字段抽取**
   - 先用 `fetch` 拉 HTML
   - 用 `cheerio` 提取标题、链接、时间等字段

2. **增加任务计划**
   - 将自然语言转成结构化 `taskPlan`
   - 明确 `maxPages`、`maxItems`、输出字段与质量要求

3. **增加动态页面通道**
   - 对需要 JS 渲染的页面切换到 Playwright
   - 对特定站点可使用更专门的执行器或 MCP

4. **增加评分与重试**
   - 抽取后计算覆盖率、重复率、条数
   - 低质量时自动二次抓取

5. **增加结果沉淀**
   - 输出到 JSON
   - 便于 Manager 或上层编排做汇总

## 目录结构速览

- `server/services/`：抓取编排、执行器、任务规划与质量控制
- `server/routes/`：WebSocket 等运行入口
- `server/api/`：HTTP 接口与任务提交入口
- `scripts/extractor-smoke.mjs`：冒烟验证脚本
- `crawler_results.json`：默认结果产物

## 关键能力点

- `robotsPolicy`：控制是否遵守 robots 规则
- `rateLimit`：限速与退避
- `retry`：失败后的二次抓取策略
- `quality`：覆盖率、重复率、最小条数等门禁
- `useBrowser`：是否使用浏览器执行

这些点很适合面试时讲“为什么这个 Agent 比普通爬虫更稳”。

## 快速开始

```bash
cd Extractor_Agent
npm install
npm run dev
```

构建生产包：

```bash
npm run build
```

## 常用运行参数

```json
{
  "maxPages": 3,
  "maxItems": 30,
  "maxConcurrency": 3,
  "useBrowser": false,
  "outputJsonPath": "crawler_results.json",
  "robotsPolicy": "strict",
  "pageMode": "auto",
  "siteCrawl": false,
  "maxDepth": 2,
  "siteCrawlScope": "auto"
}
```

### 单页模式与整站爬取

- **单页模式**（`pageMode: "single"`）：整个页面作为一条结果，字段为 `title / url / excerpt`。
  任务里出现「主页 / 首页 / 单页」且不含列表语义时也会自动启用。
- **自动兜底**：`pageMode: "auto"`（默认）下优先按列表抽取；页面没有任何列表条目时，同样产出整页一条，
  避免示例页、个人主页这类内容被误判为 0 条。
- **整站爬取**（`siteCrawl: true`）：从种子页开始按 BFS 跟踪链接，每个页面产出一条结果。
  任务里出现「整个网站 / 全站 / 整站 / 所有页面」时自动启用。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `maxDepth` | `2` | 链接深度，种子页为第 0 层 |
| `siteCrawlScope` | `auto` | `host` 仅同主机；`linked` 把种子页外链也纳入（最多 10 个主机）；`auto` 在种子页几乎没有站内链接时按 `linked` 处理 |
| `CRAWLER_SITE_MAX_PAGES` | `50` | 整站模式最大抓取页数 |
| `CRAWLER_SITE_MAX_ITEMS` | `50` | 整站模式最大输出条目 |
| `CRAWLER_SITE_MAX_DEPTH` | `2` | 整站模式默认深度 |

### `robotsPolicy`

- `strict`：默认，命中禁止规则则跳过
- `warn`：只告警，继续执行
- `off`：关闭检查，只建议在受控环境使用

## 面试常问点

### 为什么要有多通道

因为不同站点的页面形态不同：

- 有的只需 HTTP
- 有的需要浏览器渲染
- 有的需要更专门的能力或协作工具

单一通道很难兼容所有场景。

### 为什么要做质量评分

抓取不是“有没有数据”就够了，而是要判断：

- 抓全了吗
- 重复了吗
- 样本够不够
- 是否需要重新采样

### 如何控制风险

- 使用 robots 和限速
- 控制并发
- 限制目标域
- 对浏览器执行做开关控制

## 能力边界

- **适合**：列表页、详情页、结构化字段采集、需要分页和重试的场景
- **不适合**：强 GUI 交互任务、无视 robots 的大规模爬取

## Docker / 平台编排

在 `Manage-platform_Agent` 中，该服务默认映射为 **`13104:13104`**。

## 安全提示

- 浏览器通道会执行页面脚本，必须谨慎使用
- 生产环境不要关闭合规检查
- 不要把真实密钥写进仓库

## 常见问题

- **Playwright 没装好**：看构建日志是否成功安装 Chromium
- **结果太少**：调大 `maxPages` 或放宽质量阈值
- **总走错误通道**：检查任务计划和路由规则

## 文档

- 待办：[docs/Skill化升级计划.md](../docs/Skill化升级计划.md)（Extractor 章节）
## 公司信息爬取 + LLM Wiki 知识库（2026-11 新增）

本 Agent 已扩展"公司信息爬取"能力，与同仓 `wiki_kb_service/`（LLM Wiki + 图谱服务，端口 13105）联动：

### 功能

1. **公司站点解析与澄清**：只输入公司名（不输入 URL）时，用 Bing 搜索候选官网；
   - 找到多个候选 → 返回 `needs_clarification`（reason `company_candidates`），前端展示候选列表让用户勾选"爬取选中/全部爬取"
   - 找不到 → reason `company_not_found`
   - 恰好一个 → 自动进入 seed-first 模式爬取
2. **条件/特征过滤**：`companyTraits` 会并入搜索 query，帮助定位更准确的官网
3. **网站挂载文件下载**：抓取过程中自动收集页面挂载的 pdf/docx/xlsx/pptx/zip/txt/csv/ofd 等附件并下载（预算 `COMPANY_ATTACH_MAX`，默认 20 个）
4. **自动入库**：公司爬取结束后，自动把页面文本 + 附件文本 POST 到 `wiki_kb_service` 的 `/api/ingest`，完成 LLM Wiki 三层落盘 + 开放式图谱抽取（`DynamicLLMPathExtractor`，不预设 schema、必提属性、宽松解析不丢三元组）

### 前端入口（app/pages/index.vue）

- 任务区新增"公司信息爬取"面板：公司名、公司特征、`collectAttachments` 勾选、"搜索官网候选"按钮
- 候选澄清卡片：勾选候选网站后"爬取选中"或"全部爬取"
- 结果区展示：附件下载列表、`kb_ingest` 入库状态（快照 id / 三元组数 / Wiki 页面数）、"知识库/图谱界面"跳转按钮

### 相关环境变量（.env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `KB_SERVICE_URL` | `http://127.0.0.1:13105` | wiki_kb_service 地址 |
| `KB_AUTO_INGEST` | `1` | 爬取结束自动入库 |
| `COMPANY_ATTACH_MAX` | `20` | 单次爬取最多下载的附件数 |

LLM 三选一（本地 DeepSeek / 本地 Ministral / 线上 DeepSeek API）见 `.env.example` 内注释。

### 模块互通（同一项目内互相跳转）

- 爬取工作台：任务区「知识库 / 图谱界面」按钮 → 站内路由 `/kb`（iframe 嵌入图谱 UI，不脱离应用）；旁有「↗」按钮新窗口打开
- 爬取结果区：入库状态行带「查看图谱 →」按钮，爬完一键看图
- 图谱 UI：页头「← 公司爬取」链接 → 跳回爬取工作台（iframe 内嵌时自动隐藏，避免嵌套跳转）
- 公司下拉默认选中最新爬取的公司（按 id 倒序）

### 端到端启动顺序

```bash
# 1) 启动知识库服务
cd wiki_kb_service && python run.py        # :13105

# 2) 启动爬虫 Agent
cd Extractor_Agent && pnpm install && pnpm dev   # :13104

# 3) 浏览器打开爬虫界面，输入公司名 → 搜索候选 → 爬取 → 自动入库
#    图谱界面： http://127.0.0.1:13105
```

## 自动抽取开关（2026-11-09 新增）

「公司信息爬取」面板新增「**爬取后自动抽取（知识库 / 图谱）**」勾选项（默认勾选）：

- **勾选**：爬取完成后自动推送 KB 服务并即时抽取（LLM 关系/属性分析 + Wiki 更新），日志显示 `ingest 完成`
- **不勾选**：内容仍推送存为「待处理」（不调 LLM、秒级完成），日志提示存为待处理；之后在图谱页（:13105）点「**处理新增内容**」按钮补做抽取，按钮上会显示待处理条数

对应环境变量：前端勾选状态按任务传 `options.__kbAutoIngest`；`KB_AUTO_INGEST=0` 仍为整个联动的总开关。
