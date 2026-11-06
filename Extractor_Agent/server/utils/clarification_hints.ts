/**
 * 采集澄清快捷选项：按 missing_slots 结构性映射。
 */
import { extractHttpUrl } from './url_extract'

const SLOT_CHIPS: Record<string, string[]> = {
  source: ['豆瓣电影 Top250', '知乎热榜', '微博热搜'],
  goal: ['热榜列表', '新闻列表', '商品列表', '电影榜单'],
  limit: ['前 10 条', 'Top 20', '前 50 条'],
  sort: ['按最热排序', '按最新排序'],
  format: ['JSON 输出', 'CSV 输出'],
}

export function buildCrawlerClarificationSuggestions(input: {
  reason?: string
  missingSlots?: string[]
  questions?: string[]
  task?: string
}): string[] {
  const slots = (input.missingSlots ?? []).map((s) => String(s).trim()).filter(Boolean)
  const out: string[] = []

  // 任务里已经写了地址（含裸域名/IP）时，直接把补全后的地址作为首个候选
  const detectedUrl = extractHttpUrl(input.task)

  for (const slot of slots) {
    const chips = SLOT_CHIPS[slot]
    if (!chips) continue
    if (slot === 'source' && detectedUrl) out.push(detectedUrl)
    out.push(...chips)
  }

  const seen = new Set<string>()
  const deduped: string[] = []
  for (const item of out) {
    if (seen.has(item)) continue
    seen.add(item)
    deduped.push(item)
  }
  return deduped.slice(0, 6)
}

export function attachClarifySuggestions(result: any) {
  if (!result || result.status !== 'needs_clarification') return result
  const clarify = result.clarify ?? {}
  const suggestions = buildCrawlerClarificationSuggestions({
    reason: clarify.reason,
    missingSlots: clarify.missingSlots,
    questions: clarify.questions,
    task: String(result?.task ?? ''),
  })
  return {
    ...result,
    clarify: { ...clarify, suggestions },
    meta: {
      ...(result.meta ?? {}),
      needs_clarification: true,
      clarification_suggestions: suggestions,
    },
  }
}
