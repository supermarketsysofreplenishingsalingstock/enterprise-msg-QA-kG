/**
 * GET /api/runs/:id —— 单次爬取运行的完整归档结果
 * ?meta=1 时剥离重字段（stats._events / kbPages 正文），只回概要与条目，供详情面板快速加载
 */
import { readRunArchive } from '../../utils/run_archive'

export default defineEventHandler((event) => {
  const id = String(getRouterParam(event, 'id') ?? '')
  const data = readRunArchive(id)
  if (!data) {
    throw createError({ statusCode: 404, statusMessage: 'run not found' })
  }
  const metaOnly = String(getQuery(event).meta ?? '') === '1'
  if (!metaOnly) return data
  const cloned: any = { ...data }
  if (cloned.stats && typeof cloned.stats === 'object') {
    cloned.stats = { ...cloned.stats, _events: undefined, _routeLog: undefined }
  }
  if (Array.isArray(cloned.kbPages)) {
    cloned.kbPages = cloned.kbPages.map((p: any) => ({ url: p?.url, title: p?.title, status: p?.status }))
  }
  return cloned
})
