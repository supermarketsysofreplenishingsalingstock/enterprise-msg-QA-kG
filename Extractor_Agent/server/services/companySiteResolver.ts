/**
 * 公司官网解析：公司名（+条件特征）→ Bing 搜索 → 候选官网列表。
 *
 * 用途：
 * 1) 前端「公司爬取面板」的搜索按钮（POST /api/company/search）；
 * 2) 爬虫运行前自动解析（crawlerAgent.ts 内嵌调用）：
 *    - 找到 1 个候选 → 直接作为种子继续；
 *    - 找到多个候选 → 返回 needs_clarification（reason=company_candidates），让用户选择。
 */
import { fetchHtml } from '../core/fetch/runtime'
import { extractBingSerpLinks } from '../core/extract/generic'

export type CompanyCandidate = {
  title: string
  url: string
  host: string
  excerpt?: string
  score: number
  /** 内部字段：名称匹配强度（full=全名 / core=核心品牌名 / null=无匹配） */
  nameMatch?: 'full' | 'core' | null
}

const NON_SITE_HOST =
  /(?:^|\.)(?:bing\.com|microsoft\.com|baike\.baidu\.com|baidu\.com|zhihu\.com|zhidao\.baidu\.com|tieba\.baidu\.com|weibo\.com|xueqiu\.com|douyin\.com|tiktok\.com|youtube\.com|facebook\.com|linkedin\.com|qcc\.com|tianyancha\.com|qichacha\.com|amap\.com|map\.baidu\.com|docin\.com|wenku\.baidu\.com|doc88\.com|book118\.com|cnki\.net|so\.com|sogou\.com|sohu\.com|163\.com|sina\.com\.cn|qq\.com|alibaba\.com|1688\.com|tmall\.com|taobao\.com|jd\.com|pinduoduo\.com|csdn\.net|jianshu\.com|bilibili\.com|ixigua\.com|toutiao\.com|xigua\.com)$/i

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.toLowerCase().replace(/^www\./, '')
  } catch {
    return ''
  }
}

function buildQueries(company: string, traits: string[]): string[] {
  const t = traits.filter(Boolean).slice(0, 4)
  const base = String(company || '').trim()
  const core = coreBrand(base)
  // 带引号 + 核心品牌名命中率最高（Bing 对无 Cookie 请求易把长公司名拆词跑偏）
  const queries = [`"${core}" 官网`]
  if (core !== base) queries.push(`"${base}" 官网`)
  else queries.push(`${base} 官网`)
  if (t.length) queries.push(`"${core}" 官网 ${t.join(' ')}`)
  queries.push(`${core} 网站 公司主页`)
  return queries.slice(0, 4)
}

/** 核心品牌名：去掉地域前缀与公司通用后缀，如「杭州海康威视数字技术股份有限公司」→「海康威视数字技术」 */
export function coreBrand(name: string): string {
  let s = String(name || '').trim()
  s = s.replace(
    /^(?:中国|中华|浙江|杭州|上海|北京|深圳|广州|江苏|广东|山东|四川|湖北|湖南|河南|安徽|福建|天津|重庆|陕西|西安|南京|苏州|宁波|无锡|武汉|成都|青岛|大连|厦门|东莞|佛山|珠海|合肥|长沙|郑州)/,
    '',
  )
  s = s.replace(/(?:股份有限公司|有限责任公司|有限公司|控股公司|公司|集团|企业)$/, '')
  s = s.trim()
  return s || String(name || '').trim()
}

function matchStrength(value: string, fullLower: string, coreLower: string): 'full' | 'core' | null {
  const v = String(value || '').toLowerCase()
  if (fullLower && v.includes(fullLower)) return 'full'
  if (coreLower && coreLower.length >= 2 && v.includes(coreLower)) return 'core'
  return null
}

/** Bing 搜索公司官网，返回去重后的候选列表（按相关度排序） */
export async function searchCompanySites(
  companyName: string,
  traits: string[],
  config: any,
  signal: AbortSignal,
  emitLog?: (level: 'info' | 'warn' | 'error', message: string) => void,
): Promise<CompanyCandidate[]> {
  const name = String(companyName || '').trim()
  if (!name) return []
  const queries = buildQueries(name, traits || [])
  const byHost = new Map<string, CompanyCandidate>()

  for (const q of queries) {
    if (signal.aborted) break
    const searchUrl = `https://www.bing.com/search?q=${encodeURIComponent(q)}&count=20&setlang=zh-CN`
    let html = ''
    try {
      emitLog?.('info', `公司解析：Bing 搜索「${q}」`)
      html = await fetchHtml(searchUrl, config, signal)
    } catch (e: any) {
      emitLog?.('warn', `公司解析：搜索失败（${String(e?.message ?? e).slice(0, 80)}），尝试下一查询`)
      continue
    }
    const items = extractBingSerpLinks(html, searchUrl, 20)
    for (const it of items) {
      const url = String(it?.url ?? '').trim()
      if (!/^https?:\/\//i.test(url)) continue
      if (/bing\.com|microsoft\.com\/bing/i.test(url)) continue
      const host = hostOf(url)
      if (!host || NON_SITE_HOST.test(`.${host}`)) continue
      const title = String(it?.title ?? '').replace(/_?[-–]?(?:百度|Bing|必应|搜狗).*$/, '').trim()
      const excerpt = String(it?.excerpt ?? '').slice(0, 160)
      // 相关度打分：名称匹配（全名 > 核心品牌名）是主导因子，官网词只是辅助
      const nameLower = name.toLowerCase()
      const coreLower = coreBrand(name).toLowerCase()
      const tMatch = matchStrength(title, nameLower, coreLower)
      const uMatch = matchStrength(url, nameLower, coreLower)
      const eMatch = matchStrength(excerpt, nameLower, coreLower)
      const nameMatch: 'full' | 'core' | null =
        tMatch === 'full' || uMatch === 'full' || eMatch === 'full'
          ? 'full'
          : tMatch || uMatch || eMatch
      let score = 0
      if (tMatch === 'full') score += 5
      else if (tMatch === 'core') score += 4
      if (uMatch === 'full') score += 4
      else if (uMatch === 'core') score += 3
      if (eMatch === 'full') score += 1
      else if (eMatch === 'core') score += 1
      if (/官网|首页|official/i.test(title)) score += 2
      if (/\.(?:com|cn|net|com\.cn|net\.cn|org|org\.cn)$/i.test(host)) score += 1
      const prev = byHost.get(host)
      if (!prev || score > prev.score) {
        byHost.set(host, { title: title || host, url, host, excerpt, score, nameMatch })
      } else if (score === prev.score && /index\.(?:html?|aspx?|php)$/i.test(url)) {
        // 同分时偏好首页形态的 URL
        byHost.set(host, { title: title || host, url, host, excerpt, score, nameMatch })
      }
    }
  }

  const list = [...byHost.values()]
  // 有名称匹配（全名或核心品牌名）的候选优先：若存在任何 nameMatch 候选，剔除无关站点
  const named = list.filter((c) => c.nameMatch)
  const pool = named.length ? named : list
  // 少量主机名做「站点家族」合并：如 a.eb-tech.cn / eb-tech.cn 保留得分最高的
  const rootOf = (h: string) => h.split('.').slice(-2).join('.')
  const bestByRoot = new Map<string, CompanyCandidate>()
  for (const c of pool) {
    const root = rootOf(c.host)
    const prev = bestByRoot.get(root)
    if (!prev || c.score > prev.score) bestByRoot.set(root, c)
  }
  return [...bestByRoot.values()].sort((a, b) => b.score - a.score).slice(0, 6)
}

/** 从任务文本中提取公司名（如「帮我爬取羿贝科技公司的官网」→「羿贝科技公司」） */
export function extractCompanyNameFromTask(task: string): string | null {
  const t = String(task ?? '')
  const patterns = [
    /(?:爬取|抓取|采集|获取|分析)[：:]?\s*([^\s，。,,.：:]{2,20}?(?:公司|企业|集团|厂|科技|有限))[^，。]*官网?/,
    /(?:爬取|抓取|采集|获取|分析)[：:]?\s*([^\s，。,,.：:]{2,24}?(?:公司|企业|集团))/,
    /([^\s，。,,.：:]{2,20}?(?:公司|企业|集团))(?:的)?(?:官网|网站|主页|信息|资料)/,
  ]
  for (const p of patterns) {
    const m = t.match(p)
    if (m?.[1]) return m[1].trim()
  }
  return null
}

/** 是否处于公司爬取模式：显式开关、公司名，或任务文本可识别出公司 */
export function detectCompanyCrawl(task: string, options: any): { companyName: string; traits: string[] } | null {
  const explicitName = String(options?.companyName ?? '').trim()
  if (options?.companyCrawl === true && explicitName) {
    return { companyName: explicitName, traits: parseTraits(options?.companyTraits) }
  }
  const fromTask = extractCompanyNameFromTask(task)
  if (fromTask && !/https?:\/\//i.test(task)) {
    return { companyName: fromTask, traits: parseTraits(options?.companyTraits) }
  }
  return null
}

function parseTraits(v: unknown): string[] {
  const arr = Array.isArray(v) ? v : String(v ?? '').split(/[,，、;；]/)
  return arr.map((x) => String(x ?? '').trim()).filter(Boolean).slice(0, 6)
}
