/**
 * URL 抽取与清洗。
 *
 * 背景：早先用 /https?:\/\/[^\s]+/ 从任务文本里取地址，会把紧跟在地址后的中文
 * 一起吞进去，例如「帮我爬取https://example.com/的信息」会得到
 * https://example.com/的信息，请求必然 404。
 *
 * 这里只接受 RFC 3986 允许的 ASCII 字符，并在取到后做 host 校验；
 * 中文说明文字、全角标点、句末标点都会被自然截断。
 *
 * 另外支持“裸主机”写法（不带 http(s)://）：
 *   「爬取156.233.229.186主页」→ https://156.233.229.186/
 *   「抓一下 example.com/news」→ https://example.com/news
 */

/** URL 允许的字符集（RFC 3986 + 常见 %XX 编码） */
const HTTP_URL_PATTERN = /https?:\/\/[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+/i

/**
 * 裸主机：IP / localhost / 带点域名（TLD 至少 2 个字母），可带端口与路径。
 * 前置断言避免把邮箱（a@b.com）、已有 scheme 的地址、版本号（1.2.3）误识别。
 */
const BARE_HOST_PATTERN =
  /(?<![A-Za-z0-9@._\-/])(localhost|\d{1,3}(?:\.\d{1,3}){3}|(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,24})(:\d{1,5})?(\/[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*)?/i

/** 这些后缀更像文件名而不是域名，避免把 readme.md / app.js 当成站点 */
const FILE_LIKE_TLDS = new Set([
  'md', 'js', 'ts', 'jsx', 'tsx', 'vue', 'py', 'json', 'html', 'htm', 'txt', 'css',
  'yml', 'yaml', 'xml', 'csv', 'log', 'png', 'jpg', 'jpeg', 'gif', 'svg', 'pdf',
  'zip', 'rar', 'exe', 'mp3', 'mp4', 'sh', 'bat', 'ini', 'toml', 'lock', 'map',
])

/** 句末常见标点，不属于地址本身 */
const TRAILING_PUNCTUATION = /[.,;:!?'"]+$/
const TRAILING_BRACKETS = /[)\]}]+$/

function countChar(text: string, ch: string) {
  let n = 0
  for (const c of text) if (c === ch) n += 1
  return n
}

/**
 * 去掉“粘”在地址末尾的标点。
 * 括号只在明显不配对时才去掉，避免破坏 Wikipedia 一类的 ..._(bar) 路径。
 */
function trimTrailingPunctuation(raw: string) {
  let out = raw
  for (;;) {
    const before = out
    out = out.replace(TRAILING_PUNCTUATION, '')
    const closers = out.match(TRAILING_BRACKETS)?.[0] ?? ''
    for (const ch of closers) {
      const opener = ch === ')' ? '(' : ch === ']' ? '[' : '{'
      if (countChar(out, ch) > countChar(out, opener)) out = out.slice(0, -1)
      else break
    }
    if (out === before) return out
  }
}

function validateHttpUrl(raw: string): string | null {
  const candidate = trimTrailingPunctuation(String(raw ?? '').trim())
  if (!candidate) return null
  try {
    const u = new URL(candidate)
    if (!u.hostname) return null
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null
    // 统一规范化（补末尾 /、小写主机名），避免同一个地址出现多种写法影响去重
    return u.toString()
  } catch {
    return null
  }
}

/** 裸主机是否像一个真实站点（排除版本号、文件名、非法 IP） */
function isPlausibleBareHost(host: string) {
  const h = host.toLowerCase()
  if (h === 'localhost') return true
  if (/^\d{1,3}(?:\.\d{1,3}){3}$/.test(h)) {
    return h.split('.').every((octet) => Number(octet) <= 255)
  }
  const labels = h.split('.')
  const tld = labels[labels.length - 1] ?? ''
  if (!/^[a-z]{2,24}$/.test(tld)) return false
  if (FILE_LIKE_TLDS.has(tld)) return false
  return true
}

/**
 * 从任意文本中抽取第一个可执行地址；没有时返回 null。
 * 「帮我爬取https://a.com/的信息」→ https://a.com/
 * 「帮我爬取156.233.229.186主页的信息！」→ https://156.233.229.186/
 */
export function extractHttpUrl(text: unknown): string | null {
  const raw = String(text ?? '')
  const withScheme = raw.match(HTTP_URL_PATTERN)
  if (withScheme) {
    const cleaned = validateHttpUrl(withScheme[0])
    if (cleaned) return cleaned
  }

  const bare = raw.match(BARE_HOST_PATTERN)
  if (!bare) return null
  const host = String(bare[1] ?? '')
  if (!isPlausibleBareHost(host)) return null
  const port = String(bare[2] ?? '')
  const rest = String(bare[3] ?? '')
  return validateHttpUrl(`https://${host}${port}${rest}`)
}

/**
 * 清洗单个地址（用于 Manager / LLM 给出的种子）：
 * 已经是干净地址时原样返回，尾部带中文说明或标点时截断。
 */
export function sanitizeHttpUrl(input: unknown): string {
  const text = String(input ?? '').trim()
  if (!text) return ''
  return extractHttpUrl(text) ?? ''
}
