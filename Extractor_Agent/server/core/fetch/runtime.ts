import { load } from 'cheerio'
import { chromium, type Browser, type Response } from 'playwright'

import { rewriteSearchSerpSeedToCnBing, safeHost } from '../../services/crawlerAgentTaskUtils'

export type RobotsPolicy = {
  allows: (targetUrl: string) => boolean
  crawlDelayMs: number
}

export type FetchSnapshot = {
  url: string
  finalUrl: string
  title: string
  html: string
  networkJson: any[]
}

type RetryFetchOptions = {
  timeoutMs?: number
  maxAttempts?: number
  baseBackoffMs?: number
}

type ProxyLike = { server: string; username?: string; password?: string }
type ProxyPoolLike = { getNext: () => Promise<ProxyLike | null>; markBad: (proxy: ProxyLike) => void }

function normalizeWhitespace(text: string) {
  return String(text ?? '')
    .replace(/\s+/g, ' ')
    .trim()
}

function clipStr(s: string, n: number) {
  return s.length > n ? s.slice(0, n) : s
}

function nowTs() {
  return Date.now()
}

function sleep(ms: number, signal?: AbortSignal) {
  const t = Math.max(0, Math.floor(ms))
  if (!t) return Promise.resolve()
  return new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => {
      cleanup()
      resolve()
    }, t)
    const onAbort = () => {
      cleanup()
      reject(new Error('aborted'))
    }
    const cleanup = () => {
      clearTimeout(timer)
      if (signal) signal.removeEventListener('abort', onAbort)
    }
    if (signal?.aborted) return onAbort()
    if (signal) signal.addEventListener('abort', onAbort, { once: true })
  })
}

function randInt(min: number, max: number) {
  const a = Number.isFinite(min) ? min : 0
  const b = Number.isFinite(max) ? max : 0
  const lo = Math.min(a, b)
  const hi = Math.max(a, b)
  if (hi <= lo) return Math.floor(lo)
  return Math.floor(lo + Math.random() * (hi - lo + 1))
}

async function humanScroll(page: any) {
  await page.evaluate(async () => {
    await new Promise((resolve) => {
      let totalHeight = 0
      const distance = 200
      let noChangeCount = 0
      const timer = setInterval(() => {
        // @ts-ignore
        const scrollHeight = document.body.scrollHeight
        // @ts-ignore
        window.scrollBy(0, distance)
        totalHeight += distance
        if (totalHeight >= scrollHeight) {
          noChangeCount++
        } else {
          noChangeCount = 0
        }
        if (noChangeCount >= 3 || totalHeight > 10000) {
          clearInterval(timer)
          resolve(null)
        }
      }, 200)
    })
  })
}

async function simulateHumanBehavior(page: any, signal: AbortSignal) {
  await sleep(randInt(1000, 5000), signal)
  try {
    const vp = page.viewportSize() ?? { width: 1280, height: 800 }
    await page.mouse.move(randInt(10, vp.width - 10), randInt(10, vp.height - 10), { steps: randInt(5, 25) })
  } catch {}
  if (Math.random() < 0.85) {
    await humanScroll(page)
  }
  if (Math.random() < 0.6) {
    try {
      await page.mouse.wheel(0, randInt(300, 1500))
    } catch {}
  }
  await sleep(randInt(1000, 5000), signal)
}

function stealthInitScript() {
  // 每段独立 try/catch：某一段失败不影响其余补丁生效
  const script = `
    try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (e) {}
    try {
      window.chrome = {
        app: {
          isInstalled: false,
          InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
          RunningState: { RUNNING: 'running', CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run' }
        },
        runtime: {
          Platform: { MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' },
          connect: function () {},
          sendMessage: function () {},
          id: undefined
        },
        csi: function () { return {} },
        loadTimes: function () { return {} }
      };
    } catch (e) {}
    try {
      const pluginArr = [
        { name: 'PDF Viewer' },
        { name: 'Chrome PDF Viewer' },
        { name: 'Chromium PDF Viewer' },
        { name: 'Microsoft Edge PDF Viewer' },
        { name: 'WebKit built-in PDF' }
      ];
      pluginArr.item = function (i) { return this[i] };
      pluginArr.namedItem = function (n) { return this.find(function (p) { return p.name === n }) || null };
      pluginArr.refresh = function () {};
      Object.defineProperty(navigator, 'plugins', { get: function () { return pluginArr } });
    } catch (e) {}
    try { Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] }); } catch (e) {}
    try { Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 }); } catch (e) {}
    try { Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 }); } catch (e) {}
    try {
      if (window.navigator.permissions && window.navigator.permissions.query) {
        const origQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
        window.navigator.permissions.query = function (p) {
          if (p && p.name === 'notifications') {
            return Promise.resolve({ state: (window.Notification && window.Notification.permission) || 'default', onchange: null });
          }
          return origQuery(p);
        };
      }
    } catch (e) {}
    try {
      const glProto = window.WebGLRenderingContext && window.WebGLRenderingContext.prototype;
      if (glProto && glProto.getParameter) {
        const origGetParameter = glProto.getParameter;
        glProto.getParameter = function (p) {
          if (p === 37445) return 'Intel Inc.';               // UNMASKED_VENDOR_WEBGL
          if (p === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
          return origGetParameter.call(this, p);
        };
      }
    } catch (e) {}
  `
  return script
}

import { loadSessionForHost, saveSessionForHost } from '../../utils/crawl_session_store'
import { classifyFailReason, isHttpBlockedError } from '../../utils/crawl_failure_tags'
import { fetchWithInsecureTls, insecureTlsPolicy, isTlsCertificateError } from '../../utils/tls_policy'
import { canInvokeMcp, markMcpCallUsed } from './mcpBudget'

function pushRunEvent(stats: any, evt: any, options?: any) {
  if (!stats || typeof stats !== 'object') return
  if (!Array.isArray((stats as any)._events)) (stats as any)._events = []
  const arr = (stats as any)._events as any[]
  arr.push(evt)
  if (arr.length > 200) arr.splice(0, arr.length - 200)
  if (options && typeof options === 'object') {
    if (!Array.isArray((options as any).__channelTrace)) (options as any).__channelTrace = []
    ;(options as any).__channelTrace.push({ ...evt, at: new Date().toISOString() })
    if ((options as any).__channelTrace.length > 200) {
      ;(options as any).__channelTrace.splice(0, (options as any).__channelTrace.length - 200)
    }
  }
}

export function isCloudScrapeConfigured(config: any): boolean {
  return Boolean(String(config?.mcp?.provider ?? '').trim())
}

async function tryCloudScrapeFallback(
  url: string,
  config: any,
  signal: AbortSignal,
  emitLog: (level: 'info' | 'warn' | 'error', message: string) => void,
  runStats: any,
  options: any,
  tStart: number,
  cloudPermanentlyDisabled: boolean,
): Promise<FetchSnapshot | null> {
  if (cloudPermanentlyDisabled || !config?.mcp?.provider) return null
  if (!canInvokeMcp(options)) {
    emitLog('warn', 'Worker：云抓取额度已用尽，跳过 MCP 调用')
    return null
  }
  markMcpCallUsed(options)
  const mcpSnap = await fetchViaMcp(url, config, signal, emitLog)
  if (mcpSnap) {
    pushRunEvent(runStats, { ts: nowTs(), host: safeHost(url), url, channel: 'mcp', status: 'ok', ms: Date.now() - tStart }, options)
    return mcpSnap
  }
  return null
}

async function fetchViaMcpBudgeted(
  url: string,
  config: any,
  signal: AbortSignal,
  options: any,
  emitLog: (level: 'info' | 'warn' | 'error', message: string) => void,
): Promise<FetchSnapshot | null> {
  if (!canInvokeMcp(options)) {
    emitLog('warn', 'Worker：云抓取额度已用尽，跳过 MCP 调用')
    return null
  }
  markMcpCallUsed(options)
  return fetchViaMcp(url, config, signal, emitLog)
}

function buildTimeoutSignal(parentSignal: AbortSignal, timeoutMs: number) {
  const timeoutCtrl = new AbortController()
  const merged = new AbortController()
  const cleanupHandlers: Array<() => void> = []
  const timer = setTimeout(() => timeoutCtrl.abort(new Error('request_timeout')), timeoutMs)
  const abortMerged = () => {
    if (!merged.signal.aborted) {
      merged.abort(parentSignal.aborted ? parentSignal.reason : timeoutCtrl.signal.reason)
    }
  }
  const onParentAbort = () => abortMerged()
  const onTimeoutAbort = () => abortMerged()
  parentSignal.addEventListener('abort', onParentAbort)
  timeoutCtrl.signal.addEventListener('abort', onTimeoutAbort)
  cleanupHandlers.push(() => clearTimeout(timer))
  cleanupHandlers.push(() => parentSignal.removeEventListener('abort', onParentAbort))
  cleanupHandlers.push(() => timeoutCtrl.signal.removeEventListener('abort', onTimeoutAbort))
  return {
    signal: merged.signal,
    cleanup: () => cleanupHandlers.forEach((fn) => fn())
  }
}

export async function fetchWithRetry(
  input: string,
  init: RequestInit,
  parentSignal: AbortSignal,
  options?: RetryFetchOptions
): Promise<Response> {
  const timeoutMs = Number.isFinite(Number(options?.timeoutMs)) ? Math.max(1000, Math.floor(Number(options?.timeoutMs))) : 15_000
  const maxAttempts = Number.isFinite(Number(options?.maxAttempts)) ? Math.max(1, Math.floor(Number(options?.maxAttempts))) : 2
  const baseBackoffMs = Number.isFinite(Number(options?.baseBackoffMs)) ? Math.max(100, Math.floor(Number(options?.baseBackoffMs))) : 600
  let lastErr: unknown = null
  let insecureTlsAttempted = false
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    const { signal, cleanup } = buildTimeoutSignal(parentSignal, timeoutMs)
    try {
      const res = await fetch(input, { ...init, signal })
      if (res.status === 429 || res.status >= 500) {
        if (attempt < maxAttempts) {
          const waitMs = baseBackoffMs * Math.pow(2, attempt - 1)
          await sleep(waitMs, parentSignal)
          continue
        }
      }
      return res as any
    } catch (e) {
      lastErr = e
      const msg = String((e as any)?.message ?? e ?? '')
      // 自签名 / 证书与 IP 不匹配的站点：标准校验必然失败，按策略放行后重试一次
      if (!insecureTlsAttempted && isTlsCertificateError(e)) {
        insecureTlsAttempted = true
        const decision = insecureTlsPolicy(input)
        if (decision.allow) {
          try {
            return (await fetchWithInsecureTls(input, init, signal)) as any
          } catch (e2) {
            lastErr = e2
          }
        }
      }
      const retryable = /timeout|aborted|ECONNRESET|ENOTFOUND|EAI_AGAIN|fetch failed/i.test(msg)
      if (attempt >= maxAttempts || !retryable) throw lastErr
      const waitMs = baseBackoffMs * Math.pow(2, attempt - 1)
      await sleep(waitMs, parentSignal)
    } finally {
      cleanup()
    }
  }
  throw lastErr || new Error('fetch_with_retry_failed')
}

function parseRobotsTxt(text: string, ua: string): RobotsPolicy {
  const lines = String(text || '').split(/\\r?\\n/)
  const groups: Array<{ uas: string[]; allows: string[]; disallows: string[]; crawlDelaySec: number | null }> = []
  let current: { uas: string[]; allows: string[]; disallows: string[]; crawlDelaySec: number | null } | null = null
  for (const raw of lines) {
    const clean = raw.replace(/#.*$/, '').trim()
    if (!clean) continue
    const idx = clean.indexOf(':')
    if (idx <= 0) continue
    const key = clean.slice(0, idx).trim().toLowerCase()
    const value = clean.slice(idx + 1).trim()
    if (key === 'user-agent') {
      if (!current || (current.uas.length > 0 && (current.allows.length > 0 || current.disallows.length > 0 || current.crawlDelaySec !== null))) {
        current = { uas: [], allows: [], disallows: [], crawlDelaySec: null }
        groups.push(current)
      }
      current.uas.push(value.toLowerCase())
      continue
    }
    if (!current) continue
    if (key === 'allow') current.allows.push(value || '/')
    else if (key === 'disallow') current.disallows.push(value)
    else if (key === 'crawl-delay') {
      const sec = Number(value)
      if (Number.isFinite(sec) && sec >= 0) current.crawlDelaySec = sec
    }
  }

  const uaNorm = String(ua || '').toLowerCase()
  const pickGroup = () => {
    let best: typeof groups[number] | null = null
    let bestLen = -1
    for (const g of groups) {
      for (const token of g.uas) {
        if (token === '*') {
          if (bestLen < 0) {
            best = g
            bestLen = 0
          }
          continue
        }
        if (uaNorm.includes(token) && token.length > bestLen) {
          best = g
          bestLen = token.length
        }
      }
    }
    return best
  }

  const group = pickGroup()
  const allows = group?.allows ?? []
  const disallows = group?.disallows ?? []
  const crawlDelayMs = Math.max(0, Math.floor(Number(group?.crawlDelaySec ?? 0) * 1000))
  const isMatch = (pathWithQuery: string, rulePath: string) => {
    if (!rulePath) return false
    return pathWithQuery.startsWith(rulePath)
  }
  return {
    crawlDelayMs,
    allows: (targetUrl: string) => {
      let p = '/'
      try {
        const u = new URL(targetUrl)
        p = `${u.pathname || '/'}${u.search || ''}`
      } catch {}
      let bestAllowLen = -1
      let bestDisallowLen = -1
      for (const a of allows) {
        if (isMatch(p, a)) bestAllowLen = Math.max(bestAllowLen, a.length)
      }
      for (const d of disallows) {
        if (!d) continue
        if (isMatch(p, d)) bestDisallowLen = Math.max(bestDisallowLen, d.length)
      }
      if (bestDisallowLen < 0) return true
      return bestAllowLen >= bestDisallowLen
    }
  }
}

function pickUserAgent(config: any, taskPlan?: { targetSite?: string }) {
  const explicit = String(config?.crawler?.userAgent ?? '').trim()
  if (explicit) return explicit
  if (taskPlan?.targetSite === 'jd' && Math.random() < 0.5) {
    return 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1'
  }
  const base = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edg/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1'
  ]
  // 简单随机，保持与原实现一致的“可变 UA”效果
  const chosen = base[Math.floor(Math.random() * base.length)]
  return chosen || base[0]
}

export async function fetchRobotsPolicy(url: string, config: any, signal: AbortSignal): Promise<RobotsPolicy | null> {
  let origin = ''
  try {
    origin = new URL(url).origin
  } catch {
    return null
  }
  if (!origin) return null
  const robotsUrl = `${origin}/robots.txt`
  const res = await fetchWithRetry(
    robotsUrl,
    {
      headers: {
        'user-agent': pickUserAgent(config),
        'accept-language': 'zh-CN,zh;q=0.9,en;q=0.7'
      },
      redirect: 'follow'
    },
    signal,
    { timeoutMs: 8000, maxAttempts: 1, baseBackoffMs: 300 }
  ).catch(() => null)
  if (!res || !res.ok) return null
  const text = await (res as any).text().catch(() => '')
  if (!text) return null
  return parseRobotsTxt(text, pickUserAgent(config))
}

let globalBrowser: Browser | null = null
let globalBrowserHeadless: boolean | null = null
// 已触发过「有头/人工验证」兜底的 host：同站只弹一次，后续靠持久化 cookie 复用
const headfulCaptchaTriedHosts = new Set<string>()

function isBrowserRuntimeUnavailable(msg: string): boolean {
  const m = String(msg || '').toLowerCase()
  return (
    /executable doesn't exist|playwright install|browsertype\.launch/i.test(m) ||
    /libnss3\.so|libnspr4|libatk|libgbm|libx11|libasound|shared libraries|shared object file/i.test(m) ||
    /exitcode=127|exit code 127|no such file or directory/i.test(m) ||
    /browser has been closed|target page, context or browser has been closed|page has been closed|context has been closed/i.test(m)
  )
}

function resolveChromiumExecutablePath(): string | undefined {
  const fromEnv = String(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? '').trim()
  return fromEnv || undefined
}

async function getBrowser(headless: boolean) {
  if (!globalBrowser) {
    globalBrowserHeadless = headless
    const executablePath = resolveChromiumExecutablePath()
    try {
      globalBrowser = await chromium.launch({
        ...(executablePath ? { executablePath } : {}),
        headless,
        ignoreDefaultArgs: ['--enable-automation'],
        args: [
          '--disable-blink-features=AutomationControlled',
          '--disable-dev-shm-usage',
          '--no-default-browser-check',
          '--disable-features=IsolateOrigins,site-per-process'
        ]
      })
    } catch (e: any) {
      globalBrowser = null
      globalBrowserHeadless = null
      throw e
    }
  } else if (globalBrowserHeadless !== headless) {
    try {
      await globalBrowser.close()
    } catch {}
    globalBrowser = null
    globalBrowserHeadless = null
    return await getBrowser(headless)
  }
  return globalBrowser
}

export async function ensureBrowser(headless: boolean) {
  return await getBrowser(Boolean(headless))
}

export async function fetchHtml(url: string, config: any, signal: AbortSignal, session?: any) {
  const ua = String(config?.crawler?.userAgent ?? 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36').trim()
  const origin = (() => {
    try {
      return new URL(url).origin
    } catch {
      return ''
    }
  })()
  const referer = (() => {
    if (/douban\.com/i.test(url)) return 'https://movie.douban.com/'
    return origin ? `${origin}/` : ''
  })()
  const headers: Record<string, string> = {
    'user-agent': ua,
    accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'accept-language': 'zh-CN,zh;q=0.9,en;q=0.7',
    referer
  }
  // 质询 cookie 复用（跨通道）：优先用传入会话，其次读该 host 的持久化会话——
  // 浏览器/人工验证通过后的 WAF cookie，能让 HTTP 直连也直接过盾（易探云这类按 TLS 指纹拦直连的站）
  const cookieHeader = (() => {
    let cookies: any[] | undefined = session?.cookies
    if (!cookies?.length) {
      try {
        const host = safeHost(url)
        const disk = host ? loadSessionForHost(host) : null
        if (disk?.cookies?.length) cookies = disk.cookies
      } catch {}
    }
    if (!cookies?.length) return ''
    try {
      return cookies
        .filter((c) => c && typeof c.name === 'string' && typeof c.value === 'string')
        .map((c) => `${c.name}=${c.value}`)
        .join('; ')
    } catch {
      return ''
    }
  })()
  if (cookieHeader) headers['cookie'] = cookieHeader
  const res = await fetchWithRetry(
    url,
    { headers, redirect: 'follow' },
    signal,
    { timeoutMs: 15_000, maxAttempts: 2, baseBackoffMs: 600 }
  )
  if (!(res as any).ok) {
    let body = ''
    try {
      body = await (res as any).text()
    } catch {}
    const hint = body ? `; body=${clipStr(body.replace(/\s+/g, ' ').trim(), 160)}` : ''
    throw new Error(`HTTP ${(res as any).status} ${(res as any).statusText}; url=${url}${hint}`)
  }
  const contentType = String((res as any).headers?.get?.('content-type') ?? '')
  const charsetFromHeader = (() => {
    const m = contentType.match(/charset\s*=\s*([^;]+)/i)
    return m ? String(m[1] ?? '').trim().toLowerCase() : ''
  })()
  const buf = await (res as any).arrayBuffer()
  const pickEncoding = () => {
    const c = charsetFromHeader
    if (c.includes('gbk') || c.includes('gb2312') || c.includes('gb18030')) return 'gbk'
    if (c) return c
    try {
      const sniff = new TextDecoder('utf-8').decode(buf.slice(0, Math.min(4096, buf.byteLength)))
      const m1 = sniff.match(/<meta[^>]+charset\s*=\s*["']?\s*([a-z0-9_-]+)\s*["']?/i)
      const m2 = sniff.match(/<meta[^>]+content\s*=\s*["'][^"']*charset=([a-z0-9_-]+)[^"']*["']/i)
      const cs = String((m1?.[1] ?? m2?.[1] ?? '')).trim().toLowerCase()
      if (cs.includes('gbk') || cs.includes('gb2312') || cs.includes('gb18030')) return 'gbk'
      if (cs) return cs
    } catch {}
    return 'utf-8'
  }
  const enc = pickEncoding()
  try {
    return new TextDecoder(enc as any).decode(buf)
  } catch {
    return new TextDecoder('utf-8').decode(buf)
  }
}

export async function fetchViaMcp(url: string, config: any, signal: AbortSignal, emitLog?: (l: any, m: string) => void): Promise<FetchSnapshot | null> {
  const provider = String(config?.mcp?.provider ?? '').trim().toLowerCase()
  const apiKey = String(config?.mcp?.apiKey ?? '').trim()
  const baseUrl = String(config?.mcp?.baseUrl ?? '').trim()
  const qp = String(config?.mcp?.queryParamKey ?? 'apikey').trim()
  const hk = String(config?.mcp?.headerKey ?? '').trim()
  const render = Boolean(config?.mcp?.render)
  if (!provider) return null

  if (emitLog) emitLog('info', `Worker：云抓取 (${provider}) 正在抓取 ${url} ...`)

  const build = (u: string) => {
    if (provider === 'scrapingant') {
      const endpoint = baseUrl || 'https://api.scrapingant.com/v2/general'
      const q = new URL(endpoint)
      q.searchParams.set('url', u)
      if (render) q.searchParams.set('js_snippet', '')
      const headers: Record<string, string> = {}
      if (apiKey) headers['x-api-key'] = apiKey
      return { url: q.toString(), headers }
    }
    if (provider === 'zenrows') {
      const endpoint = baseUrl || 'https://api.zenrows.com/v1/'
      const q = new URL(endpoint)
      q.searchParams.set('url', u)
      if (apiKey) q.searchParams.set('apikey', apiKey)
      if (render) q.searchParams.set('js_render', 'true')
      return { url: q.toString(), headers: {} as Record<string, string> }
    }
    if (provider === 'scraperapi') {
      const endpoint = baseUrl || 'https://api.scraperapi.com/'
      const q = new URL(endpoint)
      if (apiKey) q.searchParams.set('api_key', apiKey)
      q.searchParams.set('url', u)
      if (render) q.searchParams.set('render', 'true')
      return { url: q.toString(), headers: {} as Record<string, string> }
    }
    if (provider === 'bailian') {
      const endpoint = baseUrl || 'https://dashscope.aliyuncs.com/api/v1/services/aigc/tools/crawler'
      const headers: Record<string, string> = {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json'
      }
      return {
        url: endpoint,
        headers,
        method: 'POST',
        body: JSON.stringify({
          model: 'qwen3.5-flash',
          input: { url: u },
          parameters: { render: render }
        })
      }
    }
    if (provider === 'firecrawl') {
      const endpoint = baseUrl || 'https://api.firecrawl.dev/v1/scrape'
      const headers: Record<string, string> = {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json'
      }
      return {
        url: endpoint,
        headers,
        method: 'POST',
        body: JSON.stringify({
          url: u,
          formats: ['html', 'markdown'],
          waitFor: /zhihu\.com\/hot/i.test(u) ? Math.max(render ? 3000 : 0, 8000) : render ? 3000 : 0,
          ...(String(config?.mcp?.country ?? '').trim()
            ? { location: { country: String(config.mcp.country).trim() } }
            : {}),
        })
      }
    }
    if (provider === 'generic') {
      if (!baseUrl) return null
      let final = baseUrl.replace('{url}', encodeURIComponent(u)).replace('{apiKey}', encodeURIComponent(apiKey))
      const headers: Record<string, string> = {}
      if (hk && apiKey) headers[hk] = apiKey
      if (qp && apiKey && !final.includes(`${qp}=`)) {
        const j = final.includes('?') ? '&' : '?'
        final = `${final}${j}${encodeURIComponent(qp)}=${encodeURIComponent(apiKey)}`
      }
      return { url: final, headers }
    }
    return null
  }
  const req = build(url)
  if (!req || !req.url) return null
  const res = await fetchWithRetry(
    req.url,
    {
      method: (req as any).method || 'GET',
      body: (req as any).body,
      headers: {
        ...(req.headers || {}),
        'user-agent': pickUserAgent(config),
        'accept-language': 'zh-CN,zh;q=0.9,en;q=0.7'
      },
      redirect: 'follow'
    },
    signal,
    { timeoutMs: 20_000, maxAttempts: 2, baseBackoffMs: 800 }
  )
  if (!(res as any).ok) {
    const errText = await (res as any).text().catch(() => '')
    if (emitLog) emitLog('warn', `Worker：云抓取 (${provider}) 调用失败：HTTP ${(res as any).status} ${errText.slice(0, 100)}`)
    return null
  }
  let html = ''
  if (provider === 'bailian') {
    const payload = await (res as any).json().catch(() => null) as any
    html = payload?.output?.result || payload?.output?.text || ''
    if (!html && payload) html = JSON.stringify(payload)
  } else if (provider === 'firecrawl') {
    const payload = await (res as any).json().catch(() => null) as any
    html = payload?.data?.html || payload?.html || ''
    const md = String(payload?.data?.markdown || payload?.markdown || '').trim()
    if (md && (!html || html.length < 500)) {
      html = `<article>${md.replace(/\n/g, '<br>')}</article>`
    }
    if (!html && payload) html = JSON.stringify(payload)
  } else {
    html = await (res as any).text().catch(() => '')
  }
  if (!html) {
    if (emitLog) emitLog('warn', `Worker：云抓取 (${provider}) 返回内容为空`)
    return null
  }
  const title = extractTitleFromHtml(html)
  if (looksLikeCaptchaOrBlock(title, html)) {
    if (emitLog) emitLog('warn', `Worker：云抓取 (${provider}) 返回拦截页/验证码页`)
    return null
  }

  if (emitLog) emitLog('info', `Worker：云抓取 (${provider}) 成功`)
  return { url, finalUrl: url, title, html, networkJson: [] }
}

/** @deprecated 请使用 fetchViaCloudScrape；保留别名兼容旧引用 */
export const fetchViaCloudScrape = fetchViaMcp

type CaptchaBlockHit = { rule: string; snippet: string } | null

/**
 * 判断页面是否为拦截/质询页，并给出命中规则与上下文（用于诊断日志）。
 *
 * 误报防线（易探云/浪浪云这类「正常页被判 captcha」的教训）：
 * - 整页级词匹配（裸 captcha、中文拦截文案、google 拦截文案）只在「内容贫瘠」的页面上生效——
 *   真质询/拦截页正文极少（几百字以内），而正常首页正文上千字，即使页脚有验证码组件也不误杀；
 * - WAF 强指纹（challenge-platform/cf_chl_/grecaptcha/turnstile…）无条件生效，它们只出现在拦截页；
 * - 标题级规则无条件生效。
 */
function findCaptchaBlockHit(title: string, html: string): CaptchaBlockHit {
  const t = normalizeWhitespace(title).toLowerCase()
  const h = normalizeWhitespace(html).toLowerCase()
  const titleCtx = `title="${normalizeWhitespace(title).slice(0, 60)}"`
  const ctx = (idx: number) => clipStr(h.slice(Math.max(0, idx - 50), idx + 110).replace(/\s+/g, ' '), 150)

  // 内容贫瘠判定：去掉 script/style/标签后的可见正文长度。质询页通常 <200 字，正常首页上千字
  let contentRich: boolean | null = null
  const isContentRich = () => {
    if (contentRich === null) {
      const visible = h
        .replace(/<script[\s\S]*?<\/script>/g, ' ')
        .replace(/<style[\s\S]*?<\/style>/g, ' ')
        .replace(/<[^>]+>/g, ' ')
        .replace(/\s+/g, '')
      contentRich = visible.length > 300
    }
    return contentRich
  }

  if (t.includes('just a moment') || t.includes('attention required')) return { rule: 'title:cloudflare_5s', snippet: titleCtx }
  if (/验证码|安全验证|人机验证|访问受限|安全检查/.test(title)) return { rule: 'title:中文拦截词', snippet: titleCtx }
  if (/(captcha|cloudflare|cf-ray|verify you are human|bot detection|access denied)/i.test(t)) return { rule: 'title:英文拦截词', snippet: titleCtx }

  let m = /challenge-platform|cf_chl_|grecaptcha|recaptcha\/api|hcaptcha\.com|turnstile|captcha-delivery|arkose|px-captcha|distil_r_captcha|incapsula/i.exec(h)
  if (m) return { rule: 'html:WAF强特征', snippet: ctx(m.index) }

  // 词级规则：仅对内容贫瘠页面生效（配合下方诊断日志实锤误杀源）
  if (!isContentRich()) {
    m = /captcha/i.exec(h)
    if (m) return { rule: 'html:captcha(内容贫瘠页)', snippet: ctx(m.index) }
    m = /请完成安全验证|检测到异常请求|您的访问过于频繁|抱歉，您访问的内容不存在/.exec(h)
    if (m) return { rule: 'html:中文拦截文案', snippet: ctx(m.index) }
    m = /unusual traffic from your computer network|sorry we can't complete your request|before you continue to google/i.exec(h)
    if (m) return { rule: 'html:google拦截文案', snippet: ctx(m.index) }
  }
  return null
}

export function looksLikeCaptchaOrBlock(title: string, html: string) {
  return findCaptchaBlockHit(title, html) !== null
}

/** 拦截判定诊断日志：命中规则 + 上下文，直接定位误杀源 */
function logCaptchaDiagnosis(
  emitLog: ((level: 'info' | 'warn' | 'error', message: string) => void) | null,
  url: string,
  title: string,
  html: string,
  stage: string
) {
  if (!emitLog) return
  const hit = findCaptchaBlockHit(title, html)
  if (!hit) return
  emitLog('warn', `Worker：拦截判定命中 [${hit.rule}]（${stage}）${url}；htmlLen=${html.length}；${hit.snippet}`)
}

/**
 * WAF 的 JS 质询（如 Cloudflare 5 秒盾、高防 CDN 人机校验）通常无需人工交互、
 * 几秒内自动通过：轮询等待页面标题/内容变化后复查，避免质询还没跑完就被判死。
 */
async function waitChallengeAutoPass(page: any, signal: AbortSignal, maxWaitMs: number): Promise<boolean> {
  const deadline = Date.now() + Math.max(3000, maxWaitMs)
  while (Date.now() < deadline) {
    if (signal.aborted) return false
    await sleep(1500, signal)
    try {
      const t = await page.title().catch(() => '')
      const h = await page.content()
      if (!looksLikeCaptchaOrBlock(t, h)) return true
    } catch {
      return false
    }
  }
  return false
}

export function extractTitleFromHtml(html: string) {
  try {
    const $ = load(html)
    return normalizeWhitespace($('title').first().text())
  } catch {
    return ''
  }
}

/**
 * 有头浏览器兜底通道（独立 chromium 实例，不影响全局共享浏览器）：
 * 机器优先 —— 有头模式下多数 WAF 的 JS 质询会直接放行；
 * 若仍受阻且 waitForHuman=true，则保持窗口打开，等待用户在窗口内完成人机验证（能机器做就不人工）。
 * 验证通过后 cookie 按 host 持久化，同站后续页面（无头）自动复用。
 */
async function fetchViaHeadfulFallback(
  url: string,
  config: any,
  signal: AbortSignal,
  emitLog: (level: 'info' | 'warn' | 'error', message: string) => void,
  session: any,
  waitForHuman: boolean
): Promise<FetchSnapshot | null> {
  let browser: Browser | null = null
  try {
    const executablePath = resolveChromiumExecutablePath()
    browser = await chromium.launch({
      ...(executablePath ? { executablePath } : {}),
      headless: !waitForHuman,
      ignoreDefaultArgs: ['--enable-automation'],
      args: [
        '--disable-blink-features=AutomationControlled',
        '--disable-dev-shm-usage',
        '--no-default-browser-check'
      ]
    })
    const ua = pickUserAgent(config)
    const context = await browser.newContext({
      userAgent: ua,
      viewport: { width: 1366, height: 768 },
      locale: 'zh-CN',
      timezoneId: 'Asia/Shanghai',
      ignoreHTTPSErrors: true,
      extraHTTPHeaders: { 'accept-language': 'zh-CN,zh;q=0.9,en;q=0.7' }
    })
    if (session?.cookies) {
      try {
        await context.addCookies(session.cookies)
      } catch {}
    }
    await context.addInitScript(stealthInitScript())
    const page = await context.newPage()
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45_000 })
    await simulateHumanBehavior(page, signal).catch(() => {})

    let title = await page.title().catch(() => '')
    let html = await page.content()
    if (!looksLikeCaptchaOrBlock(title, html)) {
      return finalizeHeadfulSnapshot(page, context, url, title, html)
    }
    logCaptchaDiagnosis(emitLog, url, title, html, '有头兜底/初始')

    // 机器阶段：先给 JS 质询自动通过的时间
    const autoWaitMs = waitForHuman ? 25_000 : 18_000
    if (emitLog) emitLog('warn', `Worker：有头浏览器遇到质询页，等待其自动通过（最长 ${Math.round(autoWaitMs / 1000)}s）…`)
    const autoPassed = await waitChallengeAutoPass(page, signal, autoWaitMs)
    title = await page.title().catch(() => '')
    html = await page.content()
    if (autoPassed && !looksLikeCaptchaOrBlock(title, html)) {
      if (emitLog) emitLog('info', 'Worker：质询已自动通过（有头模式）')
      return finalizeHeadfulSnapshot(page, context, url, title, html)
    }
    // 质询即使被误判（或部分通过），已拿到的 cookie 也要落盘：
    // 下一次抓取（无头/HTTP 直连）带上它往往能直接过 WAF
    await saveHeadfulCookiesQuietly(context, url)

    if (!waitForHuman) return null

    // 人工阶段：等待用户在窗口内完成验证（最后手段）
    if (emitLog) {
      emitLog('warn', `Worker：请在弹出的浏览器窗口中完成人机验证（最长 180 秒，完成后自动继续）：${url}`)
      emitLog('info', `Worker：若窗口内没有出现验证组件，说明质询可能已通过但被误判——不用操作，关闭窗口即可放弃本次，cookie 已保存`)
    }
    const humanDeadline = Date.now() + 180_000
    while (Date.now() < humanDeadline) {
      if (signal.aborted) return null
      await sleep(2000, signal)
      try {
        title = await page.title().catch(() => '')
        html = await page.content()
      } catch {
        // 页面/窗口被用户关闭：明确记录，不再静默失败
        if (emitLog) emitLog('warn', 'Worker：验证窗口已被关闭（或页面不可用），放弃本次人工验证；已保存的 cookie 仍会在后续抓取中复用')
        return null
      }
      if (!looksLikeCaptchaOrBlock(title, html)) break
    }
    if (looksLikeCaptchaOrBlock(title, html)) {
      logCaptchaDiagnosis(emitLog, url, title, html, '有头兜底/人工等待超时')
      if (emitLog) emitLog('error', `Worker：人工验证超时或未通过：${url}`)
      await saveHeadfulCookiesQuietly(context, url)
      return null
    }
    if (emitLog) emitLog('info', 'Worker：人工验证通过，cookie 已保存供同站后续页面复用')
    return finalizeHeadfulSnapshot(page, context, url, title, html)
  } catch {
    return null
  } finally {
    try {
      if (browser) await browser.close()
    } catch {}
  }
}

/** 只保存 cookie，不采集页面（用于兜底失败但仍想复用会话的场景） */
async function saveHeadfulCookiesQuietly(context: any, url: string) {
  try {
    const cookies = await context.cookies()
    const host = safeHost(url)
    if (host && cookies.length) saveSessionForHost(host, cookies as any)
  } catch {}
}

async function finalizeHeadfulSnapshot(
  page: any,
  context: any,
  url: string,
  title: string,
  html: string
): Promise<FetchSnapshot> {
  let finalUrl = url
  try {
    finalUrl = page.url()
  } catch {}
  try {
    const cookies = await context.cookies()
    const host = safeHost(finalUrl || url)
    if (host && cookies.length) saveSessionForHost(host, cookies as any)
  } catch {}
  try {
    await context.close()
  } catch {}
  try {
    await page.close()
  } catch {}
  return { url, finalUrl, title: normalizeWhitespace(title), html, networkJson: [] }
}

export async function workerExecute(
  url: string,
  config: any,
  options: any,
  signal: AbortSignal,
  session: any,
  proxyPool: ProxyPoolLike | null,
  emitLog: (level: 'info' | 'warn' | 'error', message: string) => void,
  fastPath: boolean = false,
  task?: string
): Promise<FetchSnapshot> {
  url = rewriteSearchSerpSeedToCnBing(String(url ?? '').trim())
  const maxAttempts = 3
  // 无头开关：options.headless 优先，其次环境变量 EXTRACTOR_HEADLESS（0/false → 有头模式）
  const envHeadless = String(process.env.EXTRACTOR_HEADLESS ?? '').trim().toLowerCase()
  const headless = options.headless ?? !(envHeadless === '0' || envHeadless === 'false')
  const delayMinMs = Number.isFinite(Number(options.delayMinMs)) ? Math.max(0, Math.floor(Number(options.delayMinMs))) : 1000
  const delayMaxMs = Number.isFinite(Number(options.delayMaxMs)) ? Math.max(0, Math.floor(Number(options.delayMaxMs))) : 5000
  const attemptDelay = () => sleep(randInt(delayMinMs, delayMaxMs), signal)
  const shortErr = (msg: string) => normalizeWhitespace(msg).slice(0, 160)
  let mcpFailedPermanently = false
  // 每次执行前重读该 host 的持久化 cookie 会话：有头模式/人工验证保存的登录态
  // 能立刻被同 run 后续页面与后续任务复用（state.session 只是 run 启动时载入的快照）
  let effectiveSession = session
  try {
    const diskSession = safeHost(url) ? loadSessionForHost(safeHost(url)) : null
    if (diskSession?.cookies?.length) {
      effectiveSession = diskSession
    }
  } catch {}
  if (!(options as any).__runStats) (options as any).__runStats = { _events: [] }
  const runStats: any = (options as any).__runStats

  const preferMcp = Boolean((options as any).__preferMcp) && Boolean(config?.mcp?.provider)
  let useFastPath = fastPath
  if (preferMcp) {
    const tMcp = Date.now()
    emitLog('info', `Worker：云抓取优先通道 ${url}`)
    const mcpSnap = await fetchViaMcpBudgeted(url, config, signal, options, emitLog)
    if (mcpSnap) {
      pushRunEvent(runStats, { ts: nowTs(), host: safeHost(url), url, channel: 'mcp', status: 'ok', ms: Date.now() - tMcp }, options)
      return mcpSnap
    }
    mcpFailedPermanently = true
    useFastPath = false
    emitLog('warn', `Worker：云抓取未命中，切换浏览器兜底（跳过 HTTP 直连）：${url}`)
  }

  if (useFastPath) {
    const tStart = Date.now()
    emitLog('info', `Worker：尝试快速路径抓取 ${url}`)

    const taskPlan = (options as any)?.__taskPlan as { targetSite?: string } | undefined
    const jd = taskPlan?.targetSite === 'jd'
    if (jd && config?.mcp?.provider) {
      const mcpSnap = await fetchViaMcpBudgeted(url, config, signal, options, emitLog)
      if (mcpSnap) return mcpSnap
      mcpFailedPermanently = true
      emitLog('warn', `Worker：京东任务云抓取首次失败，本次 URL 不再重复调用以节省额度`)
    }

    try {
      const html = await fetchHtml(url, config, signal, session ?? undefined)
      const title = extractTitleFromHtml(html)
      if (!looksLikeCaptchaOrBlock(title, html)) {
        pushRunEvent(runStats, { ts: nowTs(), host: safeHost(url), url, channel: 'http', status: 'ok', ms: Date.now() - tStart }, options)
        return { url, finalUrl: url, title, html, networkJson: [] }
      }
      const fb = await tryCloudScrapeFallback(url, config, signal, emitLog, runStats, options, tStart, mcpFailedPermanently)
      if (fb) return fb
      mcpFailedPermanently = true
      emitLog('warn', `Worker：快速路径云抓取失败，本次 URL 不再重复云抓取`)
    } catch (e: any) {
      const msg = typeof e?.message === 'string' ? e.message : String(e)
      const shouldFallback = isHttpBlockedError(msg)

      if (!mcpFailedPermanently && config?.mcp?.provider) {
        const fb = await tryCloudScrapeFallback(url, config, signal, emitLog, runStats, options, Date.now(), mcpFailedPermanently)
        if (fb) return fb
        mcpFailedPermanently = true
        emitLog('warn', `Worker：异常路径云抓取失败，本次 URL 不再重复云抓取`)
      }

      if (!shouldFallback) {
        // 非反爬类失败（证书/网络/超时）也要落一条事件，否则 _events 为空，
        // 上层只能看到 “主通道未知 + empty_dom” 这类误导性标签。
        pushRunEvent(runStats, { ts: nowTs(), host: safeHost(url), url, channel: 'http', status: 'error', reason: classifyFailReason(msg), ms: Date.now() - tStart }, options)
        throw e
      }
      pushRunEvent(runStats, { ts: nowTs(), host: safeHost(url), url, channel: 'http', status: 'error', reason: classifyFailReason(msg), ms: Date.now() - tStart }, options)
      emitLog('warn', `Worker：直连抓取被拦截，切换浏览器模式：${url}`)
    }
  }

  let lastErr: any = null
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    if (attempt === 2 && !mcpFailedPermanently) {
      const fb = await tryCloudScrapeFallback(url, config, signal, emitLog, runStats, options, Date.now(), mcpFailedPermanently)
      if (fb) {
        emitLog('info', `Worker：浏览器失败后云抓取兜底成功 ${url}`)
        return fb
      }
      mcpFailedPermanently = true
      emitLog('warn', `Worker：浏览器兜底云抓取失败，本次 URL 不再重复调用`)
    }
    const proxy = proxyPool ? await proxyPool.getNext() : null
    const ua = pickUserAgent(config, (options as any)?.__taskPlan)
    const tStart = Date.now()
    let browser: Browser
    try {
      browser = await getBrowser(headless)
    } catch (e: any) {
      const msg = String(e?.message || e || '')
      if (!isBrowserRuntimeUnavailable(msg)) throw e
      emitLog('warn', 'Worker：浏览器未就绪，回退 HTTP/云抓取')
      const fb = await tryCloudScrapeFallback(url, config, signal, emitLog, runStats, options, tStart, false)
      if (fb) return fb
      const html = await fetchHtml(url, config, signal, session ?? undefined)
      const title = extractTitleFromHtml(html)
      pushRunEvent(runStats, { ts: nowTs(), host: safeHost(url), url, channel: 'http', status: 'ok', ms: Date.now() - tStart }, options)
      return { url, finalUrl: url, title, html, networkJson: [] }
    }
    const tlsDecision = insecureTlsPolicy(url)
    if (tlsDecision.allow) {
      emitLog('warn', `Worker：已对 ${safeHost(url)} 跳过证书校验（${tlsDecision.reason}）`)
    }
    const context = await browser.newContext({
      userAgent: ua,
      viewport: ua.includes('iPhone') ? { width: 375, height: 667 } : { width: 1366, height: 768 },
      javaScriptEnabled: true,
      locale: 'zh-CN',
      timezoneId: 'Asia/Shanghai',
      deviceScaleFactor: ua.includes('iPhone') ? 2 : 1,
      hasTouch: ua.includes('iPhone'),
      ignoreHTTPSErrors: tlsDecision.allow,
      proxy: proxy
        ? {
            server: proxy.server,
            username: proxy.username,
            password: proxy.password
          }
        : undefined,
      extraHTTPHeaders: {
        'accept-language': 'zh-CN,zh;q=0.9,en;q=0.7'
      }
    })

    await context.addInitScript(stealthInitScript())

    if (effectiveSession?.cookies) {
      await context.addCookies(effectiveSession.cookies)
    }

    const page = await context.newPage()
    let onAbort: any = null
    let onResponse: any = null
    try {
      const networkJson: any[] = []
      let capturedCount = 0
      let capturedChars = 0
      const maxCaptured = 20
      const maxChars = 800_000

      onResponse = async (resp: Response) => {
        try {
          if (capturedCount >= maxCaptured) return
          if (capturedChars >= maxChars) return
          const req = resp.request()
          const rt = req.resourceType()
          if (rt !== 'xhr' && rt !== 'fetch') return
          const headers = resp.headers()
          const ct = String((headers as any)['content-type'] ?? '').toLowerCase()
          const respUrl = resp.url()
          const looksJson = ct.includes('application/json') || /[?&]format=json\b/i.test(respUrl) || /\.json(\?|$)/i.test(respUrl)
          if (!looksJson) return

          const text = await resp.text()
          if (!text) return
          if (text.length > 200_000) return
          const trimmed = text.trim()
          if (!trimmed) return
          const parsed = JSON.parse(trimmed)
          networkJson.push({ url: respUrl, data: parsed })
          capturedCount++
          capturedChars += text.length
        } catch {}
      }

      onAbort = () => page.close().catch(() => {})
      signal.addEventListener('abort', onAbort)
      page.on('response', onResponse)

      await attemptDelay()
      if (attempt === 1) emitLog('info', `Worker：打开页面 ${url}`)

      const resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45_000 })
      const status = resp?.status()
      if (status === 403 || status === 429) {
        throw new Error(`HTTP ${status} blocked`)
      }

      if (/zhihu\.com\/hot/i.test(url)) {
        await page
          .waitForResponse((r) => /hot-lists/i.test(r.url()) && r.status() === 200, { timeout: 20_000 })
          .catch(() => {})
        await sleep(1200, signal)
      }

      // 某些站点在页面稳定后再做随机移动/滚动，能模拟用户；如果这一步导致异常，直接忽略，不影响抓取结果。
      await simulateHumanBehavior(page, signal).catch(() => {})

      let title = await page.title().catch(() => '')
      let html = await page.content()
      let finalUrl = page.url()

      if (looksLikeCaptchaOrBlock(title, html)) {
        // 先给 WAF 的 JS 质询自动通过的机会，别立即判死
        logCaptchaDiagnosis(emitLog, url, title, html, '浏览器/初始')
        emitLog('warn', `Worker：疑似拦截/质询页，等待其自动通过（最长 15s）：${url}`)
        const autoPassed = await waitChallengeAutoPass(page, signal, 15_000)
        title = await page.title().catch(() => '')
        html = await page.content()
        finalUrl = page.url()
        if (!autoPassed && looksLikeCaptchaOrBlock(title, html)) {
          throw new Error('captcha_or_block_page')
        }
        emitLog('info', `Worker：质询已自动通过，继续抓取 ${url}`)
      }

      try {
        const cookies = await context.cookies()
        const host = safeHost(finalUrl || url)
        if (host && cookies.length) saveSessionForHost(host, cookies as any)
      } catch {}

      pushRunEvent(runStats, { ts: nowTs(), host: safeHost(url), url, channel: 'browser', status: 'ok', ms: Date.now() - tStart }, options)
      return { url, finalUrl, title: normalizeWhitespace(title), html, networkJson }
    } catch (e: any) {
      lastErr = e
      const msg = typeof e?.message === 'string' ? e.message : String(e)
      pushRunEvent(runStats, { ts: nowTs(), host: safeHost(url), url, channel: 'browser', status: 'error', reason: classifyFailReason(msg), ms: Date.now() - tStart }, options)
      const retryable =
        isHttpBlockedError(msg) ||
        /captcha_or_block_page/i.test(msg) ||
        /net::err_|timeout/i.test(msg)

      if (proxy) proxyPool?.markBad(proxy)

      if (!retryable || attempt >= maxAttempts) {
        if (msg.includes('closed') || msg.includes('Navigation interrupted')) {
          throw new Error('Task canceled by user')
        }
        if (!mcpFailedPermanently && config?.mcp?.provider && isHttpBlockedError(msg)) {
          const mcpSnap = await fetchViaMcpBudgeted(url, config, signal, options, emitLog)
          if (mcpSnap) {
            emitLog('info', `Worker：浏览器模式失败后 MCP 兜底成功 ${url}`)
            return mcpSnap
          }
          mcpFailedPermanently = true
        }
        // 有头/人工兜底（机器优先）：每个 host 只触发一次，之后靠保存的 cookie 复用
        const hostKey = safeHost(url)
        if (/captcha_or_block/i.test(msg) && hostKey && !headfulCaptchaTriedHosts.has(hostKey) && !signal.aborted) {
          headfulCaptchaTriedHosts.add(hostKey)
          emitLog('warn', `Worker：无头浏览器仍受阻，改用有头浏览器重试（机器优先）：${url}`)
          const headful = await fetchViaHeadfulFallback(url, config, signal, emitLog, effectiveSession, false)
          if (headful) {
            pushRunEvent(runStats, { ts: nowTs(), host: safeHost(url), url, channel: 'browser', status: 'ok', ms: Date.now() - tStart }, options)
            emitLog('info', `Worker：有头浏览器重试成功 ${url}`)
            return headful
          }
          if ((options as any).__allowManualCaptcha !== false && !signal.aborted) {
            emitLog('warn', `Worker：有头重试未通过，弹出浏览器窗口等待人工验证（最后手段）：${url}`)
            const manual = await fetchViaHeadfulFallback(url, config, signal, emitLog, effectiveSession, true)
            if (manual) {
              pushRunEvent(runStats, { ts: nowTs(), host: safeHost(url), url, channel: 'browser', status: 'ok', ms: Date.now() - tStart }, options)
              return manual
            }
          }
        }
        emitLog('error', `Worker：页面抓取失败：${url}；原因=${shortErr(msg)}`)
        throw e
      }

      if (/zhihu\.com\/hot/i.test(url) && isHttpBlockedError(msg)) {
        emitLog('warn', 'Worker：知乎热榜浏览器受阻，不再重复浏览器重试')
        if (!mcpFailedPermanently && config?.mcp?.provider) {
          const mcpSnap = await fetchViaMcpBudgeted(url, config, signal, options, emitLog)
          if (mcpSnap) {
            emitLog('info', `Worker：知乎热榜 MCP 兜底成功 ${url}`)
            return mcpSnap
          }
          mcpFailedPermanently = true
        }
        emitLog('error', `Worker：页面抓取失败：${url}；原因=${shortErr(msg)}`)
        throw e
      }

      emitLog('warn', `Worker：抓取受阻，重试 ${attempt + 1}/${maxAttempts}；原因=${shortErr(msg)}`)
      await attemptDelay()
    } finally {
      try {
        if (onAbort) signal.removeEventListener('abort', onAbort)
      } catch {}
      try {
        if (onResponse) page.off('response', onResponse)
      } catch {}
      try {
        await page.close()
      } catch {}
      try {
        await context.close()
      } catch {}
    }
  }
  throw lastErr || new Error('unknown_worker_error')
}

