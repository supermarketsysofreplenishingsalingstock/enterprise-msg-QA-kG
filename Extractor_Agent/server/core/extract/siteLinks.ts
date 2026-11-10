/**
 * 整站爬取：同站链接发现。
 *
 * 只收集 http(s) 的 <a href>，默认限定在种子主机名内；
 * 去掉静态资源、登录/登出、条款页等噪音，并做 URL 规范化（去 hash、去追踪参数）后去重。
 */
import { load } from 'cheerio'

/** 静态资源 / 直接下载类后缀，不作为“页面”入队 */
const ASSET_EXT =
  /\.(?:png|jpe?g|gif|svg|webp|avif|ico|bmp|css|js|mjs|cjs|map|json|xml|rss|pdf|zip|rar|7z|tar|gz|tgz|bz2|xz|mp3|mp4|m4a|avi|mov|wmv|flv|webm|wav|ogg|oga|docx?|xlsx?|pptx?|txt|csv|woff2?|ttf|otf|eot|apk|exe|dmg|iso|bin|dat)$/i

/** 无正文价值的路径 */
const SKIP_PATH =
  /\/(?:login|logout|signin|signup|register|auth|oauth|cart|checkout|payment|privacy|terms|tos|cookie|help|support|feedback)(?:\/|$)/i

/** 常见追踪参数 */
const TRACKING_PARAMS = /^(?:utm_.*|fbclid|gclid|yclid|msclkid|spm|ref|ref_src|share_token)$/i

export type SiteLinkOptions = {
  /** 单页最多收集多少条（默认 80） */
  max?: number
  /** 允许的主机名（默认仅 baseUrl 的主机） */
  allowedHostnames?: string[]
  /** 额外指定来源主机 */
  sourceHost?: string
  /** 不做主机过滤（用于从种子页发现“站点家族”） */
  allowAnyHost?: boolean
}

/** 常见社交/搜索/代码托管站：即使出现在首页也不当作“本站” */
const EXTERNAL_SKIP_HOST =
  /(?:^|\.)(?:google|googleapis|gstatic|baidu|bing|facebook|twitter|x|instagram|youtube|youtu|linkedin|github|gitlab|gitee|weibo|zhihu|bilibili|douyin|tiktok|qq|wechat|whatsapp|telegram|reddit|pinterest|tumblr)\./i

function resolveLink(raw: string, baseUrl: string): URL | null {
  try {
    const u = new URL(raw, baseUrl)
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null
    u.hash = ''
    for (const key of Array.from(u.searchParams.keys())) {
      if (TRACKING_PARAMS.test(key)) u.searchParams.delete(key)
    }
    let s = u.toString()
    if (!u.search && s.endsWith('?')) s = s.slice(0, -1)
    return new URL(s)
  } catch {
    return null
  }
}

/** 从 HTML 中抽取同站页面链接（已去重、已规范化） */
export function extractSiteLinks(html: string, baseUrl: string, opts: SiteLinkOptions = {}): string[] {
  const max = Math.max(1, Math.min(500, Math.floor(Number(opts.max ?? 80) || 80)))
  const base = (() => {
    try {
      return new URL(baseUrl)
    } catch {
      return null
    }
  })()
  if (!base) return []

  const sourceHost = String(opts.sourceHost || base.hostname || '').toLowerCase()
  const allowed = new Set(
    (opts.allowedHostnames?.length ? opts.allowedHostnames : [sourceHost])
      .map((h) => String(h ?? '').trim().toLowerCase())
      .filter(Boolean),
  )
  if (allowed.size === 0) return []

  const $ = load(html)
  $('script, style, noscript, svg, iframe').remove()

  const current = base.toString().replace(/#.*$/, '')
  const seen = new Set<string>()
  const out: string[] = []

  $('a[href]').each((_, el) => {
    if (out.length >= max) return false
    const hrefRaw = String($(el).attr('href') ?? '').trim()
    if (!hrefRaw) return
    if (/^(?:javascript:|mailto:|tel:|data:|sms:|ftp:|blob:)/i.test(hrefRaw)) return
    if (hrefRaw.startsWith('#')) return

    const u = resolveLink(hrefRaw, base.toString())
    if (!u) return
    const hostname = u.hostname.toLowerCase()
    if (!opts.allowAnyHost) {
      if (!allowed.has(hostname)) return
    } else if (EXTERNAL_SKIP_HOST.test(hostname)) {
      return
    }
    if (u.pathname === '/robots.txt' || u.pathname === '/sitemap.xml') return
    if (ASSET_EXT.test(u.pathname)) return
    if (SKIP_PATH.test(u.pathname)) return

    const key = u.toString()
    if (key === current || seen.has(key)) return
    seen.add(key)
    out.push(key)
  })

  return out
}

/**
 * 链接入队排序：相关度优先（公司信息类页面排前面），浅层、短路径优先做次级排序，
 * 让「分层精选爬 N 页」在预算内优先抓到公司简介/产品/资质/动态等高价值页面。
 */
const RELEVANT_PATH =
  /(?:about|intro|profile|company|corp|product|service|solution|business|news|info|dynamic|develop|qualif|honor|honour|certif|case|culture|value|team|partner|contact|join|recruit|career|brief|vision|mission|history|背景|简介|概况|关于|产品|服务|方案|业务|新闻|动态|资讯|资质|荣誉|奖项|认证|案例|文化|价值观|理念|愿景|使命|团队|伙伴|合作|联系|招聘|加入|发展|历程)/i

/** 低价值路径（仅降权不丢弃：标签/分页/搜索等列表噪声） */
const LOW_VALUE_PATH = /(?:tag|tags|label|category|page\/\d+|\?page=|\/node\/\d+|feed|rss|sitemap|login|register|search)/i

export function rankSiteLinks(links: string[]): string[] {
  const score = (u: string) => {
    try {
      const url = new URL(u)
      const parts = url.pathname.split('/').filter(Boolean)
      // 相关度：命中关键词强加分（放最前），低价值路径降分
      const path = `${url.pathname}${url.search}`
      let s = 0
      if (RELEVANT_PATH.test(path)) s -= 10_000
      if (LOW_VALUE_PATH.test(path)) s += 8_000
      // 次级：浅层、短路径优先，覆盖面更均衡
      return s + parts.length * 1000 + parts.join('/').length
    } catch {
      return 1_000_000
    }
  }
  return [...links].sort((a: string, b: string) => score(a) - score(b))
}

/** ── 公司爬取：页面上挂载的文件（附件）收集 ──
 * 公司官网常见挂载文件：pdf / doc(x) / xls(x) / ppt(x) / zip / txt / csv 等。
 */
const ATTACH_EXT =
  /\.(?:pdf|zip|rar|7z|docx?|xlsx?|pptx?|txt|csv|md|wps|et|dps|ofd)$/i

export type AttachmentLink = { url: string; filename: string; ext: string }

/** 从 HTML 中收集挂载文件链接（含 <a href> 与 <iframe src>，去重、规范化） */
export function extractAttachmentLinks(html: string, baseUrl: string, opts: { max?: number } = {}): AttachmentLink[] {
  const max = Math.max(1, Math.min(50, Math.floor(Number(opts.max ?? 12) || 12)))
  const base = (() => {
    try {
      return new URL(baseUrl)
    } catch {
      return null
    }
  })()
  if (!base || !html) return []

  const $ = load(html)
  const seen = new Set<string>()
  const out: AttachmentLink[] = []

  const push = (raw: string, hintTitle?: string) => {
    if (out.length >= max) return
    const hrefRaw = String(raw ?? '').trim()
    if (!hrefRaw || /^(?:javascript:|mailto:|tel:|data:|sms:|ftp:|blob:)/i.test(hrefRaw)) return
    const u = resolveLink(hrefRaw, base.toString())
    if (!u) return
    const pathname = u.pathname
    if (!ATTACH_EXT.test(pathname)) return
    const key = u.toString()
    if (seen.has(key)) return
    seen.add(key)
    const m = pathname.match(/([^/]+)$/)
    const filename = decodeURIComponent((m?.[1] ?? 'attachment').slice(0, 120))
    out.push({ url: key, filename, ext: (pathname.match(ATTACH_EXT)?.[1] ?? '').toLowerCase() })
    void hintTitle
  }

  $('a[href]').each((_, el) => {
    const label = String($(el).attr('title') ?? $(el).text() ?? '').trim().slice(0, 80)
    push(String($(el).attr('href') ?? ''), label)
  })
  $('iframe[src]').each((_, el) => {
    push(String($(el).attr('src') ?? ''))
  })

  return out
}
