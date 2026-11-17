/**
 * GET /api/runs/:id/image/:idx —— 返回某次历史爬取归档里的图片文件
 * 安全：只允许读取 .data/images 目录下的文件，防止路径穿越
 */
import { readFile } from 'node:fs/promises'
import { existsSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { readRunArchive } from '../../../../utils/run_archive'

export default defineEventHandler(async (event) => {
  const id = String(getRouterParam(event, 'id') ?? '')
  const idx = Number(getRouterParam(event, 'idx'))
  const data = readRunArchive(id)
  if (!data || !Array.isArray(data.images) || !Number.isInteger(idx) || idx < 0 || idx >= data.images.length) {
    throw createError({ statusCode: 404, statusMessage: 'image not found' })
  }
  const entry = data.images[idx]
  const filePath = String(entry?.path ?? '')
  if (!filePath) {
    throw createError({ statusCode: 404, statusMessage: 'image path missing' })
  }
  const abs = resolve(filePath)
  const allowedRoot = resolve(process.cwd(), '.data', 'images')
  if (!abs.startsWith(allowedRoot + '\\') && !abs.startsWith(allowedRoot + '/') && abs !== allowedRoot) {
    throw createError({ statusCode: 403, statusMessage: 'image path outside images dir' })
  }
  if (!existsSync(abs)) {
    throw createError({ statusCode: 404, statusMessage: 'image file missing (可能已被清理)' })
  }
  const buf = await readFile(abs)
  const contentType = String(entry?.contentType ?? '') || 'application/octet-stream'
  setResponseHeaders(event, {
    'content-type': contentType,
    'cache-control': 'public, max-age=86400',
    'content-length': String(statSync(abs).size)
  })
  return buf
})
