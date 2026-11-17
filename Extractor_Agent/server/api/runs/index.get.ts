/**
 * GET /api/runs —— 历史爬取记录摘要列表（新在前）
 * 每次调用顺带做一次存量 crawler_results.json 的幂等迁移导入，让旧结果也能看到
 */
import { listRunArchive, importLegacyResult } from '../../utils/run_archive'

export default defineEventHandler(async () => {
  try {
    await importLegacyResult()
  } catch {}
  return { runs: listRunArchive() }
})
