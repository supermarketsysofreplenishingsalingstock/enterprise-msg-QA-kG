/**
 * 爬取结果归档：每次任务运行把结果 JSON 存入 .data/runs/<id>.json，
 * 并维护 .data/runs/index.json 摘要索引（新在前，最多保留 50 条）。
 * 供前端「历史爬取」面板回看之前的爬取记录。
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync, readdirSync, statSync, unlinkSync } from 'node:fs'
import { join } from 'node:path'

const MAX_ARCHIVED_RUNS = 50

export type RunSummary = {
  id: string
  task: string
  startedAt: string
  finishedAt: string
  status: string
  attempts?: number
  pages: number
  items: number
  attachments: number
  images: number
  kbPages: number
  seedHost?: string
  seedCount?: number
}

function runsDir() {
  const dir = join(process.cwd(), '.data', 'runs')
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
  return dir
}

function indexPath() {
  return join(runsDir(), 'index.json')
}

function safeId(id: string) {
  return String(id ?? '').trim().replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 80)
}

function readIndex(): RunSummary[] {
  try {
    const raw = readFileSync(indexPath(), 'utf8')
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? arr : []
  } catch {
    return []
  }
}

function writeIndex(list: RunSummary[]) {
  try {
    writeFileSync(indexPath(), JSON.stringify(list, null, 2), 'utf8')
  } catch {}
}

function countOf(v: any): number {
  return Array.isArray(v) ? v.length : 0
}

function firstHostOf(result: any): string {
  try {
    const seeds = result?.taskPlan?.seeds ?? result?.plan?.seeds
    if (Array.isArray(seeds) && seeds.length) {
      const first = typeof seeds[0] === 'string' ? seeds[0] : seeds[0]?.url
      const m = /https?:\/\/([^/\s]+)/i.exec(String(first ?? ''))
      if (m) return m[1]
    }
  } catch {}
  return ''
}

function seedCountOf(result: any): number {
  try {
    const seeds = result?.taskPlan?.seeds ?? result?.plan?.seeds
    if (Array.isArray(seeds)) return seeds.length
  } catch {}
  return 0
}

export function makeRunId(startedAt?: string) {
  const ts = Date.now()
  const rand = Math.random().toString(36).slice(2, 8)
  const stamp = String(startedAt ?? '').replace(/[^0-9]/g, '').slice(0, 14)
  return `run_${stamp || ts}_${rand}`
}

export function summarizeRun(id: string, result: any): RunSummary {
  return {
    id,
    task: String(result?.task ?? '').slice(0, 160),
    startedAt: String(result?.startedAt ?? ''),
    finishedAt: String(result?.finishedAt ?? ''),
    status: String(result?.status ?? ''),
    attempts: Number(result?.attempts ?? 0) || undefined,
    pages: countOf(result?.kbPages),
    items: countOf(result?.items),
    attachments: countOf(result?.attachments),
    images: countOf(result?.images),
    kbPages: countOf(result?.kbPages),
    seedHost: firstHostOf(result) || undefined,
    seedCount: seedCountOf(result) || undefined,
  }
}

/** 任务结束时归档一次运行结果（失败不影响主流程） */
export async function archiveRunResult(result: any, explicitId?: string): Promise<RunSummary | null> {
  try {
    if (!result || typeof result !== 'object') return null
    const id = safeId(explicitId ?? makeRunId(result?.startedAt))
    if (!id) return null
    writeFileSync(join(runsDir(), `${id}.json`), JSON.stringify({ ...result, runId: id }, null, 2), 'utf8')
    const list = readIndex().filter((e) => e && e.id !== id)
    list.unshift(summarizeRun(id, result))
    // 超出上限时清理最旧的归档文件
    while (list.length > MAX_ARCHIVED_RUNS) {
      const dropped = list.pop()
      if (dropped?.id) {
        try {
          unlinkSync(join(runsDir(), `${dropped.id}.json`))
        } catch {}
      }
    }
    writeIndex(list)
    return list[0] ?? null
  } catch {
    return null
  }
}

/** 摘要列表（新在前）；索引缺失时从目录兜底重建 */
export function listRunArchive(): RunSummary[] {
  let list = readIndex()
  if (!list.length) {
    try {
      const dir = runsDir()
      const files = readdirSync(dir)
        .filter((f) => /^run_[a-zA-Z0-9_-]+\.json$/.test(f) && f !== 'index.json')
        .map((f) => ({ f, m: statSync(join(dir, f)).mtimeMs }))
        .sort((a, b) => b.m - a.m)
        .slice(0, MAX_ARCHIVED_RUNS)
      for (const { f } of files) {
        try {
          const data = JSON.parse(readFileSync(join(dir, f), 'utf8'))
          list.push(summarizeRun(f.replace(/\.json$/, ''), data))
        } catch {}
      }
    } catch {}
  }
  return list
}

/** 读取单次运行的完整归档结果 */
export function readRunArchive(id: string): any | null {
  const key = safeId(id)
  if (!key || key === 'index') return null
  try {
    return JSON.parse(readFileSync(join(runsDir(), `${key}.json`), 'utf8'))
  } catch {
    return null
  }
}

/**
 * 把存量 crawler_results.json 导入归档（让「已有的爬取内容」也能在历史面板看）。
 * 用文件 mtime 做幂等标记：未变化则跳过；变化时先与索引比对 startedAt，
 * 避免与 workflow 的正常运行归档重复。
 */
export async function importLegacyResult(): Promise<RunSummary | null> {
  try {
    const src = join(process.cwd(), 'crawler_results.json')
    if (!existsSync(src)) return null
    const mtime = statSync(src).mtimeMs
    const markerFile = join(runsDir(), 'legacy_import_marker.json')
    let marker: { mtimeMs?: number } = {}
    try {
      marker = JSON.parse(readFileSync(markerFile, 'utf8'))
    } catch {}
    if (marker.mtimeMs === mtime) return null

    const data = JSON.parse(readFileSync(src, 'utf8'))
    if (!data || typeof data !== 'object') return null
    const startedAt = String(data.startedAt ?? '')
    // 同 startedAt 的归档已存在（本次运行刚被 workflow 正常归档），只补标记不重复归档
    const dup = readIndex().some((e) => e && startedAt && e.startedAt === startedAt)
    let summary: RunSummary | null = null
    if (!dup) {
      summary = await archiveRunResult(data, `run_legacy_${Math.round(mtime)}`)
    }
    try {
      writeFileSync(markerFile, JSON.stringify({ mtimeMs: mtime, at: new Date().toISOString() }), 'utf8')
    } catch {}
    return summary
  } catch {
    return null
  }
}
