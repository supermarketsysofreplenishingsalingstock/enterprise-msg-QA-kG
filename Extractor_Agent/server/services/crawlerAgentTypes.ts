export type CrawlerAgentOptions = {
  maxPages?: number
  maxItems?: number
  delayMinMs?: number
  delayMaxMs?: number
  useBrowser?: boolean
  maxConcurrency?: number // For distributed crawling
  headless?: boolean
  proxyFilePath?: string
  outputJsonPath?: string
  resumeId?: string
  checkpointIntervalMs?: number
  rateLimit?: {
    tokensPerInterval?: number
    intervalMs?: number
    backoffBaseMs?: number
    backoffMaxMs?: number
    perHostOverrides?: Record<string, { tokensPerInterval?: number; intervalMs?: number }>
  }
  robotsPolicy?: 'strict' | 'warn' | 'off'
  /** 整站爬取：从种子出发跟踪同站链接（BFS），按页面产出条目 */
  siteCrawl?: boolean
  /** 整站爬取最大链接深度（种子为 0） */
  maxDepth?: number
  /** 整站边界：auto=按种子页结构自动判断；host=仅同主机；linked=种子页外链也纳入（上限 10 个主机） */
  siteCrawlScope?: 'auto' | 'host' | 'linked'
  /** 单页模式：single=整页一条（title/url/excerpt）；list=只抓列表；auto=有列表取列表，无列表产出整页一条 */
  pageMode?: 'auto' | 'single' | 'list'
}

export type AgentConfig = {
  qwenApiKey?: string
  qwenBaseUrl?: string
  qwenModel?: string
  qwenVlModel?: string
  qwenEnableThinking?: boolean
  plannerMode?: 'auto' | 'llm' | 'heuristic'
  agentMode?: 'smart' | 'llm' | 'rules'
  extractorMode?: 'adaptive' | 'fast' | 'deep'
  crawler?: { userAgent?: string; proxyFilePath?: string }
  mcp?: {
    provider?: 'scrapingant' | 'zenrows' | 'scraperapi' | 'generic'
    apiKey?: string
    baseUrl?: string
    queryParamKey?: string
    headerKey?: string
    render?: boolean
    country?: string
  }
  skills?: Array<{
    host?: string
    includes?: string
    urlTemplate: string
    tokenHeader?: string
    token?: string
  }>
}

export type EmitEvent =
  | { type: 'log'; payload: { level: 'info' | 'warn' | 'error'; message: string; ts: number } }
  | { type: 'progress'; payload: { stage: string; done?: number; total?: number } }

export type RunParams = {
  task: string
  options?: CrawlerAgentOptions
  config: AgentConfig
  signal: AbortSignal
  emit: (evt: EmitEvent) => void
}

