<template>
  <section class="page" data-module="tag_drill">
    <header class="page-head">
      <div><h2>标签下钻</h2><p class="page-desc">维护标签体系的多级下钻、交叉分析与分布统计配置，并导出可复核的分组明细文件用于标签快照归档。</p></div>
    </header>
    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span :class="m.trend > 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">{{ m.trend > 0 ? '↑' : '↓' }}{{ Math.abs(m.trend) }}%</span>
      </article>
    </div>
    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label v-for="field in paramFields" :key="field"><span>{{ field }}</span>
        <select v-if="field.includes('方法')||field.includes('策略')||field.includes('粒度')" v-model="params[field]"><option v-for="o in options[field]" :key="o" :value="o">{{ o }}</option></select>
        <input v-else v-model="params[field]" :placeholder="`请输入${field}`" />
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis" :disabled="loading">{{ loading ? '运行中…' : '运行分析' }}</button>
    </form>

    <div v-if="message" class="form-tip" :class="messageKind" data-testid="drill-message">{{ message }}</div>

    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }"><span>{{ bar.label }}</span></div>
    </div>
    <table class="data-table" v-if="results.length">
      <thead><tr><th v-for="c in resultColumns" :key="c">{{ c }}</th></tr></thead>
      <tbody><tr v-for="row in results" :key="row.id"><td v-for="c in resultColumns" :key="c">{{ row[c] ?? '—' }}</td></tr></tbody>
    </table>
    <p v-if="!results.length && !loading" style="text-align:center;color:var(--muted)">请配置参数后运行分析</p>

    <!-- 分组明细导出：一份文件来自同一次下钻，冻结版本、口径、统计值与原始结果编号 -->
    <div class="export-panel" data-testid="export-panel">
      <h3>分组明细导出</h3>
      <p class="page-desc">从下钻统计数据准备可复核文件：保存父子标签版本、分组口径、统计值与原始结果编号，并冻结导出时的父子层级。</p>
      <div class="analysis-form">
        <label><span>下钻配置编号</span><input v-model="exportConfigId" placeholder="如 TD-001" data-testid="export-config-id" /></label>
        <label><span>下钻运行编号（可选）</span><input v-model="exportRunId" placeholder="留空使用最新一次运行" data-testid="export-run-id" /></label>
        <button class="btn primary" type="button" data-testid="prepare-export" @click="prepareExportFile">准备分组明细文件</button>
      </div>

      <div v-if="lastExport" class="export-summary" data-testid="last-export">
        <div><strong>文件编号：</strong>{{ lastExport.export_id }}</div>
        <div><strong>下钻运行：</strong>{{ lastExport.drill_run_id }}</div>
        <div><strong>分组行数：</strong>{{ lastExport.group_count }}</div>
        <div><strong>父标签版本：</strong>{{ lastExport.parent_tag.tag_id }} {{ lastExport.parent_tag.version }}</div>
        <div><strong>子标签版本：</strong><span v-for="c in lastExport.child_tags" :key="c.tag_id">{{ c.tag_id }} {{ c.version }} </span></div>
        <div :class="lastExport.hierarchy_conflict ? 'trend-down' : 'trend-up'" data-testid="export-conflict">
          层级状态：{{ lastExport.hierarchy_conflict ? '历史下钻与现行层级冲突（文件保留旧层级）' : '与现行层级一致' }}
        </div>
        <button class="btn" type="button" @click="downloadExport(lastExport.export_id)">下载该文件</button>
      </div>

      <h4 style="margin-top:16px">层级调整（模拟子标签换父级）</h4>
      <div class="analysis-form">
        <label><span>子标签编号</span><input v-model="moveChild" placeholder="如 TAG-002" data-testid="move-child" /></label>
        <label><span>新父标签编号</span><input v-model="moveParent" placeholder="如 TAG-003" data-testid="move-parent" /></label>
        <button class="btn" type="button" data-testid="move-tag" @click="moveTag">转到新父级</button>
      </div>

      <h4 style="margin-top:16px">已准备的导出文件</h4>
      <table class="data-table" v-if="exportFiles.length">
        <thead><tr><th>文件编号</th><th>下钻运行</th><th>分组行数</th><th>统计方式</th><th>层级冲突</th><th>准备时间</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="f in exportFiles" :key="f.export_id" :data-testid="`export-row-${f.export_id}`">
            <td>{{ f.export_id }}</td>
            <td>{{ f.drill_run_id }}</td>
            <td>{{ f.group_count }}</td>
            <td>{{ f.stat_method }}</td>
            <td :class="f.hierarchy_conflict ? 'trend-down' : 'trend-up'">{{ f.hierarchy_conflict ? '冲突' : '一致' }}</td>
            <td>{{ f.prepared_at }}</td>
            <td><button class="btn" type="button" @click="downloadExport(f.export_id)">下载</button></td>
          </tr>
        </tbody>
      </table>
      <p v-else style="color:var(--muted)">暂无导出文件</p>
    </div>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request, fetchJson } from '@/api/client'

const ENDPOINT = '/api/tag-drill'
interface ExportFile { export_id: string; drill_run_id: string; config_id: string; stat_method: string; group_count: number; prepared_at: string; hierarchy_conflict: boolean; parent_tag: { tag_id: string; version: string }; child_tags: { tag_id: string; version: string }[] }

const summaryMetrics = ref<{label:string;value:number;trend:number}[]>([])
const results = ref<Record<string, any>[]>([]); const chartData = ref<{label:string;value:number}[]>([])
const loading = ref(false)
const params = ref<Record<string,string>>({})
const message = ref(''); const messageKind = ref('trend-up')
const paramFields = ["配置编号", "父标签", "子标签集", "下钻维度", "数据范围", "统计方式", "输出格式", "刷新间隔"].filter((f: string) => !['编号','名称','版本'].includes(f)).slice(0, 4)
const options: Record<string,string[]> = { '统计粒度': ['按日','按周','按月'], '分组字段': ['全量','按类型','按区域'], '比较方法': ['环比','同比','绝对值'], '时间范围': ['最近7天','最近30天','最近90天'] }
const resultColumns = computed(() => ["config_id", "parent_tag", "child_tags", "drill_dimensions", "stat_method"])

const exportConfigId = ref('TD-001')
const exportRunId = ref('')
const exportFiles = ref<ExportFile[]>([])
const lastExport = ref<ExportFile | null>(null)
const moveChild = ref('TAG-002')
const moveParent = ref('TAG-003')

function barHeight(v: number) { const max = Math.max(...chartData.value.map(d => d.value), 1); return Math.max(10, (v / max) * 140) }

async function fetchSummary() {
  try {
    const data = await fetchJson<{ items: Record<string, any>[]; total: number }>(ENDPOINT)
    const rows = data.items || []
    summaryMetrics.value = [
      { label: '下钻配置', value: data.total ?? rows.length, trend: 0 },
      { label: '已生效配置', value: rows.filter(r => r.status === '已生效').length, trend: 0 },
      { label: '导出文件', value: exportFiles.value.length, trend: 0 },
    ]
    results.value = rows
  } catch (e) {
    summaryMetrics.value = [{ label: '下钻配置', value: 0, trend: 0 }, { label: '已生效配置', value: 0, trend: 0 }]
  }
}

async function runAnalysis() {
  loading.value = true
  message.value = ''
  try {
    const configId = params.value['配置编号'] || 'TD-001'
    const res = await request(`${ENDPOINT}/${configId}/run`, { method: 'POST' })
    if (!res.ok) throw new Error(`运行失败 ${res.status}`)
    const data = await res.json()
    message.value = `下钻运行完成：${data.entry.drill_run_id}`
    messageKind.value = 'trend-up'
    const groups = (data.entry.drill_data ? Object.values(data.entry.drill_data).flat() : []) as { dimension: string; stat_value: number }[]
    chartData.value = groups.slice(0, 6).map(g => ({ label: g.dimension, value: g.stat_value }))
    await fetchSummary()
  } catch (e: any) {
    message.value = e?.message || '运行失败'
    messageKind.value = 'trend-down'
  }
  loading.value = false
}

async function loadExports() {
  try {
    const data = await fetchJson<{ items: ExportFile[] }>(`${ENDPOINT}/exports`)
    exportFiles.value = data.items || []
  } catch (e) {
    exportFiles.value = []
  }
}

async function prepareExportFile() {
  message.value = ''
  try {
    const payload = exportRunId.value.trim() ? { drill_run_id: exportRunId.value.trim() } : {}
    const res = await request(`${ENDPOINT}/${exportConfigId.value.trim()}/prepare-export`, { method: 'POST', body: JSON.stringify(payload) })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || '准备失败')
    lastExport.value = data.entry
    message.value = `分组明细文件已准备：${data.entry.export_id}（来自 ${data.entry.drill_run_id}）`
    messageKind.value = 'trend-up'
    await loadExports()
  } catch (e: any) {
    message.value = e?.message || '准备失败'
    messageKind.value = 'trend-down'
  }
}

function downloadExport(exportId: string) {
  window.open(`${ENDPOINT}/exports/${exportId}`, '_blank')
}

async function moveTag() {
  message.value = ''
  try {
    const res = await request(`${ENDPOINT}/move-tag`, { method: 'POST', body: JSON.stringify({ child_tag: moveChild.value.trim(), new_parent: moveParent.value.trim() }) })
    const data = await res.json()
    if (!res.ok) throw new Error(data?.detail || '层级调整失败')
    message.value = `已将 ${data.entry.child_tag} 从 ${data.entry.old_parent ?? '无'} 转到 ${data.entry.new_parent}；历史导出文件保留旧层级`
    messageKind.value = 'trend-down'
    await loadExports()
  } catch (e: any) {
    message.value = e?.message || '层级调整失败'
    messageKind.value = 'trend-down'
  }
}

onMounted(() => { void fetchSummary(); void loadExports() })
</script>
