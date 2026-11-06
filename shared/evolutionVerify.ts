/**
 * 进化 promote 前黄金集/结构回放门禁（Phase 3 ④ Verify）
 */

import path from 'node:path'
import { existsSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

export type EvolutionVerifyResult = {
  ok: boolean
  agent: string
  gate: string
  reason?: string
  checks: Array<{ id: string; ok: boolean; detail?: string }>
}

export function isEvolutionVerifyEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return String(env.EVO_VERIFY_BEFORE_PROMOTE ?? '1').trim() !== '0'
}

function repoAgentDir(name: 'DB_Agent' | 'RAG_Agent' | 'Manager_Agent' | 'AI_admin_Agent'): string {
  const cwd = process.cwd()
  if (path.basename(cwd) === name) return cwd
  const inRepo = path.join(cwd, name)
  if (existsSync(path.join(inRepo, 'package.json')) || existsSync(path.join(inRepo, 'nuxt.config.ts'))) {
    return inRepo
  }
  return path.resolve(cwd, '..', name)
}

/** DB：metrics 结构回归（smoke-metrics 同等断言） */
export async function verifyDbEvolutionPromote(): Promise<EvolutionVerifyResult> {
  const checks: EvolutionVerifyResult['checks'] = []
  const dbRoot = repoAgentDir('DB_Agent')
  const prevCwd = process.cwd()
  try {
    process.chdir(dbRoot)
    process.env.ENABLE_METRICS_DIRECT = '1'
    const metricsMod = await import(pathToFileURL(path.join(dbRoot, 'utils', 'metrics_compiler.ts')).href)
    const domainMod = await import(pathToFileURL(path.join(dbRoot, 'utils', 'domain_patch.ts')).href)
    const { resolveMetricPatch } = metricsMod as typeof import('../DB_Agent/utils/metrics_compiler')
    const { invalidateDomainPatchCache } = domainMod as typeof import('../DB_Agent/utils/domain_patch')

    process.env.DB_AGENT_DOMAIN = 'generic'
    invalidateDomainPatchCache()
    const genericTable = resolveMetricPatch('当前数据库有多少张表', { intent: 'aggregation' })
    checks.push({
      id: 'generic_schema_table_count',
      ok: genericTable?.id === 'schema_table_count',
      detail: String(genericTable?.id || 'missing')
    })

    process.env.DB_AGENT_DOMAIN = 'p2026'
    invalidateDomainPatchCache()
    const personTotal = resolveMetricPatch('老人一共有多少人', { intent: 'aggregation' })
    checks.push({
      id: 'p2026_person_total_count',
      ok: personTotal?.id === 'person_total_count',
      detail: String(personTotal?.id || 'missing')
    })

    const footTrend = resolveMetricPatch('足压检测按月趋势', { intent: 'trend' })
    checks.push({
      id: 'p2026_foot_trend',
      ok: footTrend?.id === 'foot_pressure_monthly_trend',
      detail: String(footTrend?.id || 'missing')
    })
  } catch (e) {
    checks.push({ id: 'db_metrics_exception', ok: false, detail: String((e as Error)?.message || e) })
  } finally {
    process.chdir(prevCwd)
  }

  const ok = checks.every((c) => c.ok)
  return { ok, agent: 'db', gate: 'smoke_metrics', reason: ok ? undefined : 'metrics_regression', checks }
}

/** Manager：golden-smoke 结构校验 */
export async function verifyManagerEvolutionPromote(): Promise<EvolutionVerifyResult> {
  const checks: EvolutionVerifyResult['checks'] = []
  try {
    const fs = await import('node:fs/promises')
    const mgrRoot = repoAgentDir('Manager_Agent')
    const raw = await fs.readFile(path.join(mgrRoot, 'eval', 'golden-smoke.json'), 'utf8')
    const obj = JSON.parse(raw) as { cases?: unknown[] }
    const cases = Array.isArray(obj?.cases) ? obj.cases : []
    checks.push({ id: 'golden_smoke_cases', ok: cases.length >= 1, detail: `count=${cases.length}` })
    for (const c of cases.slice(0, 8)) {
      const row = c as { id?: string; user?: string; expect?: { intentHint?: string } }
      const id = String(row.id || '')
      checks.push({
        id: `case_${id || 'unknown'}`,
        ok: Boolean(String(row.user || '').trim() && row.expect?.intentHint),
        detail: row.expect?.intentHint
      })
    }
  } catch (e) {
    checks.push({ id: 'manager_golden_exception', ok: false, detail: String((e as Error)?.message || e) })
  }
  const ok = checks.every((c) => c.ok)
  return { ok, agent: 'manager', gate: 'golden_smoke', reason: ok ? undefined : 'golden_smoke_failed', checks }
}

/** RAG：HTML/PPTX 解析结构 smoke */
export async function verifyRagEvolutionPromote(): Promise<EvolutionVerifyResult> {
  const checks: EvolutionVerifyResult['checks'] = []
  try {
    const ragRoot = repoAgentDir('RAG_Agent')
    const htmlMod = await import(pathToFileURL(path.join(ragRoot, 'server', 'utils', 'html_text.ts')).href)
    const { stripHtmlToPlainText, looksLikeHtmlDocument } = htmlMod as typeof import('../RAG_Agent/server/utils/html_text')
    const sample = '<html><body><h1>探视制度</h1><p>14:00-16:00</p></body></html>'
    const plain = stripHtmlToPlainText(sample)
    checks.push({ id: 'html_strip', ok: plain.includes('探视制度') && !plain.includes('<p>') })
    const buf = Buffer.from(sample, 'utf-8')
    checks.push({ id: 'html_sniff', ok: looksLikeHtmlDocument(buf, 'policy.html') })
  } catch (e) {
    checks.push({ id: 'rag_smoke_exception', ok: false, detail: String((e as Error)?.message || e) })
  }
  const ok = checks.every((c) => c.ok)
  return { ok, agent: 'rag', gate: 'retrieval_smoke', reason: ok ? undefined : 'rag_smoke_failed', checks }
}

/** Admin：办公 golden batch 结构存在性 */
export async function verifyAdminEvolutionPromote(): Promise<EvolutionVerifyResult> {
  const checks: EvolutionVerifyResult['checks'] = []
  try {
    const fs = await import('node:fs/promises')
    const adminRoot = path.join(repoAgentDir('AI_admin_Agent'), 'backend', 'scripts')
    for (const name of ['smoke_batch0.py', 'smoke_batch1.py']) {
      const p = path.join(adminRoot, name)
      const st = await fs.stat(p).catch(() => null)
      checks.push({ id: name, ok: Boolean(st?.isFile()) })
    }
  } catch (e) {
    checks.push({ id: 'admin_golden_exception', ok: false, detail: String((e as Error)?.message || e) })
  }
  const ok = checks.every((c) => c.ok)
  return { ok, agent: 'admin', gate: 'office_golden', reason: ok ? undefined : 'admin_golden_missing', checks }
}

export async function verifyBeforePromote(
  agent: 'db' | 'manager' | 'rag' | 'admin',
  env: NodeJS.ProcessEnv = process.env
): Promise<EvolutionVerifyResult> {
  if (!isEvolutionVerifyEnabled(env)) {
    return { ok: true, agent, gate: 'disabled', checks: [{ id: 'verify_disabled', ok: true }] }
  }
  if (agent === 'db') return verifyDbEvolutionPromote()
  if (agent === 'manager') return verifyManagerEvolutionPromote()
  if (agent === 'rag') return verifyRagEvolutionPromote()
  return verifyAdminEvolutionPromote()
}
