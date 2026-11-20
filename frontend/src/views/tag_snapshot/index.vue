<template>
  <section class="page" data-module="tag_snapshot">
    <header class="page-head">
      <div><h2>标签快照</h2><p class="page-desc">维护用户标签体系的周期快照、版本追溯与变更对比；接收下钻分组明细文件归档，并通过同一归档编号提供下载与来源回查。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记快照记录</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出作业清单</button>
      </div>
    </header>
    <div class="filter-bar">
      <label><span>开始日期</span><input type="date" v-model="dateFrom" /></label>
      <label><span>结束日期</span><input type="date" v-model="dateTo" /></label>
      <button class="btn" type="button" @click="reload">查询</button>
    </div>
    <div class="timeline-wrap">
      <div v-for="item in timelineItems" :key="item.id" class="timeline-row">
        <span class="timeline-label">{{ item.name || item.label }}</span>
        <div class="timeline-track">
          <div class="timeline-bar" :class="`status-${item.status}`" :style="{ left: barLeft(item) + '%', width: barWidth(item) + '%' }" :data-testid="`bar-${item.id}`" :title="`${item.start} ~ ${item.end}`" @click="selectItem(item)">&nbsp;</div>
        </div>
        <span style="font-size:12px;color:var(--muted);margin-left:8px;">{{ item.start }} ~ {{ item.end }}</span>
      </div>
      <p v-if="!timelineItems.length" style="text-align:center;color:var(--muted)">暂无作业记录</p>
    </div>
    <div v-if="selectedItem" class="timeline-log" data-testid="timeline-log">
      <strong>{{ selectedItem.name || selectedItem.label }}</strong> — 状态：{{ selectedItem.status }}
      <div style="margin-top:4px;">{{ selectedItem.logs || '暂无日志' }}</div>
    </div>

    <!-- 分组明细文件归档：同一归档编号幂等交付，附件完整确认后才可下载与回查 -->
    <div class="archive-panel" data-testid="archive-panel">
      <h3>下钻分组明细归档</h3>
      <p class="page-desc">归档请求携带完整分组明细文件；同一归档编号的并发/重试只交付一份快照，已确认后重复请求返回原文件。</p>
      <div class="analysis-form">
        <label><span>归档编号</span><input v-model="archiveId" placeholder="如 ARC-001（重试保持不变）" data-testid="archive-id" /></label>
        <label><span>导出文件编号</span><input v-model="archiveExportId" placeholder="如 DE-TD-001-0001" data-testid="archive-export-id" /></label>
        <label><span>快照编号（可选）</span><input v-model="archiveSnapshotId" placeholder="留空自动新建" data-testid="archive-snapshot-id" /></label>
        <label><span>故障注入</span>
          <select v-model="simulateFailure" data-testid="archive-failure">
            <option value="">不注入</option>
            <option value="after_write">写入后失败</option>
            <option value="before_confirm">确认前中断</option>
          </select>
        </label>
        <button class="btn primary" type="button" data-testid="submit-archive" @click="submitArchive">提交归档</button>
        <button class="btn" type="button" data-testid="reload-archives" @click="loadArchives">刷新归档列表</button>
      </div>
      <div v-if="archiveMessage" class="archive-message" :class="archiveOk ? 'trend-up' : 'trend-down'" data-testid="archive-message">{{ archiveMessage }}</div>

      <table class="data-table" v-if="archives.length" style="margin-top:12px">
        <thead><tr><th>归档编号</th><th>导出文件</th><th>下钻运行</th><th>状态</th><th>尝试次数</th><th>层级冲突</th><th>快照编号</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="a in archives" :key="a.archive_id" :data-testid="`archive-row-${a.archive_id}`">
            <td>{{ a.archive_id }}</td>
            <td>{{ a.export_id }}</td>
            <td>{{ a.drill_run_id }}</td>
            <td :class="a.status === 'confirmed' ? 'trend-up' : 'trend-down'">{{ a.status === 'confirmed' ? '已确认' : '归档中' }}</td>
            <td>{{ a.attempts }}</td>
            <td>{{ a.hierarchy_conflict ? '冲突' : '—' }}</td>
            <td>{{ a.snapshot_id || '—' }}</td>
            <td>
              <button class="btn" type="button" :disabled="a.status !== 'confirmed'" @click="downloadArchive(a.archive_id)">下载</button>
              <button class="btn" type="button" :disabled="a.status !== 'confirmed'" @click="traceArchive(a.archive_id)">来源回查</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else style="color:var(--muted)">暂无归档记录</p>

      <div v-if="traceResult" class="trace-box" data-testid="trace-result">
        <h4>来源回查：{{ traceResult.archive_id }}</h4>
        <p :class="traceResult.all_verified ? 'trend-up' : 'trend-down'">
          逐条核对 {{ traceResult.verified_groups }}/{{ traceResult.total_groups }} 一致
          ｜层级冲突：{{ traceResult.hierarchy_conflict ? '是（文件保留旧层级）' : '否' }}
        </p>
        <table class="data-table">
          <thead><tr><th>原始结果编号</th><th>分组口径</th><th>归档统计值</th><th>下钻原始统计值</th><th>同次下钻</th><th>核对</th></tr></thead>
          <tbody>
            <tr v-for="t in traceResult.traces.slice(0, 8)" :key="t.source_result_id">
              <td>{{ t.source_result_id }}</td>
              <td>{{ t.group_rule }}</td>
              <td>{{ t.archived_stat_value }}</td>
              <td>{{ t.source_stat_value }}</td>
              <td>{{ t.same_drill_run ? '是' : '否' }}</td>
              <td :class="t.verified ? 'trend-up' : 'trend-down'">{{ t.verified ? '一致' : '不一致' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记快照记录</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { request, fetchJson } from '@/api/client'

const ENDPOINT = '/api/tag-snapshot'
const columns = ["快照编号", "快照日期", "用户基数", "标签数量", "分群数量", "快照路径", "存储格式", "保留天数"]
interface SnapshotRow { id: number; snapshot_id: string; snapshot_date: string; status: string; user_count?: number; start?: string; end?: string; name?: string; label?: string; logs?: string }
interface ArchiveRecord { archive_id: string; export_id: string; drill_run_id: string; status: string; attempts: number; hierarchy_conflict?: boolean; snapshot_id?: string }
interface TraceLine { source_result_id: string; group_rule: string; archived_stat_value: number; source_stat_value: number; same_drill_run: boolean; verified: boolean }
interface TraceResult { archive_id: string; total_groups: number; verified_groups: number; all_verified: boolean; hierarchy_conflict: boolean; traces: TraceLine[] }

const timelineItems = ref<any[]>([])
const selectedItem = ref<any>(null)
const dateFrom = ref(''); const dateTo = ref('')
const errorMessage = ref(''); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const allItems = ref<any[]>([])

const archiveId = ref('ARC-001')
const archiveExportId = ref('')
const archiveSnapshotId = ref('')
const simulateFailure = ref('')
const archives = ref<ArchiveRecord[]>([])
const archiveMessage = ref(''); const archiveOk = ref(true)
const traceResult = ref<TraceResult | null>(null)

function barLeft(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 0; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return ((new Date(item.start).getTime() - min) / range) * 100 }
function barWidth(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 20; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return Math.max(5, ((new Date(item.end).getTime() - new Date(item.start).getTime()) / range) * 100) }
function selectItem(item: any) { selectedItem.value = selectedItem.value?.id === item.id ? null : item }

function decorate(rows: SnapshotRow[]) {
  return rows.map((r, i) => ({
    ...r,
    name: r.snapshot_id,
    label: r.snapshot_id,
    start: r.snapshot_date,
    end: r.snapshot_date,
    logs: `状态 ${r.status}；用户基数 ${r.user_count ?? 0}`,
  }))
}

async function reload() {
  try {
    const data = await fetchJson<{ items: SnapshotRow[] }>(ENDPOINT)
    allItems.value = data.items || []
    applyFilter()
  } catch(e) { errorMessage.value = '加载失败' }
}
function applyFilter() {
  let items = allItems.value
  if(dateFrom.value) items = items.filter((i: any) => i.snapshot_date >= dateFrom.value)
  if(dateTo.value) items = items.filter((i: any) => i.snapshot_date <= dateTo.value)
  timelineItems.value = decorate(items); selectedItem.value = null
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try {
    const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) })
    if(!res.ok) throw new Error('创建失败')
    showCreate.value = false
    await reload()
  } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }

async function loadArchives() {
  try {
    const data = await fetchJson<{ items: ArchiveRecord[] }>(`${ENDPOINT}/archives`)
    archives.value = data.items || []
  } catch (e) {
    archives.value = []
  }
}

async function loadExportFile(exportId: string): Promise<Record<string, any>> {
  // 从下钻服务读取完整文件后再随归档请求提交（附件必须完整）
  return await fetchJson<Record<string, any>>(`/api/tag-drill/exports/${exportId}`)
}

async function submitArchive() {
  archiveMessage.value = ''
  traceResult.value = null
  try {
    const exportFile = await loadExportFile(archiveExportId.value.trim())
    const payload: Record<string, any> = {
      archive_id: archiveId.value.trim(),
      export_file: exportFile,
    }
    if (archiveSnapshotId.value.trim()) payload.snapshot_id = archiveSnapshotId.value.trim()
    if (simulateFailure.value) payload.simulate_failure = simulateFailure.value
    const res = await request(`${ENDPOINT}/archives`, { method: 'POST', body: JSON.stringify(payload) })
    const data = await res.json()
    if (!res.ok) {
      const detail = data?.detail
      archiveOk.value = false
      archiveMessage.value = `归档未确认（${detail?.code ?? res.status}）：${detail?.message ?? '请用相同归档编号重试'}。文件可能已写入，可直接重新提交。`
    } else {
      archiveOk.value = true
      archiveMessage.value = data.entry.idempotent_replay
        ? `幂等返回原文件：${data.entry.export_id}（只交付一份快照）`
        : `归档已确认：${data.entry.archive_id} -> 快照 ${data.entry.snapshot_id}${data.entry.hierarchy_conflict ? '；检测到层级冲突，文件保留旧层级' : ''}`
    }
    await loadArchives()
    await reload()
  } catch (e: any) {
    archiveOk.value = false
    archiveMessage.value = e?.message || '归档请求失败'
  }
}

function downloadArchive(id: string) {
  window.open(`${ENDPOINT}/archives/${id}/download`, '_blank')
}

async function traceArchive(id: string) {
  try {
    traceResult.value = await fetchJson<TraceResult>(`${ENDPOINT}/archives/${id}/trace`)
  } catch (e: any) {
    archiveOk.value = false
    archiveMessage.value = e?.message || '来源回查失败'
  }
}

onMounted(() => { void reload(); void loadArchives() })
</script>
