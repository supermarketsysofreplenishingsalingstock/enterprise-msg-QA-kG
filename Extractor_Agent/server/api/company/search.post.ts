/**
 * 公司官网候选搜索：公司名（+条件特征）→ Bing → 候选网站列表。
 * 供前端「公司爬取面板」预检使用（不启动爬取）。
 */
import { searchCompanySites } from '../../services/companySiteResolver'
import { applyPlatformRuntimeOverrides } from '../../utils/platform_config'

export default defineEventHandler(async (event) => {
  const body = await readBody(event).catch(() => ({}))
  const companyName = String(body?.companyName ?? body?.company ?? '').trim()
  if (!companyName) {
    return { ok: false, error: 'companyName 不能为空', candidates: [] }
  }
  const traits: string[] = Array.isArray(body?.traits)
    ? body.traits.map((x: unknown) => String(x ?? '').trim()).filter(Boolean)
    : String(body?.traits ?? '')
        .split(/[,，、;；]/)
        .map((x: unknown) => String(x ?? '').trim())
        .filter(Boolean)
  const config = await applyPlatformRuntimeOverrides(useRuntimeConfig(event) as any)
  const signal = AbortSignal.timeout(60_000)
  try {
    const candidates = await searchCompanySites(companyName, traits, config, signal)
    return {
      ok: true,
      companyName,
      traits,
      candidates,
      message:
        candidates.length > 1
          ? `找到 ${candidates.length} 个候选网站，请选择要爬取的站点（可多选或全部爬取）`
          : candidates.length === 1
            ? `找到唯一候选官网：${candidates[0].url}`
            : '未找到候选网站，请检查公司名称或补充条件特征',
    }
  } catch (e: any) {
    return { ok: false, error: String(e?.message ?? e).slice(0, 240), candidates: [] }
  }
})
