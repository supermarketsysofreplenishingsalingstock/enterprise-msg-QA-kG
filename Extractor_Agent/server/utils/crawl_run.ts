import { runCrawlerAgent } from '../services/crawlerAgent'
import type { AgentConfig, CrawlerAgentOptions, EmitEvent } from '../services/crawlerAgentTypes'
import {
  applyExtractorModeOverride,
  applyExtractorModesToConfig,
  describeModesFromConfig,
} from './extractor_modes'
import { attachClarifySuggestions } from './clarification_hints'
import { mergeFollowupTaskWithHistory, type TaskTurn } from './task_condense'
import { applyManagerTaskHints, sanitizeIncomingTask } from './incoming_task'
import { getExtractorAgentEnv } from './extractor_agent_env'
import { extractCompanyNameFromTask } from '../services/companySiteResolver'
import { buildExtractRunMeta, recordCrawlMetric, setRunMeta } from './crawl_metrics'
import { recordRunLearningSignal } from './crawl_learning'
import { buildRouteContextKey, recordChannelOutcomeFromRun } from './crawl_route_policy'
import { inferFailureTagsFromRunAsync } from './crawl_failure_tags'
import { recordExtractTemplate } from './crawl_extract_templates'
import { buildCrawlInjectBlocksAsync, buildExperienceHint, recordCrawlExperience } from './crawl_experience'
import { createQwenChatModel } from '../services/crawlerAgentFrontload'
import { maybeReflectAfterRun } from './crawl_reflect'
import { appendPromptPatch } from './prompt_evolution'
import { runLightweightCuratorOnCrawlEnd } from './learning_curator'
import { learnFromSuccessfulCrawl } from './user_preferences'
import { indexCrawlExperienceVector } from './experience_vectors'
import { ensureRunCost, runCostToMeta } from './runCost'
import { buildExtractorMemoryFacts } from './manager_memory_bridge'

export type ExecuteExtractParams = {
  task: string
  options?: CrawlerAgentOptions
  config: AgentConfig
  signal: AbortSignal
  emit?: (evt: EmitEvent) => void
  manager_task_json?: string
  managerTask?: Record<string, unknown>
  session_id?: string
  source?: string
  history?: TaskTurn[]
}

export async function executeExtractRun(params: ExecuteExtractParams) {
  const env = getExtractorAgentEnv()
  const rawTask = String(params.task ?? '').trim()
  if (!rawTask) throw new Error('task 不能为空')

  const managerRaw =
    params.manager_task_json?.trim() ||
    (params.managerTask && typeof params.managerTask === 'object' ? JSON.stringify(params.managerTask) : '')

  let config = applyExtractorModesToConfig(params.config)
  const reqExtractorMode = String((params.options as any)?.extractor_mode ?? '').trim()
  if (reqExtractorMode) config = applyExtractorModeOverride(config, reqExtractorMode)
  const reqQwenModel = String((params.options as any)?.qwen_model ?? '').trim()
  if (reqQwenModel) config = { ...config, qwenModel: reqQwenModel }

  const condensed = await mergeFollowupTaskWithHistory(rawTask, params.history, createQwenChatModel(config))
  const sanitized = sanitizeIncomingTask(condensed)
  const hinted = applyManagerTaskHints(sanitized, managerRaw || null)
  const modes = describeModesFromConfig(config)
  const options: CrawlerAgentOptions = {
    robotsPolicy: env.robotsPolicy,
    maxItems: env.maxItemsDefault,
    maxPages: env.maxPagesDefault,
    maxConcurrency: env.maxConcurrencyDefault,
    ...params.options,
    ...hinted.options,
  }
  if (params.session_id) (options as any).session_id = params.session_id
  if ((hinted.options as any)?.preferred_channel) {
    (options as any).preferred_channel = (hinted.options as any).preferred_channel
  }
  if ((options as any).preferred_channel === 'mcp' || (hinted.options as any)?.__preferMcp) {
    (options as any).__preferMcp = true
  }
  if (!Number.isFinite(Number((options as any).__mcpMaxCalls))) {
    (options as any).__mcpMaxCalls = env.mcpMaxCallsDefault
  }
  const sessionKey = params.session_id || String((options as any).session_id ?? '').trim() || undefined
  const embeddingConfig = {
    apiKey: String(config?.qwenApiKey ?? ''),
    baseUrl: String(config?.qwenBaseUrl ?? env.qwenBaseUrl),
    model: env.embeddingModel,
  }
  ;(options as any).__rawTaskForTemplates = hinted.task
  ;(options as any).__injectBlocks = await buildCrawlInjectBlocksAsync(hinted.task, sessionKey, embeddingConfig)

  ensureRunCost(options as Record<string, unknown>)
  const emit = params.emit ?? (() => {})
  const t0 = Date.now()
  const result = await runCrawlerAgent({
    task: hinted.task,
    options,
    config,
    signal: params.signal,
    emit,
  })

  const enriched = attachClarifySuggestions(result)
  const model = createQwenChatModel(config)
  const failureTags = await inferFailureTagsFromRunAsync(enriched, model)
  const routeContextKey = buildRouteContextKey({
    targetSite: enriched?.taskPlan?.targetSite,
    contentType: enriched?.taskPlan?.contentType,
    antiBotRisk: enriched?.preflight?.antiBotRisk,
  })
  const meta = {
    ...buildExtractRunMeta(enriched),
    ...runCostToMeta(ensureRunCost(options as Record<string, unknown>)),
    ...(enriched?.meta &&
    typeof enriched.meta === 'object' &&
    (enriched.meta as { serp_fallback?: boolean }).serp_fallback
      ? { serp_fallback: true }
      : {}),
    extractor_mode: modes.extractorMode,
    planner_mode: modes.plannerMode,
    agent_mode: modes.agentMode,
    mode_label: modes.label,
    route_context_key: routeContextKey,
    failure_tags: failureTags,
    session_id: sessionKey,
  }
  const withMeta = { ...enriched, meta }
  setRunMeta(meta)

  const reflectHint = await maybeReflectAfterRun(model, hinted.task, withMeta)
  if (reflectHint) {
    withMeta.meta = { ...withMeta.meta, reflect_hint: reflectHint }
    setRunMeta(withMeta.meta)
  }

  const ms = Date.now() - t0
  if (env.enableCrawlMetrics) {
    const status = String(result?.status ?? '')
    const items = Array.isArray(result?.items) ? result.items : []
    recordCrawlMetric({
      target_site: meta.target_site,
      content_type: meta.content_type,
      channel: meta.primary_channel,
      ok: status === 'ok' || status === 'partial_ok',
      empty: items.length === 0 && status !== 'needs_clarification',
      quality_passed: meta.quality_passed,
      retry_triggered: meta.retry_triggered,
      ms,
      task: hinted.task.slice(0, 200),
      status,
    })
  }

  recordRunLearningSignal({
    task: hinted.task,
    result: withMeta,
    ms,
    source: params.source,
  })

  recordChannelOutcomeFromRun(withMeta, ms)

  const status = String(withMeta?.status ?? '')
  const items = Array.isArray(withMeta?.items) ? withMeta.items : []
  if ((status === 'ok' || status === 'partial_ok') && items.length > 0) {
    const tp = withMeta.taskPlan ?? {}
    const plan = (withMeta as any).plan ?? {}
    const seedUrl =
      String((withMeta as any)?.plan?.seedUrls?.[0] ?? '').trim() ||
      String(withMeta?.stats?._routeLog?.[0]?.url ?? '').trim()
    recordExtractTemplate({
      task: hinted.task,
      target_site: tp.targetSite,
      content_type: tp.contentType,
      seed_url: seedUrl,
      fields: tp.fields,
      channel: meta.primary_channel,
      entity: (withMeta as any)?.plan?.extraction?.entity,
      item_count: items.length,
    })
    recordCrawlExperience({
      task: hinted.task,
      target_site: tp.targetSite,
      content_type: tp.contentType,
      channel: meta.primary_channel,
      seed_url: seedUrl,
      fields: tp.fields,
    })
    const expHint = buildExperienceHint({
      target_site: tp.targetSite,
      content_type: tp.contentType,
      channel: meta.primary_channel,
      seed_url: seedUrl,
      fields: tp.fields,
    })
    await indexCrawlExperienceVector({
      task: hinted.task,
      hint: expHint,
      target_site: tp.targetSite,
      content_type: tp.contentType,
      channel: meta.primary_channel,
      embeddingConfig,
    })
    learnFromSuccessfulCrawl({
      sessionKey,
      task: hinted.task,
      target_site: tp.targetSite,
      content_type: tp.contentType,
      limit: tp.limit ?? plan?.maxItems,
      fields: tp.fields,
      channel: meta.primary_channel,
      output_format: tp.outputSpec?.format,
    })
    const memoryFacts = buildExtractorMemoryFacts({
      task: hinted.task,
      target_site: tp.targetSite,
      content_type: tp.contentType,
      channel: meta.primary_channel,
      item_count: items.length,
      seed_url: seedUrl,
      fields: tp.fields,
    })
    if (memoryFacts.length) {
      withMeta.meta = { ...withMeta.meta, memory_facts: memoryFacts }
      setRunMeta(withMeta.meta)
    }
  } else if (items.length === 0 && env.enablePromptEvolution) {
    appendPromptPatch({
      stage: 'plan',
      text: `空结果：优先核对种子 URL 与通道（当前 ${meta.primary_channel ?? 'unknown'}）`,
      source: 'empty_result',
    })
  }

  runLightweightCuratorOnCrawlEnd()

  // ── 公司爬取：自动推送知识库 ingest（LLM Wiki + DynamicLLMPathExtractor 图谱一体完成）──
  // 前端「爬取后自动抽取」开关 → options.__kbAutoIngest（默认 true）。
  // 不勾选时仍会推送内容存为「待处理」（extract:false），可在图谱页点「处理新增内容」补做抽取。
  // 兜底：即使不是公司流程（如直接给 URL 爬取），只要抓到了页面文本也推送入库，
  // 公司名优先 options.__companyName，其次从任务文本识别，最后用首个页面的域名。
  const kbPagesAll: any[] = Array.isArray(withMeta.kbPages) ? withMeta.kbPages : []
  const isCompanyCrawl = (options as any).__companyCrawl === true
  if (env.kbAutoIngest && ['ok', 'partial_ok'].includes(status) && (isCompanyCrawl || kbPagesAll.length)) {
    let companyName = String((options as any).__companyName ?? '').trim()
    if (!companyName) companyName = extractCompanyNameFromTask(String(withMeta.task ?? '')) ?? ''
    if (!companyName) {
      try {
        const firstUrl = new URL(String(kbPagesAll[0]?.url ?? 'https://unknown'))
        companyName = firstUrl.hostname.replace(/^www\./, '')
      } catch {
        companyName = '未命名公司'
      }
    }
    if (!companyName) {
      withMeta.meta = { ...withMeta.meta, kb_ingest: { ok: false, error: 'missing company name' } }
    } else {
      // 前端两条入口（公司爬取 / 开始任务）都会显式传 __kbAutoIngest；
      // 未传时（如脚本直接调 /api/extract）沿用历史默认「开」，但日志里标注，避免与 UI 勾选状态对不上
      const kbAutoIngestRaw = (options as any).__kbAutoIngest
      const kbAutoIngestExplicit = typeof kbAutoIngestRaw === 'boolean'
      const kbExtract = kbAutoIngestRaw !== false
      try {
        const kbUrl = env.kbServiceUrl.replace(/\/$/, '')
        const kbPayload = {
          company: companyName,
          crawl_time: new Date().toISOString().slice(0, 19).replace('T', ' '),
          note: String(withMeta.task ?? '').slice(0, 200),
          extract: kbExtract,
          pages: kbPagesAll,
          files: (Array.isArray(withMeta.attachments) ? withMeta.attachments : [])
            .filter((f: any) => f?.ok && f?.path)
            .map((f: any) => ({
              url: f.url,
              title: f.filename,
              path: f.path,
              content_type: f.contentType,
              size: f.size,
            })),
        }
        emit?.({
          type: 'log',
          payload: {
            level: 'info',
            message: `知识库：推送「${companyName}」爬取内容到 ${kbUrl}（页面 ${kbPayload.pages.length} / 文件 ${kbPayload.files.length}，${
              kbExtract ? (kbAutoIngestExplicit ? '自动抽取' : '自动抽取（未显式关闭，默认开）') : '存为待处理'
            }）`,
            ts: Date.now(),
          },
        })
        const kbResp = await fetch(`${kbUrl}/api/ingest`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(kbPayload),
          signal: AbortSignal.timeout(900_000),
        })
        const kbRes: any = await kbResp.json().catch(() => null)
        if (kbResp.ok && kbRes?.ok) {
          withMeta.meta = {
            ...withMeta.meta,
            kb_ingest: kbExtract
              ? {
                  ok: true,
                  mode: 'auto_extract',
                  snapshot_id: kbRes.snapshot_id,
                  triplets: kbRes.triplet_count,
                  wiki_pages: kbRes.wiki_pages_updated,
                  graph_ui: `${kbUrl}/`,
                }
              : {
                  ok: true,
                  mode: 'pending',
                  snapshot_id: kbRes.snapshot_id,
                  pending_chunks: kbRes.pending_chunks,
                  note: '已存为待处理内容，请在图谱页点「处理新增内容」',
                  graph_ui: `${kbUrl}/`,
                },
          }
          emit?.({
            type: 'log',
            payload: {
              level: 'info',
              message: kbExtract
                ? `知识库：ingest 完成（快照 ${kbRes.snapshot_id}，三元组 ${kbRes.triplet_count}，实体页 ${kbRes.wiki_pages_updated}）；图谱界面 ${kbUrl}/`
                : `知识库：内容已存为待处理（chunk ${kbRes.pending_chunks}，未抽取）；在图谱页点「处理新增内容」完成抽取`,
              ts: Date.now(),
            },
          })
        } else {
          withMeta.meta = {
            ...withMeta.meta,
            kb_ingest: { ok: false, error: String(kbRes?.detail ?? kbResp.statusText).slice(0, 240) },
          }
        }
        setRunMeta(withMeta.meta)
      } catch (e: any) {
        withMeta.meta = {
          ...withMeta.meta,
          kb_ingest: { ok: false, error: String(e?.message ?? e).slice(0, 240) },
        }
        setRunMeta(withMeta.meta)
        emit?.({
          type: 'log',
          payload: {
            level: 'warn',
            message: `知识库：ingest 失败（${String(e?.message ?? e).slice(0, 160)}）。请确认 wiki_kb_service 已启动。`,
            ts: Date.now(),
          },
        })
      }
    }
  }

  return withMeta
}
