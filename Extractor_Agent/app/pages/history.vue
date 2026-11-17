<template>
  <div class="container">
    <VideoBackground />
    <div class="content-wrapper">
      <header class="main-header">
        <div class="header-row">
          <h1 class="title">历史爬取</h1>
          <NuxtLink to="/" class="capability-badge">← 返回爬虫控制台</NuxtLink>
        </div>
        <div class="glitch-line"></div>
        <p class="subtitle">每次任务运行自动归档 · 点击行展开详情 · 图片可直接预览</p>
      </header>

      <div class="panel history-panel">
        <div class="panel-header">
          <span class="panel-icon">◈</span>
          爬取记录
          <button class="btn btn-secondary btn-small history-refresh" :disabled="loading" @click="loadHistory">
            {{ loading ? '加载中…' : '刷新' }}
          </button>
        </div>
        <div class="panel-content custom-scrollbar">
          <div v-if="!runs.length && !loading" class="history-empty">// 暂无历史记录——每次任务运行结束后会自动归档到这里</div>
          <div v-for="r in runs" :key="r.id" class="history-block">
            <div class="history-row" :class="{ active: detailId === r.id }" @click="toggleDetail(r.id)">
              <span class="history-time">{{ formatRunTime(r.finishedAt || r.startedAt) }}</span>
              <span class="history-task" :title="r.task">{{ r.task || '(无任务描述)' }}</span>
              <span class="history-pills">
                <span v-if="r.status" class="item-pill">{{ r.status }}</span>
                <span v-if="r.items" class="item-pill">条目 {{ r.items }}</span>
                <span v-if="r.images" class="item-pill">图片 {{ r.images }}</span>
                <span v-if="r.attachments" class="item-pill">文件 {{ r.attachments }}</span>
                <span v-if="r.seedHost" class="item-pill">{{ r.seedHost }}</span>
              </span>
              <a class="btn btn-secondary btn-small history-json" :href="`/api/runs/${r.id}`" target="_blank" rel="noopener noreferrer" @click.stop>JSON</a>
            </div>
            <div v-if="detailId === r.id" class="history-detail custom-scrollbar">
              <div v-if="detailLoading" class="history-empty">加载详情中…</div>
              <template v-else-if="detail">
                <div class="meta-grid">
                  <div class="meta-item">
                    <span class="meta-label">开始时间</span>
                    <span class="meta-value">{{ formatRunTime(detail.startedAt) }}</span>
                  </div>
                  <div class="meta-item">
                    <span class="meta-label">结束时间</span>
                    <span class="meta-value">{{ formatRunTime(detail.finishedAt) }}</span>
                  </div>
                  <div class="meta-item">
                    <span class="meta-label">结果状态</span>
                    <span class="meta-value">{{ detail.status || '--' }}</span>
                  </div>
                  <div class="meta-item">
                    <span class="meta-label">条目数</span>
                    <span class="meta-value">{{ (detail.items || []).length }}</span>
                  </div>
                </div>

                <div v-if="(detail.images || []).length" class="items-block">
                  <div class="items-title">爬取图片 {{ detail.images.length }} 张（点击放大）</div>
                  <div class="img-grid">
                    <div v-for="(im, idx) in detail.images" :key="`img_${r.id}_${idx}`" class="img-cell">
                      <button type="button" class="img-thumb-btn" @click="openViewer(idx)">
                        <img
                          class="img-thumb"
                          :src="`/api/runs/${r.id}/image/${idx}`"
                          loading="lazy"
                          :alt="im.filename || im.url || '图片'"
                          @error="onImgError($event)"
                        />
                      </button>
                      <a v-if="im.url" class="img-src" :href="im.url" target="_blank" rel="noopener noreferrer" :title="im.url">
                        {{ im.filename || '图片' + (idx + 1) }}
                      </a>
                      <span v-else class="img-src">{{ im.filename || '图片' + (idx + 1) }}</span>
                    </div>
                  </div>
                </div>

                <div v-if="(detail.items || []).length" class="items-block">
                  <div class="items-title">抓取条目 {{ detail.items.length }} 条</div>
                  <div class="items-list">
                    <div v-for="(it, idx) in detail.items" :key="`h_${r.id}_${idx}`" class="item-row">
                      <div class="item-main">
                        <span v-if="it.rank != null" class="item-rank">#{{ it.rank }}</span>
                        <a v-if="safeHttpUrl(it.url)" class="item-link" :href="safeHttpUrl(it.url)" target="_blank" rel="noopener noreferrer">
                          {{ it.title || it.name || it.url }}
                        </a>
                        <span v-else class="item-text">{{ it.title || it.name || it.url || '(无标题)' }}</span>
                      </div>
                      <div class="item-sub">
                        <span v-if="it.rating != null" class="item-pill">评分 {{ it.rating }}</span>
                        <span v-if="it.artist" class="item-pill">{{ it.artist }}</span>
                        <span v-if="it.quote" class="item-dim">{{ it.quote }}</span>
                        <span v-else-if="it.info" class="item-dim">{{ it.info }}</span>
                      </div>
                    </div>
                  </div>
                </div>
                <div v-else-if="!(detail.images || []).length" class="history-empty">// 本次运行没有结构化条目（可能抓取失败）</div>
              </template>
              <div v-else class="history-empty">// 详情加载失败，归档文件可能已被清理</div>
            </div>
          </div>
        </div>
      </div>

      <!-- 图片放大查看 -->
      <div v-if="viewerIdx >= 0 && detail" class="img-viewer-mask" @click="closeViewer">
        <div class="img-viewer-body" @click.stop>
          <img class="img-viewer-img" :src="`/api/runs/${detailId}/image/${viewerIdx}`" :alt="currentImage?.filename || '图片'" @error="onViewerError" />
          <div class="img-viewer-meta">
            <span>{{ currentImage?.filename || '--' }}</span>
            <span v-if="currentImage?.size" class="img-viewer-size">{{ Math.round((currentImage.size || 0) / 1024) }} KB</span>
            <a v-if="currentImage?.url" :href="currentImage.url" target="_blank" rel="noopener noreferrer">来源页面</a>
            <button type="button" class="btn btn-secondary btn-small" @click="closeViewer">关闭</button>
          </div>
        </div>
      </div>

      <footer class="main-footer">
        <div class="footer-line"></div>
        <p>NUXT · LANGGRAPH · SEED-FIRST · QWEN</p>
      </footer>
    </div>
  </div>
</template>

<script setup lang="ts">
import VideoBackground from '../components/VideoBackground.vue'

const runs = ref<any[]>([])
const loading = ref(false)
const detailId = ref('')
const detail = ref<any>(null)
const detailLoading = ref(false)
const viewerIdx = ref(-1)

async function loadHistory() {
  loading.value = true
  try {
    const res = await $fetch<{ runs?: any[] }>('/api/runs')
    runs.value = Array.isArray(res?.runs) ? res.runs : []
  } catch {
    runs.value = []
  } finally {
    loading.value = false
  }
}

async function toggleDetail(id: string) {
  if (detailId.value === id) {
    detailId.value = ''
    detail.value = null
    viewerIdx.value = -1
    return
  }
  detailId.value = id
  detail.value = null
  viewerIdx.value = -1
  detailLoading.value = true
  try {
    detail.value = await $fetch<any>(`/api/runs/${id}`, { query: { meta: 1 } })
  } catch {
    detail.value = null
  } finally {
    detailLoading.value = false
  }
}

function formatRunTime(ts: string) {
  const t = Date.parse(String(ts || ''))
  if (!Number.isFinite(t)) return String(ts || '--')
  const d = new Date(t)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function safeHttpUrl(u: any) {
  const s = String(u ?? '')
  return /^https?:\/\//i.test(s) ? s : ''
}

const currentImage = computed(() => {
  if (!detail.value || viewerIdx.value < 0) return null
  return detail.value.images?.[viewerIdx.value] ?? null
})

function openViewer(idx: number) {
  viewerIdx.value = idx
}
function closeViewer() {
  viewerIdx.value = -1
}
function onImgError(e: Event) {
  const el = e.target as HTMLElement
  if (el) {
    el.style.display = 'none'
  }
}
function onViewerError(e: Event) {
  const el = e.target as HTMLElement
  if (el) {
    el.src =
      'data:image/svg+xml;utf8,' +
      encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><rect width="100%" height="100%" fill="#222"/><text x="50%" y="50%" fill="#888" text-anchor="middle" font-size="14">图片文件不存在或已被清理</text></svg>')
  }
}

onMounted(() => {
  loadHistory()
})
</script>

<style>
.container {
  position: relative;
  min-height: 100vh;
  color: var(--text-color, #ddd);
}
.content-wrapper {
  position: relative;
  z-index: 1;
  max-width: 1080px;
  margin: 0 auto;
  padding: 32px;
}
.main-header .header-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.main-header .subtitle {
  font-size: 12px;
  color: #888;
}
.panel.history-panel {
  margin-top: 16px;
  border: 1px solid var(--border-color, #ddd);
  border-radius: 10px;
  background: rgba(10, 10, 12, 0.55);
  backdrop-filter: blur(6px);
}
.history-panel .panel-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-color, #ddd);
  font-weight: 700;
}
.history-refresh {
  margin-left: auto;
}
.history-panel .panel-content {
  padding: 10px 16px 16px 16px;
  max-height: 62vh;
  overflow-y: auto;
}
.history-empty {
  padding: 12px 4px;
  font-size: 12px;
  color: #666;
  font-family: monospace;
}
.history-block {
  border-bottom: 1px dashed var(--border-color, #ddd);
  padding-bottom: 8px;
  margin-bottom: 8px;
}
.history-block:last-child {
  border-bottom: none;
}
.history-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 8px;
  cursor: pointer;
  border-radius: 6px;
  transition: background 0.15s ease;
}
.history-row:hover,
.history-row.active {
  background: rgba(255, 255, 255, 0.05);
}
.history-time {
  flex: 0 0 auto;
  font-family: monospace;
  font-size: 11px;
  color: #888;
  white-space: nowrap;
}
.history-task {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.history-pills {
  flex: 0 0 auto;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.history-json {
  flex: 0 0 auto;
  text-decoration: none;
}
.history-detail {
  margin: 4px 8px 8px 8px;
  padding: 12px;
  border: 1px solid var(--border-color, #ddd);
  border-radius: 8px;
  max-height: 520px;
  overflow-y: auto;
  background: rgba(0, 0, 0, 0.25);
}
.items-title {
  font-size: 12px;
  font-weight: 700;
  margin: 10px 0 6px 0;
}
.item-row {
  padding: 5px 0;
}
.item-main {
  font-size: 12px;
}
.item-sub {
  font-size: 11px;
  color: #888;
  margin-top: 2px;
}
.item-link {
  color: #7cc4ff;
  text-decoration: none;
}
.item-link:hover {
  text-decoration: underline;
}
.item-rank {
  color: #888;
  font-family: monospace;
  margin-right: 6px;
}
.item-pill {
  display: inline-block;
  font-size: 10px;
  padding: 1px 6px;
  border: 1px solid var(--border-color, #444);
  border-radius: 999px;
  color: #bbb;
  margin-right: 4px;
}
.item-dim {
  color: #777;
}
/* 图片网格 */
.img-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 10px;
  margin-bottom: 8px;
}
.img-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.img-thumb-btn {
  padding: 0;
  border: 1px solid var(--border-color, #444);
  border-radius: 6px;
  overflow: hidden;
  background: #111;
  cursor: zoom-in;
}
.img-thumb {
  display: block;
  width: 100%;
  height: 96px;
  object-fit: cover;
}
.img-src {
  font-size: 10px;
  color: #888;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-decoration: none;
}
a.img-src:hover {
  color: #7cc4ff;
}
/* 图片放大查看 */
.img-viewer-mask {
  position: fixed;
  inset: 0;
  z-index: 999;
  background: rgba(0, 0, 0, 0.82);
  display: flex;
  align-items: center;
  justify-content: center;
}
.img-viewer-body {
  max-width: 90vw;
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.img-viewer-img {
  max-width: 88vw;
  max-height: 76vh;
  object-fit: contain;
  border-radius: 8px;
  background: #111;
}
.img-viewer-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;
  color: #bbb;
}
.img-viewer-meta a {
  color: #7cc4ff;
  text-decoration: none;
}
.img-viewer-size {
  color: #777;
}
.custom-scrollbar::-webkit-scrollbar {
  width: 4px;
}
.custom-scrollbar::-webkit-scrollbar-track {
  background: transparent;
}
.custom-scrollbar::-webkit-scrollbar-thumb {
  background: var(--border-color, #555);
  border-radius: 4px;
}
.main-footer {
  margin-top: 28px;
  text-align: center;
}
.main-footer p {
  font-size: 9px;
  font-weight: 700;
  color: #333;
  letter-spacing: 4px;
}
</style>
