/**
 * 自签名 / 证书与主机名不匹配站点的 TLS 放行策略。
 *
 * 典型场景：站点直接部署在裸 IP 上（https://1.2.3.4/），证书里没有该 IP，
 * Node fetch 会抛 ERR_TLS_CERT_ALTNAME_INVALID，Playwright 会报
 * net::ERR_CERT_COMMON_NAME_INVALID。这类站点本身无法通过标准校验，但内容
 * 常常是内部工具页 / 教学站点，抓取时需要放行。
 *
 * 默认策略（mode=auto）：只对「IP 直连 / localhost / .local」放开，
 * 普通公网域名仍然严格校验证书。
 *
 * 环境变量：
 *   EXTRACTOR_INSECURE_TLS=auto|1|0     1=全放开  0=全禁止  auto=默认
 *   EXTRACTOR_INSECURE_TLS_HOSTS=a,b    额外放行的主机（支持子域）
 */

const TLS_ERROR_PATTERN =
  /(ERR_TLS_|ERR_CERT_|CERT_|SELF_SIGNED|UNABLE_TO_VERIFY|DEPTH_ZERO|ALTNAME|COMMON_NAME|CERTIFICATE)/i

const TRUTHY = new Set(['1', 'true', 'on', 'yes', 'always'])
const FALSY = new Set(['0', 'false', 'off', 'no', 'never'])

export type TlsPolicyDecision = { allow: boolean; reason: string }

/** 判断错误是否属于“证书不可信”这一类（Node fetch 会把真实原因藏在 cause.code 里） */
export function isTlsCertificateError(err: unknown): boolean {
  const e = err as any
  if (!e) return false
  const candidates = [e.code, e.errno, e.cause?.code, e.cause?.errno, e.message, e.cause?.message]
  for (const c of candidates) {
    if (c && TLS_ERROR_PATTERN.test(String(c))) return true
  }
  return false
}

function isIPv4Literal(host: string) {
  return /^\d{1,3}(?:\.\d{1,3}){3}$/.test(host)
}

function isIPv6Literal(host: string) {
  return /^\[?[0-9a-f:]+\]?$/i.test(host) && host.includes(':')
}

/** 该 URL 是否允许“跳过证书校验” */
export function insecureTlsPolicy(url: string): TlsPolicyDecision {
  const mode = String(process.env.EXTRACTOR_INSECURE_TLS ?? '').trim().toLowerCase()
  let host = ''
  try {
    host = new URL(String(url ?? '')).hostname.toLowerCase()
  } catch {
    return { allow: false, reason: 'invalid_url' }
  }

  const allowList = String(process.env.EXTRACTOR_INSECURE_TLS_HOSTS ?? '')
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean)
  if (allowList.some((h) => host === h || host.endsWith(`.${h}`))) {
    return { allow: true, reason: 'host_allowlist' }
  }

  if (FALSY.has(mode)) return { allow: false, reason: 'disabled_by_env' }
  if (TRUTHY.has(mode)) return { allow: true, reason: 'forced_by_env' }

  if (host === 'localhost' || host === '::1' || host === '[::1]' || host.endsWith('.local')) {
    return { allow: true, reason: 'loopback' }
  }
  if (isIPv4Literal(host) || isIPv6Literal(host)) return { allow: true, reason: 'ip_literal' }
  return { allow: false, reason: 'public_domain' }
}

let insecureAgentPromise: Promise<any> | null = null
const warnedHosts = new Set<string>()

function getInsecureAgent() {
  if (!insecureAgentPromise) {
    insecureAgentPromise = (async () => {
      const undici: any = await import('undici')
      return new undici.Agent({ connect: { rejectUnauthorized: false } })
    })()
  }
  return insecureAgentPromise
}

/**
 * 用 undici 的 dispatcher 发起一次“不校验证书”的请求。
 * 结果体先读成 Buffer 再包成标准 Response，避免调用方清理超时信号后 body 被中断。
 */
export async function fetchWithInsecureTls(
  input: string,
  init: any,
  signal?: AbortSignal
): Promise<Response> {
  const host = (() => {
    try {
      return new URL(input).host
    } catch {
      return input
    }
  })()
  if (!warnedHosts.has(host)) {
    warnedHosts.add(host)
    console.warn(`[fetch] 已对 ${host} 跳过 TLS 证书校验（自签名/证书不匹配）`)
  }

  const undici: any = await import('undici')
  const agent = await getInsecureAgent()
  const res = await undici.fetch(input, { ...init, dispatcher: agent, signal })
  const buf = Buffer.from(await res.arrayBuffer())
  const headers = new Headers()
  res.headers.forEach((value: string, key: string) => headers.set(key, value))
  return new Response(buf, { status: res.status, statusText: res.statusText, headers }) as unknown as Response
}
