/**
 * 用 Manager 下发的 SERP 摘要补全抓取 items 的空 excerpt。
 */

function normalizeUrlKey(url: string): string {
  try {
    const u = new URL(String(url ?? '').trim())
    u.hash = ''
    return u.toString().replace(/\/+$/, '')
  } catch {
    return String(url ?? '').trim().replace(/\/+$/, '')
  }
}

export function parseSerpContextByUrl(serpContext: string): Map<string, string> {
  const map = new Map<string, string>()
  const ctx = String(serpContext ?? '').trim()
  if (!ctx) return map

  for (const block of ctx.split(/\n(?=\d+\.\s)/)) {
    const urlM = block.match(/URL:\s*(https?:\/\/\S+)/i)
    const url = urlM ? urlM[1]!.replace(/[)\],.]+$/, '') : ''
    if (!url) continue
    const snip = block
      .replace(/^\d+\.\s*.+\n?/i, '')
      .replace(/URL:.+/i, '')
      .trim()
      .slice(0, 400)
    if (snip) map.set(normalizeUrlKey(url), snip)
  }
  return map
}

export function enrichItemsFromSerpContext<T extends Record<string, unknown>>(
  items: T[],
  serpContext?: string
): T[] {
  const ctx = String(serpContext ?? '').trim()
  if (!ctx || !items.length) return items
  const byUrl = parseSerpContextByUrl(ctx)
  if (!byUrl.size) return items

  return items.map((item) => {
    const url = String(item.url ?? '').trim()
    if (!url) return item
    const excerpt = String(item.excerpt ?? item.summary ?? '').trim()
    if (excerpt.length >= 12) return item
    const fromSerp = byUrl.get(normalizeUrlKey(url))
    if (!fromSerp) return item
    return { ...item, excerpt: fromSerp, excerpt_source: 'manager_serp' }
  })
}
