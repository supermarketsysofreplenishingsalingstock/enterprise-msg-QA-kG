/**
 * 公司爬取：附件（页面上挂载的文件）下载 + 页面正文提取。
 *
 * - downloadAttachment：二进制下载（fetchWithRetry → arrayBuffer → 落盘 .data/attachments/<run>/）
 * - extractPageText：HTML → 正文文本（供知识库 ingest 用）
 */
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { load } from 'cheerio'
import { fetchWithRetry } from './runtime'

export type AttachmentFile = {
  url: string
  path: string
  filename: string
  ext: string
  contentType: string
  size: number
  ok: boolean
  error?: string
}

function sanitizeFilename(name: string, ext: string, idx: number): string {
  let base = String(name ?? '').trim() || `file_${idx}`
  base = base.replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 80)
  if (!path.extname(base)) base = `${base}.${ext || 'bin'}`
  return base
}

/** 下载单个附件到 destDir，返回元信息（失败不抛出，返回 ok:false） */
export async function downloadAttachment(
  url: string,
  config: any,
  signal: AbortSignal,
  destDir: string,
  filename: string,
  ext: string,
  idx: number,
): Promise<AttachmentFile> {
  const ua = String(
    config?.crawler?.userAgent ?? 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
  ).trim()
  let origin = ''
  try {
    origin = new URL(url).origin
  } catch {}
  const headers: Record<string, string> = {
    'user-agent': ua,
    accept: '*/*',
    'accept-language': 'zh-CN,zh;q=0.9,en;q=0.7',
    ...(origin ? { referer: `${origin}/` } : {}),
  }
  try {
    await mkdir(destDir, { recursive: true })
    const res = await fetchWithRetry(
      url,
      { headers, redirect: 'follow' },
      signal,
      { timeoutMs: 30_000, maxAttempts: 2, baseBackoffMs: 800 },
    )
    if (!(res as any).ok) {
      throw new Error(`HTTP ${(res as any).status} ${(res as any).statusText}`)
    }
    const contentType = String((res as any).headers?.get?.('content-type') ?? '')
    const buf = Buffer.from(await (res as any).arrayBuffer())
    if (buf.byteLength < 64) throw new Error('文件过小，疑似错误响应')
    const safe = sanitizeFilename(filename, ext, idx)
    const filePath = path.join(destDir, `${String(idx).padStart(3, '0')}_${safe}`)
    await writeFile(filePath, buf)
    return {
      url,
      path: filePath,
      filename: safe,
      ext: ext || path.extname(safe).replace('.', ''),
      contentType,
      size: buf.byteLength,
      ok: true,
    }
  } catch (e: any) {
    return {
      url,
      path: '',
      filename,
      ext,
      contentType: '',
      size: 0,
      ok: false,
      error: String(e?.message ?? e).slice(0, 160),
    }
  }
}

const BAD_TEXT_TAGS = 'script, style, noscript, svg, iframe, nav, footer, header, aside, form'

/** HTML → 正文文本（知识库 ingest 用） */
export function extractPageText(html: string, maxChars = 8000): string {
  if (!html) return ''
  try {
    const $ = load(html)
    $(BAD_TEXT_TAGS).remove()
    const title = String($('title').first().text() ?? '').trim()
    const text = String($('body').text() ?? '')
      .replace(/[\t\r]+/g, ' ')
      .replace(/\n{3,}/g, '\n\n')
      .replace(/[ ]{2,}/g, ' ')
      .trim()
    return (title ? `【页面标题】${title}\n\n` : '') + text.slice(0, maxChars)
  } catch {
    return ''
  }
}
