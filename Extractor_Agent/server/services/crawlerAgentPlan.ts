/**
 * 抓取规划：种子队列、启发式/LLM 计划、槽位澄清与冲突检测。
 */
import { ChatPromptTemplate } from '@langchain/core/prompts'
import { StringOutputParser } from '@langchain/core/output_parsers'
import { RunnableSequence } from '@langchain/core/runnables'
import { ChatOpenAI } from '@langchain/openai'
import { withQwenModelKwargs } from '#agent-shared/qwenModelKwargs'
import { z } from 'zod'
import { getCapabilityProfile } from './capabilityRegistry'
import type { TargetSite, ContentType } from './capabilityRegistry'
import {
  StructuredTaskPlanSchema,
  buildHeuristicStructuredTaskPlan,
  type StructuredTaskPlan,
} from './crawlerAgentTaskPlanning'
import {
  inferStructuralTaskPlan,
  mergeStructuralIntoTaskPlan,
  parseTaskLimitStructural,
} from './structural_task_infer'
import {
  buildBingSearchSeedFromTaskText,
  canonicalizeSeedUrl,
} from './crawlerAgentTaskUtils'
import { extractHttpUrl } from '../utils/url_extract'
import { getExtractorAgentEnv } from '../utils/extractor_agent_env'
import type { CrawlInjectBlocks } from '../utils/crawl_experience'
import type { AgentConfig, CrawlerAgentOptions } from './crawlerAgentTypes'
import {
  buildSeedCrawlPlanTemplate,
  buildSlotInferPrompt,
  buildStructuredTaskPlanPrompt,
  getSlotClarifyDefaults,
} from '../utils/extractor_playbook_prompts'

const PlanSchema = z.object({
  target: z.string().default(''),
  seedUrls: z.array(z.string()).min(1),
  extraction: z
    .object({
      entity: z.string().default('item'),
      fields: z.array(z.string()).default(['title', 'url']),
      vision: z.boolean().optional().default(false),
    })
    .default({ entity: 'item', fields: ['title', 'url'], vision: false }),
  needsLogin: z.boolean().optional().default(false),
  maxPages: z.number().optional().default(1),
  maxItems: z.number().optional().default(10),
  siteCrawl: z.boolean().optional().default(false),
  maxDepth: z.number().optional().default(2),
  siteCrawlScope: z.enum(['auto', 'host', 'linked']).optional().default('auto'),
  pageMode: z.enum(['auto', 'single', 'list']).optional().default('auto'),
})

export type PlannerPlan = z.infer<typeof PlanSchema>

/** 「整站/全站」类任务：跟踪同站链接逐页抓取 */
const SITE_CRAWL_HINT =
  /整站|全站|整个网站|整个站点|整个官网|全官网|官网所有|官网上?(?:的)?(?:所有|全部)|网站(?:上)?(?:的)?(?:所有|全部)|所有页面|所有网页|所有信息|全部信息|所有内容|全部内容|所有资料|全部资料|站内(?:所有|全部)?|遍历(?:整个)?(?:网站|站点|站)|(?:爬取|抓取)整个(?:网站|站点|站)|全站信息|整站信息/i

/** 明确的列表/榜单语义：不进入单页模式 */
const LIST_HINT =
  /列表|榜单|排行|热榜|热搜|top\s*\d+|前\s*\d+\s*[条个篇名]|所有文章|全部文章|文章列表|新闻列表/i

/** 单页语义 */
const SINGLE_HINT = /单页|单个页面|这个页面|该页面|此页面|此页|个人主页|我的主页|个人网站/i
const HOME_HINT = /主页|首页/

export type CrawlIntent = {
  siteCrawl: boolean
  pageMode: 'auto' | 'single' | 'list'
  siteCrawlScope: 'auto' | 'host' | 'linked'
  maxDepth: number
  maxPages: number
  maxItems: number
}

/** 整站预算：显式 options 优先，否则用环境默认 */
export function resolveSiteCrawlBudget(options?: CrawlerAgentOptions) {
  const env = getExtractorAgentEnv()
  const optPages = Number(options?.maxPages)
  const optItems = Number(options?.maxItems)
  const optDepth = Number(options?.maxDepth)
  return {
    maxPages: Number.isFinite(optPages) && optPages > 1 ? Math.min(200, Math.floor(optPages)) : env.siteCrawlMaxPages,
    maxItems:
      Number.isFinite(optItems) && optItems > 0 && optItems !== env.maxItemsDefault
        ? Math.min(250, Math.floor(optItems))
        : env.siteCrawlMaxItems,
    maxDepth: Number.isFinite(optDepth) && optDepth > 0 ? Math.min(5, Math.floor(optDepth)) : env.siteCrawlMaxDepth,
  }
}

/**
 * 从任务文本/选项里识别抓取形态：
 *  - 整站：跟踪同站链接，每页产出一条（title/url/excerpt）
 *  - 单页：整页一条
 *  - 列表：沿用既有列表抽取
 */
export function detectCrawlIntent(task: string, options?: CrawlerAgentOptions): CrawlIntent {
  const t = String(task ?? '')
  const env = getExtractorAgentEnv()
  const explicitSite = Boolean(options?.siteCrawl) || SITE_CRAWL_HINT.test(t)
  const budget = resolveSiteCrawlBudget(options)
  const forcedScope = String(options?.siteCrawlScope ?? '').toLowerCase()
  const siteCrawlScope: 'auto' | 'host' | 'linked' =
    forcedScope === 'host' || forcedScope === 'linked' || forcedScope === 'auto' ? forcedScope : 'auto'
  if (explicitSite) {
    return {
      siteCrawl: true,
      pageMode: 'single',
      siteCrawlScope,
      maxDepth: budget.maxDepth,
      maxPages: budget.maxPages,
      maxItems: budget.maxItems,
    }
  }

  const forced = String(options?.pageMode ?? '').toLowerCase()
  const basePages = Number.isFinite(Number(options?.maxPages)) ? Math.max(1, Math.floor(Number(options!.maxPages))) : 1
  const baseItemsRaw = Number(options?.maxItems)
  const baseItems =
    Number.isFinite(baseItemsRaw) && baseItemsRaw > 0 ? Math.max(1, Math.floor(baseItemsRaw)) : env.maxItemsDefault
  const depth = Number.isFinite(Number(options?.maxDepth)) && Number(options!.maxDepth) > 0
    ? Math.min(5, Math.floor(Number(options!.maxDepth)))
    : env.siteCrawlMaxDepth

  if (forced === 'single' || forced === 'list' || forced === 'auto') {
    return {
      siteCrawl: false,
      pageMode: forced,
      siteCrawlScope,
      maxDepth: depth,
      maxPages: basePages,
      maxItems: forced === 'single' ? 1 : baseItems,
    }
  }

  const hasList = LIST_HINT.test(t)
  const singleByText =
    !hasList &&
    (SINGLE_HINT.test(t) || (HOME_HINT.test(t) && Boolean(extractHttpUrl(t))))
  return {
    siteCrawl: false,
    pageMode: singleByText ? 'single' : 'auto',
    siteCrawlScope,
    maxDepth: depth,
    maxPages: basePages,
    maxItems: singleByText ? 1 : baseItems,
  }
}

/** 把抓取形态写进计划（放在用户限额归一化之后调用，避免预算被默认值覆盖） */
export function applyCrawlIntent<T extends Record<string, any>>(plan: T, intent: CrawlIntent): T {
  const next: Record<string, any> = { ...plan }
  next.siteCrawl = intent.siteCrawl
  next.maxDepth = intent.maxDepth
  next.siteCrawlScope = intent.siteCrawlScope
  next.pageMode = intent.pageMode
  // 只有整站/单页模式需要改写页数预算；列表与公网检索沿用规划阶段的结果
  if (intent.siteCrawl || intent.pageMode === 'single') {
    next.maxPages = intent.maxPages
    next.maxItems = intent.maxItems
  }
  if (intent.siteCrawl || intent.pageMode === 'single') {
    const fields = Array.isArray(plan?.extraction?.fields) ? plan.extraction.fields.map(String) : ['title', 'url']
    next.extraction = {
      ...(plan?.extraction ?? { entity: 'item' }),
      entity: plan?.extraction?.entity ?? 'page',
      fields: Array.from(new Set([...fields, 'title', 'url', 'excerpt'])),
    }
  }
  return next as T
}

/** 总管 seed_urls 优先：跳过 LLM/Bing，直接 BFS 精抓种子 */
export function buildSeedFirstPlan(
  managerSeeds: string[],
  options?: CrawlerAgentOptions,
  taskPlan?: StructuredTaskPlan | null,
): PlannerPlan {
  const seeds = managerSeeds
    .map((u) => String(u ?? '').trim())
    .filter((u) => u.startsWith('http://') || u.startsWith('https://'))
    .slice(0, 12)
  const hintFields = Array.isArray((options as any)?.hint_fields)
    ? (options as any).hint_fields.map((x: unknown) => String(x ?? '').trim()).filter(Boolean)
    : []
  const fields =
    hintFields.length > 0
      ? hintFields.slice(0, 12)
      : taskPlan?.fields?.length
        ? taskPlan.fields.slice(0, 12)
        : ['title', 'url', 'excerpt', 'source']
  const maxItems = Number.isFinite(Number(options?.maxItems))
    ? Math.max(1, Math.floor(Number(options?.maxItems)))
    : Number.isFinite(Number(taskPlan?.limit)) && Number(taskPlan!.limit) > 0
      ? Math.floor(Number(taskPlan!.limit))
      : 10
  const maxPages = Math.max(1, Math.min(10, seeds.length))
  return {
    target: 'manager_seeds',
    seedUrls: seeds.length ? seeds : ['about:blank#unresolved_seed'],
    needsLogin: Boolean(taskPlan?.needsAuth),
    extraction: { entity: 'item', fields, vision: false },
    maxPages,
    maxItems,
  }
}

function seedFromTaskPlan(taskPlan?: StructuredTaskPlan | null): string | null {
  if (!taskPlan || taskPlan.targetSite === 'generic') return null
  const prof = getCapabilityProfile(taskPlan.targetSite as TargetSite, taskPlan.contentType as ContentType)
  return prof?.defaultSeedUrls?.[0] ?? null
}

export function buildHeuristicPlan(
  task: string,
  options?: CrawlerAgentOptions,
  taskPlan?: StructuredTaskPlan | null,
): PlannerPlan {
  const t = String(task ?? '')
  const explicitUrl = extractHttpUrl(t)
  const managerSeeds = Array.isArray((options as any)?.__managerSeedUrls)
    ? (options as any).__managerSeedUrls
    : []
  const hasManagerSeeds = managerSeeds.length > 0
  if (hasManagerSeeds) {
    return buildSeedFirstPlan(managerSeeds, options, taskPlan)
  }
  let seedUrl = 'about:blank#unresolved_seed'
  if (explicitUrl) {
    seedUrl = explicitUrl
  } else if (taskPlan?.openWebSearch && !(options as any)?.__seedFirstMode) {
    seedUrl = buildBingSearchSeedFromTaskText(t)
  } else {
    const fromPlan = seedFromTaskPlan(taskPlan)
    if (fromPlan) seedUrl = fromPlan
  }

  seedUrl = canonicalizeSeedUrl(t, seedUrl, taskPlan)

  const needsLogin = Boolean(taskPlan?.needsAuth)

  const baseMaxPages = Number.isFinite(Number(options?.maxPages))
    ? Math.max(1, Math.floor(Number(options?.maxPages)))
    : 1
  const allowOpenWeb = Boolean(taskPlan?.openWebSearch) && !hasManagerSeeds && !(options as any)?.__seedFirstMode
  const maxPages = allowOpenWeb ? Math.max(baseMaxPages, 6) : baseMaxPages

  const seedUrls = [seedUrl]

  const basePlan: PlannerPlan = {
    target: 'generic_web',
    seedUrls,
    needsLogin,
    extraction: {
      entity: 'item',
      fields: allowOpenWeb ? ['title', 'url', 'excerpt', 'source'] : ['title', 'url'],
      vision: false,
    },
    maxPages,
    maxItems: (() => {
      const fromOpts = Number(options?.maxItems)
      if (Number.isFinite(fromOpts) && fromOpts > 0) return Math.max(1, Math.floor(fromOpts))
      const fromPlan = Number(taskPlan?.limit)
      if (Number.isFinite(fromPlan) && fromPlan > 0) return Math.max(1, Math.floor(fromPlan))
      return 10
    })(),
  }
  return applyCrawlIntent(basePlan, detectCrawlIntent(t, options))
}

export function isUnresolvedSeed(seedUrl: string) {
  return String(seedUrl ?? '').trim().toLowerCase() === 'about:blank#unresolved_seed'
}

/** 结构性澄清预判（无 LLM，供回归测试与快速门禁） */
export function needsCrawlerClarifyStructural(task: string): boolean {
  const structural = inferStructuralTaskPlan(task)
  if (structural.targetSite !== 'generic') return false
  const preview = buildHeuristicPlan(task, undefined, buildHeuristicStructuredTaskPlan(task))
  const seed = String(preview.seedUrls?.[0] ?? '').trim()
  return isUnresolvedSeed(seed)
}

function safeJsonParse(text: string) {
  try {
    const jsonMatch =
      text.match(/```json\s*([\s\S]*?)\s*```/) ||
      text.match(/\[\s*\{[\s\S]*\}\s*\]/) ||
      text.match(/\{\s*[\s\S]*\s*\}/)
    const toParse = jsonMatch ? (Array.isArray(jsonMatch) ? jsonMatch[1] || jsonMatch[0] : jsonMatch) : text
    return JSON.parse(toParse)
  } catch {
    return null
  }
}

type CrawlerSlotInfer = {
  hasSource: boolean
  hasGoal: boolean
  hasLimit: boolean
  limitValue?: number
  confidence?: number
  sourceHint?: string
  goalHint?: string
  limitHint?: string
}

const CrawlerSlotInferSchema = z.object({
  hasSource: z.boolean().default(false),
  hasGoal: z.boolean().default(false),
  hasLimit: z.boolean().default(false),
  limitValue: z.number().int().min(1).max(250).optional(),
  confidence: z.number().min(0).max(1).optional(),
  sourceHint: z.string().optional(),
  goalHint: z.string().optional(),
  limitHint: z.string().optional(),
})

export function createQwenChatModel(config: AgentConfig, vision: boolean = false) {
  const apiKey = String(config?.qwenApiKey ?? '').trim()
  if (!apiKey) return null
  const baseURL = String(config?.qwenBaseUrl ?? 'https://dashscope.aliyuncs.com/compatible-mode/v1').trim()
  const modelName = vision ? (config?.qwenVlModel ?? 'qwen-vl-plus') : (config?.qwenModel ?? 'qwen3.5-flash')
  return new ChatOpenAI(
    withQwenModelKwargs(modelName, {
      apiKey,
      modelName,
      configuration: { baseURL },
      enableThinking: Boolean(config?.qwenEnableThinking),
      skipForVision: vision,
    }) as ConstructorParameters<typeof ChatOpenAI>[0]
  )
}

async function inferCrawlerSlotsByLlm(task: string, config: AgentConfig, inject?: string): Promise<CrawlerSlotInfer | null> {
  const model = createQwenChatModel(config)
  if (!model) return null
  try {
    const prompt = buildSlotInferPrompt(task, inject)
    const res = await model.invoke([['human', prompt]])
    const parsed = safeJsonParse(String(res.content ?? '').trim())
    const safe = CrawlerSlotInferSchema.safeParse(parsed)
    if (!safe.success) return null
    return safe.data
  } catch {
    return null
  }
}

export async function detectCrawlerMissingSlotsSmart(
  task: string,
  config: AgentConfig,
  opts?: { openWebSearch?: boolean; fromManager?: boolean; inject?: string },
) {
  if (opts?.openWebSearch || opts?.fromManager) return null

  const semantic = await inferCrawlerSlotsByLlm(task, config, opts?.inject)
  if (!semantic) return null

  const conf = Number(semantic.confidence ?? 0)
  if (conf < 0.72) return null

  const missingSlots: string[] = []
  if (!semantic.hasSource) missingSlots.push('source')
  if (!semantic.hasGoal) missingSlots.push('goal')
  if (!semantic.hasLimit && !Number.isFinite(Number(semantic.limitValue))) missingSlots.push('limit')

  if (!missingSlots.length) return null
  if (missingSlots.length >= 3) {
    const defaults = getSlotClarifyDefaults()
    const questions: string[] = []
    if (missingSlots.includes('source')) {
      const hint = String(semantic.sourceHint ?? '').trim()
      questions.push(hint || defaults.source)
    }
    if (missingSlots.includes('goal')) {
      const hint = String(semantic.goalHint ?? '').trim()
      questions.push(hint || defaults.goal)
    }
    if (missingSlots.includes('limit')) {
      const hint = String(semantic.limitHint ?? '').trim()
      questions.push(hint || defaults.limit)
    }
    return { missingSlots, questions }
  }
  return null
}

export async function resolveRequestedLimit(task: string, config: AgentConfig) {
  const fromStructural = parseTaskLimitStructural(task)
  if (fromStructural != null && fromStructural > 0) return fromStructural
  const semantic = await inferCrawlerSlotsByLlm(task, config)
  const v = Number(semantic?.limitValue ?? NaN)
  if (Number.isFinite(v) && v > 0) return Math.min(250, Math.floor(v))
  return null
}

async function inferStructuredTaskPlanByLlm(
  task: string,
  config: AgentConfig,
  inject?: string,
): Promise<StructuredTaskPlan | null> {
  const model = createQwenChatModel(config)
  if (!model) return null
  try {
    const prompt = buildStructuredTaskPlanPrompt(task, inject)
    const res = await model.invoke([['human', prompt]])
    const parsed = safeJsonParse(String(res.content ?? '').trim())
    const safe = StructuredTaskPlanSchema.safeParse(parsed)
    if (!safe.success) return null
    return safe.data
  } catch {
    return null
  }
}

export async function buildStructuredTaskPlan(
  task: string,
  config: AgentConfig,
  inject?: Pick<CrawlInjectBlocks, 'plan' | 'experience'>,
): Promise<StructuredTaskPlan> {
  const h = buildHeuristicStructuredTaskPlan(task)
  const structural = inferStructuralTaskPlan(task)
  const planInject = [inject?.experience, inject?.plan].filter(Boolean).join('\n')
  const l = await inferStructuredTaskPlanByLlm(task, config, planInject || undefined)
  if (!l) return mergeStructuralIntoTaskPlan(h, structural)
  const conf = Number(l.confidence ?? 0)
  if (conf < 0.45) return mergeStructuralIntoTaskPlan(h, structural)
  const limitRaw = Number.isFinite(Number(l.limit)) ? Number(l.limit) : h.limit
  const limit =
    limitRaw != null && Number.isFinite(limitRaw) && limitRaw > 0
      ? limitRaw
      : l.openWebSearch
        ? 12
        : h.limit
  const merged: StructuredTaskPlan = {
    targetSite: l.targetSite || h.targetSite,
    contentType: l.contentType || h.contentType,
    limit,
    fields: (() => {
      const fromLlm =
        Array.isArray(l.fields) && l.fields.length
          ? l.fields.map((x) => String(x)).filter(Boolean).slice(0, 12)
          : h.fields
      if (!l.openWebSearch) return fromLlm
      const mergedFields = [
        ...new Set([...fromLlm, 'title', 'url', 'excerpt', 'source'].map((x) => String(x).trim()).filter(Boolean)),
      ]
      return mergedFields.slice(0, 12)
    })(),
    filters: Array.isArray(l.filters) ? l.filters.map((x) => String(x)).filter(Boolean).slice(0, 12) : h.filters,
    sortBy: String(l.sortBy ?? '').trim() || h.sortBy,
    sortOrder: l.sortOrder ?? h.sortOrder,
    timeRange: l.timeRange || h.timeRange,
    outputSpec: l.outputSpec || h.outputSpec,
    qualityTarget: l.qualityTarget || h.qualityTarget,
    needsAuth: Boolean(l.needsAuth ?? h.needsAuth),
    confidence: conf,
    openWebSearch: Boolean(l.openWebSearch),
  }
  return mergeStructuralIntoTaskPlan(merged, structural)
}

const TaskConflictSchema = z.object({
  issues: z.array(z.string()).default([]),
  questions: z.array(z.string()).default([]),
  confidence: z.number().min(0).max(1).optional(),
})

export async function detectTaskConflictsByLlm(
  task: string,
  taskPlan: StructuredTaskPlan,
  config: AgentConfig,
): Promise<{ issues: string[]; questions: string[] }> {
  const model = createQwenChatModel(config)
  if (!model) return { issues: [], questions: [] }
  try {
    const res = await model.invoke([
      [
        'system',
        [
          '你是抓取任务冲突检测器。判断用户任务与结构化计划是否矛盾，只输出 JSON。',
          'issues 示例：multi_source_conflict、sort_conflict、limit_too_large、time_range_invalid。',
          'questions 为需要用户澄清的中文问句（0-3 条）。',
          '无冲突时 issues/questions 为空数组。',
          'schema: {"issues":string[],"questions":string[],"confidence":number}',
        ].join('\n'),
      ],
      ['human', `用户任务：${String(task ?? '').trim()}\n\n结构化计划：${JSON.stringify(taskPlan)}`],
    ])
    const parsed = safeJsonParse(String(res.content ?? '').trim())
    const safe = TaskConflictSchema.safeParse(parsed)
    if (!safe.success || Number(safe.data.confidence ?? 0) < 0.5) return { issues: [], questions: [] }
    return {
      issues: safe.data.issues.map((x) => String(x)).filter(Boolean).slice(0, 6),
      questions: safe.data.questions.map((x) => String(x)).filter(Boolean).slice(0, 4),
    }
  } catch {
    return { issues: [], questions: [] }
  }
}

export async function plannerWithLlm(
  task: string,
  model: ChatOpenAI,
  _cfg?: AgentConfig,
  inject?: string,
  taskPlan?: StructuredTaskPlan | null,
) {
  const prompt = ChatPromptTemplate.fromTemplate(buildSeedCrawlPlanTemplate(inject))
  const chain = RunnableSequence.from([prompt, model, new StringOutputParser()])
  try {
    const text = await chain.invoke({ task })
    const raw = safeJsonParse(String(text ?? '').trim())
    const parsed = PlanSchema.safeParse(raw)
    if (parsed.success) {
      const plan = parsed.data
      plan.seedUrls = plan.seedUrls.map((u) => canonicalizeSeedUrl(task, u, taskPlan))
      return plan
    }
  } catch {}
  return null
}
