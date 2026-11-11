/**
 * SPA / SSR 站点「路由发现」——整站爬取的链接兜底来源。
 *
 * 很多公司官网是 Vue/Nuxt/React 单页应用：导航是 `<div @click>` 或 Vue Router 跳转，
 * 页面上几乎没有 `<a href>`，于是 extractSiteLinks 只能拿到 0 条站内链接，
 * 「爬全站」会退化成只爬首页一页。
 *
 * 这里不启动浏览器，改用三种零成本/低成本来源补全 URL 清单：
 *   1) HTML 内联脚本里的路由表（`path:"/about"`）
 *   2) /sitemap.xml（存在即拿全站清单）
 *   3) 同源 JS bundle 里的路由表（`path:"/about"`，最多 8 个脚本）
 *
 * 全部结果都会做：主机过滤（www 归一）、静态资源过滤、动态路由过滤、去重、附加上限。
 */

const DEFAULT_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

/** 静态资源 / 附件：不作为页面路由 */
const ASSET_PATH =
  /\.(?:png|jpe?g|gif|svg|webp|avif|ico|bmp|css|js|mjs|cjs|map|json|xml|rss|pdf|zip|rar|7z|tar|gz|tgz|bz2|xz|mp3|mp4|m4a|avi|mov|wmv|flv|webm|wav|ogg|oga|docx?|xlsx?|pptx?|txt|csv|woff2?|ttf|otf|eot|apk|exe|dmg|iso|bin|dat)$/i

/** 框架内部资源目录 / 接口前缀：不是内容页 */
const INTERNAL_PREFIX =
  /^\/(?:_nuxt|_next|dist|static|assets|public|vendor|lib|js|css|api|__|cdn|images?|img|fonts?|media|uploads?)\//i

/** 无正文价值的路径 */
const SKIP_PATH =
  /\/(?:login|logout|signin|signup|register|auth|oauth|cart|checkout|payment|privacy|terms|tos|cookie|help|support|feedback)(?:\/|$)/i

/** 动态路由占位符 / 非法字符：`/article/:id`、`/a/*`、`{id}` 这类抓不了，直接丢弃 */
const DYNAMIC_ROUTE = /[:*{}()<>[\]\\\s'"`|,;]/

export type SiteRouteOptions = {
  /** 允许的主机名（与 extractSiteLinks 一致） */
  allowedHostnames?: string[]
  /** 最多返回多少条（默认 200） */
  max?: number
  /** 单个请求超时（默认 6000ms） */
  timeoutMs?: number
  /** 最多追加抓取多少个同源 JS 脚本（默认 8） */
  maxScripts?: number
  userAgent?: string
  signal?: AbortSignal
}

/** www 归一：`www.a.com` 与 `a.com` 视为同一站点 */
function hostKey(host: string): string {
  return String(host ?? '')
    .trim()
    .toLowerCase()
    .replace(/^www\./, '')
}

function hostAllowed(hostname: string, allowed: Set<string>): boolean {
  const key = hostKey(hostname)
  if (!key) return false
  if (allowed.has(key)) return true
  // 子域（m.a.com / news.a.com）在 host 模式下一并放行，避免站点换子域就断链
  for (const a of allowed) {
    if (key === a || key.endsWith(`.${a}`)) return true
  }
  return false
}

/** 统一路由候选：返回规范化后的 pathname(+search)，不合格返回 null */
function normalizeRoute(raw: string): string | null {
  const s = String(raw ?? '').trim()
  if (!s || !s.startsWith('/') || s.startsWith('//')) return null
  if (s.length > 100) return null
  if (DYNAMIC_ROUTE.test(s)) return null
  const pathOnly = s.split('#')[0].split('?')[0]
  if (!pathOnly || pathOnly === '/') return null
  // 单字母路径段（`/a/b`、`/a/i`）基本是 CSS/JS 里的图标编号，不是内容页
  const segments = pathOnly.split('/').filter(Boolean)
  if (!segments.length || segments.some((seg) => seg.length < 2)) return null
  if (ASSET_PATH.test(pathOnly)) return null
  if (INTERNAL_PREFIX.test(pathOnly)) return null
  if (SKIP_PATH.test(pathOnly)) return null
  return pathOnly
}

/** 从任意文本（内联脚本 / JS bundle）里抽取路由表条目 */
export function extractRoutePathsFromText(text: string, limit = 200): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  if (!text) return out
  const push = (raw: string) => {
    if (out.length >= limit) return
    const p = normalizeRoute(raw)
    if (!p || seen.has(p)) return
    seen.add(p)
    out.push(p)
  }
  // 1) 路由表：path:"/xxx" / path: '/xxx'
  for (const m of text.matchAll(/path\s*:\s*["'](\/[^"'\n]{0,90})["']/g)) push(m[1])
  // 2) 兜底：字符串字面量里的绝对路径（只收带 2 段以上或明显语义路径，减少误报）
  for (const m of text.matchAll(/["'](\/[a-z0-9][a-z0-9\-_/]{2,60})["']/gi)) push(m[1])
  return out
}

/** 从 sitemap.xml 里抽 <loc> */
export function extractSitemapLocs(xml: string, limit = 200): string[] {
  const out: string[] = []
  if (!xml || !/<(?:urlset|sitemapindex)/i.test(xml)) return out
  for (const m of xml.matchAll(/<loc>\s*([^<\s]+)\s*<\/loc>/gi)) {
    const u = m[1]
    if (/^https?:\/\//i.test(u)) out.push(u)
    if (out.length >= limit) break
  }
  return out
}

async function fetchText(
  url: string,
  opts: { userAgent?: string; timeoutMs?: number; signal?: AbortSignal; maxBytes?: number },
): Promise<string> {
  const timeoutMs = Math.max(1000, Math.min(20000, Number(opts.timeoutMs ?? 6000) || 6000))
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  const onAbort = () => ctrl.abort()
  opts.signal?.addEventListener('abort', onAbort)
  try {
    const res = await fetch(url, {
      signal: ctrl.signal,
      redirect: 'follow',
      headers: {
        'user-agent': opts.userAgent || DEFAULT_UA,
        accept: '*/*',
      },
    })
    if (!res.ok) return ''
    const text = await res.text()
    const max = Math.max(20_000, Number(opts.maxBytes ?? 2_000_000) || 2_000_000)
    return text.length > max ? text.slice(0, max) : text
  } catch {
    return ''
  } finally {
    clearTimeout(timer)
    opts.signal?.removeEventListener?.('abort', onAbort)
  }
}

/**
 * 发现站点内可抓取的页面 URL（同源）。
 *
 * 优先级：内联路由表（零请求）→ sitemap.xml（1 请求）→ JS bundle（≤ maxScripts 请求）。
 * 任何一步失败都静默跳过，绝不阻塞抓取主流程。
 */
export async function discoverSiteRoutes(
  pageUrl: string,
  html: string,
  opts: SiteRouteOptions = {},
): Promise<string[]> {
  const base = (() => {
    try {
      return new URL(pageUrl)
    } catch {
      return null
    }
  })()
  if (!base) return []

  const max = Math.max(1, Math.min(500, Math.floor(Number(opts.max ?? 200) || 200)))
  const allowed = new Set(
    (opts.allowedHostnames?.length ? opts.allowedHostnames : [base.hostname])
      .map((h) => hostKey(String(h ?? '')))
      .filter(Boolean),
  )
  if (allowed.size === 0) allowed.add(hostKey(base.hostname))

  const origin = base.origin
  const picked = new Set<string>()
  const out: string[] = []
  const addRoute = (route: string) => {
    if (out.length >= max) return
    let abs = ''
    try {
      abs = new URL(route, origin + '/').toString().replace(/#.*$/, '')
    } catch {
      return
    }
    if (abs === pageUrl.replace(/#.*$/, '')) return
    let host = ''
    try {
      host = new URL(abs).hostname
    } catch {
      return
    }
    if (!hostAllowed(host, allowed)) return
    if (picked.has(abs)) return
    picked.add(abs)
    out.push(abs)
  }

  // ① 内联脚本 / __NUXT__ 里的路由表：零额外请求（只扫 <script>，避免把 CSS 里的 url() 当路由）
  const inlineScripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)]
    .map((m) => m[1])
    .join('\n')
  for (const route of extractRoutePathsFromText(inlineScripts, max)) addRoute(route)

  // ② sitemap.xml：命中即拿到全站清单
  if (out.length < max) {
    const sitemap = await fetchText(`${origin}/sitemap.xml`, {
      userAgent: opts.userAgent,
      timeoutMs: opts.timeoutMs,
      signal: opts.signal,
      maxBytes: 4_000_000,
    })
    for (const loc of extractSitemapLocs(sitemap, max)) {
      try {
        const u = new URL(loc)
        if (!hostAllowed(u.hostname, allowed)) continue
        addRoute(`${u.pathname}${u.search}`)
      } catch {}
    }
  }

  // ③ 同源 JS bundle 里的路由表（Nuxt/React 的路由通常内联在入口 chunk）
  if (out.length < 3) {
    const maxScripts = Math.max(1, Math.min(16, Math.floor(Number(opts.maxScripts ?? 8) || 8)))
    const scriptUrls: string[] = []
    for (const m of html.matchAll(/<script[^>]+src=["']([^"']+)["']/gi)) {
      try {
        const u = new URL(m[1], `${origin}/`)
        if (!hostAllowed(u.hostname, allowed)) continue
        if (!/\.js(\?|$)/i.test(u.pathname)) continue
        if (!scriptUrls.includes(u.toString())) scriptUrls.push(u.toString())
      } catch {}
      if (scriptUrls.length >= maxScripts) break
    }
    // 并行拉取（最多 8 个），避免串行叠加超时
    const codes = await Promise.all(
      scriptUrls.map((src) =>
        fetchText(src, {
          userAgent: opts.userAgent,
          timeoutMs: opts.timeoutMs,
          signal: opts.signal,
          maxBytes: 3_000_000,
        }),
      ),
    )
    for (const code of codes) {
      if (out.length >= max) break
      for (const route of extractRoutePathsFromText(code, max)) addRoute(route)
    }
  }

  return out
}
