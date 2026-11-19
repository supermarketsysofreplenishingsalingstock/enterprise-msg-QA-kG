/**
 * 断点续爬 checkpoint（.data/checkpoints/<主种子host>.json）：
 * - 每批页面爬完即把「已爬 URL + 待爬队列/深度 + 页面正文 + 图片/附件」原子落盘
 * - 中途中断 / 等待人工验证时被杀 / 被网站封 / dev server 重启，都不丢已爬内容
 * - 下次爬取同站点（主种子 host 相同）自动载入：跳过已爬 URL，从上次队列接着往后爬，
 *   并把历史内容合并进本次结果一起推送知识库；推送成功后自动清除断点
 * - options.freshCrawl === true 可忽略并清除断点，强制从头爬
 */
import { existsSync, mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

export type CrawlCheckpointData = {
  task?: string
  company?: string
  crawledUrls?: string[]
  queue?: string[]
  depth?: Record<string, number>
  kbPages?: { url?: string; title?: string; text?: string }[]
  images?: any[]
  attachments?: any[]
  updatedAt?: string
}

const DEBOUNCE_MS = 2000
const lastSavedAt = new Map<string, number>()

function ckDir() {
  return join(process.cwd(), '.data', 'checkpoints')
}

function ckPath(key: string) {
  return join(ckDir(), `${safeKey(key)}.json`)
}

function safeKey(key: string) {
  return String(key ?? '')
    .trim()
    .replace(/[^a-zA-Z0-9._-]/g, '_')
    .slice(0, 80)
}

/** 断点键 = 主种子（第一个种子 URL）的 hostname（去 www. 前缀）；无种子返回 ''（禁用断点） */
export function checkpointKeyFromSeeds(seedUrls: unknown): string {
  const arr = Array.isArray(seedUrls) ? seedUrls : []
  for (const s of arr) {
    try {
      const h = new URL(String(s)).hostname.toLowerCase().replace(/^www\./, '')
      if (h) return h
    } catch {}
  }
  return ''
}

/** 读取断点；图片/附件先过滤掉磁盘上已不存在的文件（对应 URL 允许下次重新下载） */
export function loadCrawlCheckpoint(key: string): CrawlCheckpointData | null {
  try {
    if (!key) return null
    const fp = ckPath(key)
    if (!existsSync(fp)) return null
    const data = JSON.parse(readFileSync(fp, 'utf8'))
    if (!data || typeof data !== 'object') return null
    data.images = (Array.isArray(data.images) ? data.images : []).filter(
      (im: any) => im?.path && existsSync(String(im.path)),
    )
    data.attachments = (Array.isArray(data.attachments) ? data.attachments : []).filter(
      (f: any) => f?.path && existsSync(String(f.path)),
    )
    return data as CrawlCheckpointData
  } catch {
    return null
  }
}

/**
 * 保存断点（原子写：tmp + rename；2s 防抖）。
 * 图片/附件落盘前先剔除磁盘上已不存在的文件；队列里已爬过的 URL 剔除。
 */
export function saveCrawlCheckpoint(key: string, data: CrawlCheckpointData, force = false) {
  try {
    if (!key) return
    const now = Date.now()
    if (!force && now - (lastSavedAt.get(key) ?? 0) < DEBOUNCE_MS) return
    lastSavedAt.set(key, now)

    const crawled = new Set((Array.isArray(data.crawledUrls) ? data.crawledUrls : []).map(String).filter(Boolean))
    const payload = {
      ...data,
      crawledUrls: Array.from(crawled).slice(0, 5000),
      queue: (Array.isArray(data.queue) ? data.queue : [])
        .map(String)
        .filter((u) => u && !crawled.has(u))
        .slice(0, 5000),
      images: (Array.isArray(data.images) ? data.images : []).filter(
        (im: any) => im?.path && existsSync(String(im.path)),
      ),
      attachments: (Array.isArray(data.attachments) ? data.attachments : []).filter(
        (f: any) => f?.path && existsSync(String(f.path)),
      ),
      updatedAt: new Date().toISOString(),
    }
    const dir = ckDir()
    if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
    const fp = ckPath(key)
    const tmp = `${fp}.${process.pid}.${Math.random().toString(36).slice(2, 8)}.tmp`
    writeFileSync(tmp, JSON.stringify(payload), 'utf8')
    renameSync(tmp, fp)
  } catch {}
}

/** 清除断点（推送知识库成功后调用；下次爬取从零开始） */
export function clearCrawlCheckpoint(key: string) {
  try {
    if (!key) return
    const fp = ckPath(key)
    if (existsSync(fp)) unlinkSync(fp)
    lastSavedAt.delete(key)
  } catch {}
}
