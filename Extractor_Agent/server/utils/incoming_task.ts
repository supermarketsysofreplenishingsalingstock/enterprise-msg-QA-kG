/**
 * 入站采集任务清洗：兼容总管/规划器口吻，还原为可独立执行的抓取任务。
 */

import type { CrawlerAgentOptions } from '../services/crawlerAgentTypes'
import { getExtractorAgentEnv } from './extractor_agent_env'

const CRAWLER_PREFIXES = [
  '从互联网抓取相关数据：',
  '从互联网抓取相关数据:',
  '从互联网抓取相关信息：',
  '从互联网抓取相关信息:',
  '从互联网抓取相关内容：',
  '从互联网抓取相关内容:',
  '从互联网抓取：',
  '从互联网抓取:',
  '从网页抓取：',
  '从网页抓取:',
  '抓取互联网：',
  '抓取互联网:',
  '爬取互联网：',
  '爬取互联网:',
  '联网抓取：',
  '联网抓取:',
  '联网爬取：',
  '联网爬取:',
  '进行开放式发现搜索：',
  '进行开放式发现搜索:',
] as const

const PLANNER_BLOCK_TAGS = ['约束', '上下文', '上游', '步骤', '总管'] as const

const AGENT_LINE_PREFIXES = ['rag:', 'rag：', 'db:', 'db：', 'crawler:', 'crawler：', 'admin:', 'admin：'] as const

function stripCrawlerPrefix(q: string): string {
  let s = q.trim()
  for (const p of CRAWLER_PREFIXES) {
    if (s.startsWith(p)) {
      s = s.slice(p.length).trim()
      break
    }
  }
  return s
}

function stripTrailingReport(q: string): string {
  let s = q.trim()
  if (s.endsWith('，并生成报告')) s = s.slice(0, -'，并生成报告'.length).trim()
  else if (s.endsWith(',并生成报告')) s = s.slice(0, -',并生成报告'.length).trim()
  else if (s.endsWith('并生成报告')) s = s.slice(0, -'并生成报告'.length).replace(/[，,]\s*$/, '').trim()
  return s
}

function stripPlannerBlock(q: string): string {
  let cut = q.length
  for (const tag of PLANNER_BLOCK_TAGS) {
    for (const lead of ['\n\n[', '\n[']) {
      const marker = `${lead}${tag}`
      const i = q.indexOf(marker)
      if (i >= 0 && i < cut) cut = i
    }
  }
  return cut < q.length ? q.slice(0, cut).trim() : q
}

export type ManagerTaskHints = {
  source?: string
  refined_task?: string
  hint_fields?: string[]
  preferred_channel?: 'http' | 'browser' | 'mcp'
  must_filters?: string[]
  open_web_discovery?: boolean
  seed_urls?: string[]
  serp_context?: string
}

export function sanitizeIncomingTask(raw: string): string {
  const max = getExtractorAgentEnv().taskMaxChars
  let t = String(raw ?? '').replace(/\r\n/g, '\n').trim()
  if (!t) return ''

  t = stripPlannerBlock(t)
  t = stripCrawlerPrefix(t)
  t = stripTrailingReport(t)

  const lines = t
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l && !AGENT_LINE_PREFIXES.some((p) => l.toLowerCase().startsWith(p.toLowerCase())))
  t = lines.join('\n').trim() || String(raw ?? '').trim()

  t = t.replace(/\s+/g, ' ').trim()
  return t.slice(0, max) || String(raw ?? '').trim().slice(0, max)
}

export function parseManagerTaskJson(raw?: string | Record<string, unknown> | null): ManagerTaskHints | null {
  let obj: Record<string, unknown> | null = null
  if (raw && typeof raw === 'object') obj = raw
  else if (typeof raw === 'string' && raw.trim()) {
    try {
      obj = JSON.parse(raw.trim()) as Record<string, unknown>
    } catch {
      return null
    }
  }
  if (!obj || typeof obj !== 'object') return null

  const refined = String(obj.refined_task ?? obj.refined_question ?? obj.query ?? '').trim()
  const hintFields = Array.isArray(obj.hint_fields)
    ? obj.hint_fields.map((x) => String(x ?? '').trim()).filter(Boolean).slice(0, 12)
    : []
  const channel = String(obj.preferred_channel ?? '').trim().toLowerCase()
  const preferred_channel =
    channel === 'http' || channel === 'browser' || channel === 'mcp' ? (channel as ManagerTaskHints['preferred_channel']) : undefined
  const must_filters = Array.isArray(obj.must_filters)
    ? obj.must_filters.map((x) => String(x ?? '').trim()).filter(Boolean).slice(0, 8)
    : undefined
  const seed_urls = Array.isArray(obj.seed_urls)
    ? obj.seed_urls.map((x) => String(x ?? '').trim()).filter((u) => u.startsWith('http://') || u.startsWith('https://')).slice(0, 12)
    : undefined
  const serp_context = String(obj.serp_context ?? '').trim().slice(0, 2400) || undefined
  const source = String(obj.source ?? '').trim() || undefined
  const open_web_discovery =
    obj.open_web_discovery === true ||
    (String(obj.source ?? '') === 'manager' &&
      !seed_urls?.length &&
      hintFields.some((f) => f === 'excerpt' || f === 'source'))

  if (!refined && !hintFields.length && !preferred_channel && !seed_urls?.length && !open_web_discovery) return null
  return {
    ...(source ? { source } : {}),
    ...(refined ? { refined_task: refined } : {}),
    ...(hintFields.length ? { hint_fields: hintFields } : {}),
    ...(preferred_channel ? { preferred_channel } : {}),
    ...(must_filters?.length ? { must_filters } : {}),
    ...(open_web_discovery ? { open_web_discovery: true } : {}),
    ...(seed_urls?.length ? { seed_urls } : {}),
    ...(serp_context ? { serp_context } : {})
  }
}

export function applyManagerTaskHints(
  task: string,
  managerTask?: string | Record<string, unknown> | null,
): { task: string; options: Partial<CrawlerAgentOptions> } {
  const hints = parseManagerTaskJson(managerTask)
  if (!hints) return { task, options: {} }

  const nextTask = hints.refined_task ? sanitizeIncomingTask(hints.refined_task) : task
  const options: Partial<CrawlerAgentOptions> = {}
  if (hints.preferred_channel === 'browser') options.useBrowser = true
  if (hints.preferred_channel === 'mcp') (options as any).__preferMcp = true
  if (hints.preferred_channel) (options as any).preferred_channel = hints.preferred_channel
  if (hints.hint_fields?.length) {
    ;(options as any).hint_fields = hints.hint_fields
  }
  if (hints.must_filters?.length) {
    ;(options as any).must_filters = hints.must_filters
  }
  if (hints.seed_urls?.length) {
    ;(options as any).__managerSeedUrls = hints.seed_urls
    ;(options as any).__seedFirstMode = true
    ;(options as any).__openWebDiscovery = false
    ;(options as any).maxPages = Math.max(Number((options as any).maxPages ?? 1), Math.min(10, hints.seed_urls.length))
    if (!hints.preferred_channel) {
      ;(options as any).preferred_channel = 'mcp'
      ;(options as any).__preferMcp = true
    }
  }
  if (hints.serp_context) {
    ;(options as any).__serpContext = hints.serp_context
  }
  if (hints.source === 'manager') {
    ;(options as any).__fromManager = true
  }
  if (hints.open_web_discovery && !hints.seed_urls?.length) {
    ;(options as any).__openWebDiscovery = true
  }
  return { task: nextTask || task, options }
}
