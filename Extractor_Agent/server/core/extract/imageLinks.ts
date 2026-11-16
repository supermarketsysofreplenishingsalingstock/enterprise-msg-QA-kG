/**
 * 页面图片链接提取：给「爬取图片」用（仿公司爬取的挂载文件下载链路）。
 *
 * - extractImageLinks(html, baseUrl, { max, allowedHostnames })
 *   收集 <img src / data-src / data-original> 与 <source srcset>，解析为绝对 URL；
 *   过滤 data: URI、SVG、javascript:、明显图标（文件名含 logo/icon/sprite/favicon/
 *   qrcode/avatar 等）与懒加载占位图；默认只收 allowedHostnames 域内图片（与站内
 *   页面同域，跨域 CDN 图片在 config.allowedHostnames 覆盖时也会收）。
 */
import { load } from 'cheerio'

export type ImageLink = { url: string; title: string }

const JUNK_NAME =
  /(?:logo|icon|sprite|favicon|avatar|qrcode|qr_code|weixin|wechat|badge|placeholder|blank\.|loading|spacer|1x1|pixel)/i
const IMG_EXT = /\.(?:jpe?g|png|webp|bmp|gif)(?:[?#].*)?$/i

function absolutize(raw: string, baseUrl: string): string {
  const v = String(raw ?? '').trim()
  if (!v || /^(?:data:|javascript:|blob:)/i.test(v)) return ''
  try {
    const u = new URL(v, baseUrl)
    if (!/^https?:$/i.test(u.protocol)) return ''
    u.hash = ''
    return u.toString()
  } catch {
    return ''
  }
}

function firstSrcset(srcset: string): string {
  // "a.jpg 1x, b.jpg 2x" → 取第一段（通常是原图路径）
  return String(srcset ?? '').split(',')[0]?.trim().split(/\s+/)[0] ?? ''
}

export function extractImageLinks(
  html: string,
  baseUrl: string,
  opts: { max?: number; allowedHostnames?: string[] } = {},
): ImageLink[] {
  const max = Math.max(0, Number(opts.max ?? 12) || 12)
  const allowed = new Set(
    (opts.allowedHostnames ?? []).map((h) => h.toLowerCase().replace(/^www\./, '')),
  )
  const out: ImageLink[] = []
  const seen = new Set<string>()
  if (!html || !baseUrl) return out
  try {
    const $ = load(html)
    const push = (raw: string, title: string) => {
      if (out.length >= max) return
      const url = absolutize(raw, baseUrl)
      if (!url || seen.has(url)) return
      // 过滤：无扩展名（接口动态出图），明显图标 / 懒加载占位
      if (!IMG_EXT.test(url)) return
      const file = url.split('/').pop() ?? ''
      if (JUNK_NAME.test(file)) return
      if (allowed.size) {
        let host = ''
        try {
          host = new URL(url).hostname.toLowerCase().replace(/^www\./, '')
        } catch {
          return
        }
        // 允许精确域或其子域（allowedHostnames 含根域时）
        const ok = [...allowed].some((h) => host === h || host.endsWith(`.${h}`))
        if (!ok) return
      }
      seen.add(url)
      out.push({ url, title: String(title ?? '').trim().slice(0, 120) })
    }
    $('img').each((_i, el) => {
      const $el = $(el)
      const title = String($el.attr('alt') ?? $el.attr('title') ?? '').trim()
      const src =
        $el.attr('src') ||
        $el.attr('data-src') ||
        $el.attr('data-original') ||
        $el.attr('data-lazy-src') ||
        ''
      push(src || firstSrcset(String($el.attr('srcset') ?? '')), title)
    })
    $('source').each((_i, el) => {
      if (out.length >= max) return
      const $el = $(el)
      const parent = $el.parent()
      const inPicture = parent.is('picture') || parent.is('video')
      if (!inPicture) return
      push(firstSrcset(String($el.attr('srcset') ?? '')), String($el.attr('alt') ?? ''))
    })
  } catch {
    return out
  }
  return out
}
