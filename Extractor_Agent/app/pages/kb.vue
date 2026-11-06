<script setup lang="ts">
// 知识库 / 图谱界面：iframe 嵌入 wiki_kb_service 的图谱 UI（端口 13105）
// 使爬取工作台与知识库在同一应用内互相跳转，不脱离站点。
const config = useRuntimeConfig()
// KB 服务地址：与 /api/config 的 companyCrawl.kbServiceUrl 同源配置
const kbBase = ref(String((config.public as any)?.kbServiceUrl || 'http://127.0.0.1:13105').replace(/\/+$/, ''))
const crawlerOrigin = ref('')
onMounted(async () => {
  crawlerOrigin.value = window.location.origin
  try {
    const cfg: any = await $fetch('/api/config')
    const kb = cfg?.companyCrawl
    if (kb?.kbServiceUrl) kbBase.value = String(kb.kbServiceUrl).replace(/\/+$/, '')
  } catch {
    /* 用默认值 */
  }
})
const kbUrl = computed(() => {
  const base = `${kbBase.value}/?embed=1`
  return crawlerOrigin.value ? `${base}&crawler=${encodeURIComponent(crawlerOrigin.value)}` : base
})

function backToWorkbench() {
  navigateTo('/')
}
</script>

<template>
  <div class="kb-shell">
    <div class="kb-topbar">
      <button type="button" class="kb-back" @click="backToWorkbench">← 返回爬取工作台</button>
      <div class="kb-title">公司信息知识库 · 图谱 / 快照对比 / Wiki</div>
      <a class="kb-newtab" :href="kbBase" target="_blank" rel="noopener">新窗口打开 ↗</a>
    </div>
    <iframe class="kb-frame" :src="kbUrl" title="知识库图谱界面" allow="fullscreen"></iframe>
  </div>
</template>

<style scoped>
.kb-shell {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: #10161f;
}
.kb-topbar {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 8px 14px;
  background: #151d29;
  border-bottom: 1px solid #263243;
  flex: none;
}
.kb-back {
  border: 1px solid #33445c;
  background: #1b2637;
  color: #d8e1ea;
  border-radius: 6px;
  padding: 5px 12px;
  font-size: 13px;
  cursor: pointer;
}
.kb-back:hover { background: #24334a; }
.kb-title {
  flex: 1;
  color: #9db8d8;
  font-size: 13px;
  text-align: center;
}
.kb-newtab {
  color: #7fb0ff;
  font-size: 13px;
  text-decoration: none;
}
.kb-frame {
  flex: 1;
  width: 100%;
  border: 0;
}
</style>
