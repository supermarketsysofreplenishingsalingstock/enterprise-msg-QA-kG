import { autoPromoteEligiblePatches, promotePromptPatch } from '../../utils/prompt_evolution'
import { getExtractorAgentEnv } from '../../utils/extractor_agent_env'

export default defineEventHandler(async (event) => {
  const body = (await readBody(event).catch(() => null)) as {
    patchId?: string
    auto?: boolean
    minHits?: number
  } | null

  if (body?.auto) {
    const minHits = Number.isFinite(body.minHits) ? Number(body.minHits) : getExtractorAgentEnv().promptPromoteMinHits
    const promoted = autoPromoteEligiblePatches(minHits)
    return { ok: true, promoted, count: promoted.length }
  }

  const patchId = String(body?.patchId ?? '').trim()
  if (!patchId) {
    throw createError({ statusCode: 400, statusMessage: '请提供 patchId 或 auto=true' })
  }

  const res = promotePromptPatch(patchId)
  if (!res.ok) {
    throw createError({ statusCode: 400, statusMessage: res.reason })
  }
  return { ok: true, hintId: res.hintId }
})
