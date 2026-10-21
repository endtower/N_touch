<script setup lang="ts">
import AppDialog from './AppDialog.vue'
import { computed, ref, watch } from 'vue'
import { DataAnalysis, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import type { TokenBucket, TokenStatsResponse } from '../types'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [v: boolean] }>()

const data = ref<TokenStatsResponse | null>(null)
const loading = ref(false)

// 日期区间 (YYYY-MM-DD)；默认最近 30 天（含今天）
function defaultRange(): [string, string] {
  const pad = (n: number) => n.toString().padStart(2, '0')
  const fmt = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
  const end = new Date()
  const start = new Date()
  start.setDate(start.getDate() - 29)
  return [fmt(start), fmt(end)]
}
const dateRange = ref<[string, string]>(defaultRange())

watch(
  () => props.visible,
  (v) => {
    if (v) fetchStats()
  },
)

async function fetchStats() {
  loading.value = true
  try {
    const [start, end] = dateRange.value || ['', '']
    const qs = new URLSearchParams()
    if (start) qs.set('start', start)
    if (end) qs.set('end', end)
    const res = await fetch(`/token-stats?${qs.toString()}`)
    if (!res.ok) {
      const t = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${t || res.statusText}`)
    }
    data.value = (await res.json()) as TokenStatsResponse
  } catch (e: any) {
    ElMessage.error(`加载 token 统计失败: ${e?.message || e}`)
  } finally {
    loading.value = false
  }
}

function close() {
  emit('update:visible', false)
}

// ===== 聚合行 (把 Record<string, Bucket> 拍平成数组, 按 total_tokens 倒序) =====
interface Row {
  key: string
  bucket: TokenBucket
}

function toRows(map: Record<string, TokenBucket> | undefined): Row[] {
  if (!map) return []
  return Object.entries(map)
    .map(([key, bucket]) => ({ key, bucket }))
    .sort((a, b) => b.bucket.total_tokens - a.bucket.total_tokens)
}

const grandTotal = computed(() => data.value?.totals.total_tokens ?? 0)
const providerRows = computed(() => toRows(data.value?.by_provider))
const agentRows = computed(() => toRows(data.value?.by_agent))
const modelRows = computed(() => toRows(data.value?.by_model))
const dayRows = computed(() =>
  (data.value?.per_day ?? []).map(d => ({ key: d.date, bucket: d.totals })),
)
const maxDayTokens = computed(() => Math.max(1, ...dayRows.value.map(r => r.bucket.total_tokens)))

// ===== 展示辅助 =====
function fmtNum(n: number | undefined): string {
  return (n ?? 0).toLocaleString('en-US')
}
function fmtCost(n: number | undefined): string {
  return `¥${(n ?? 0).toFixed(4)}`
}
// 大数简写：390,532 -> 39.1万
function fmtShort(n: number | undefined): string {
  const v = n ?? 0
  if (v >= 100_000_000) return `${(v / 100_000_000).toFixed(2)}亿`
  if (v >= 10_000) return `${(v / 10_000).toFixed(1)}万`
  return fmtNum(v)
}

// 缓存命中率 = 命中 / (命中 + 未命中) —— 两者之和即 prompt_tokens
function hitRate(b: TokenBucket): number {
  const denom = (b.cache_hit_tokens ?? 0) + (b.cache_miss_tokens ?? 0)
  if (denom <= 0) return 0
  return (b.cache_hit_tokens / denom) * 100
}
function hitRateText(b: TokenBucket): string {
  return `${hitRate(b).toFixed(1)}%`
}
function hitColor(b: TokenBucket): string {
  const r = hitRate(b)
  if (r >= 60) return 'var(--accent-2)'
  if (r >= 30) return 'var(--warn)'
  return 'var(--text-3)'
}

// 占比：该维度 token 数 / 区间总 token
function share(b: TokenBucket): number {
  if (grandTotal.value <= 0) return 0
  return (b.total_tokens / grandTotal.value) * 100
}
function shareText(b: TokenBucket): string {
  return `${share(b).toFixed(1)}%`
}

// 按天柱高：相对区间内单日峰值
function dayBarHeight(b: TokenBucket): number {
  return Math.max(2, (b.total_tokens / maxDayTokens.value) * 100)
}

function shortTime(iso?: string | null): string {
  if (!iso) return '—'
  return iso.replace('T', ' ').slice(0, 19)
}
</script>

<template>
  <AppDialog size="panel"
    :model-value="props.visible"
    :close-on-click-modal="false"
    align-center
    class="tk-dialog"
    title="Token 用量统计"
    @update:model-value="(v: boolean) => emit('update:visible', v)"
  >
    <template #header>
      <div class="tk-header">
        <div class="tk-title">
          <el-icon :size="18" color="var(--accent)"><DataAnalysis /></el-icon>
          <span>Token 用量统计</span>
          <span class="tk-sub">只读 · 按天 / Agent / 提供商 / 模型</span>
        </div>
        <div class="tk-actions">
          <el-date-picker
            v-model="dateRange"
            type="daterange"
            unlink-panels
            value-format="YYYY-MM-DD"
            range-separator="至"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            size="small"
            style="width: 240px"
          />
          <el-button size="small" :icon="Refresh" :loading="loading" @click="fetchStats">查询</el-button>
          <el-button size="small" @click="close">关闭</el-button>
        </div>
      </div>
    </template>

    <div v-if="!data" class="tk-loading">{{ loading ? '加载中…' : '等待数据' }}</div>
    <div v-else-if="data.totals.call_count === 0" class="tk-empty">
      该区间内没有调用记录
      <div class="tk-empty-sub">换个日期范围试试</div>
    </div>
    <div v-else class="tk-body">
      <!-- 顶部汇总 -->
      <section class="tk-summary">
        <div class="tk-card">
          <div class="tk-card-label">总 Token</div>
          <div class="tk-card-value">{{ fmtNum(data.totals.total_tokens) }}</div>
          <div class="tk-card-foot">
            <span class="tk-dot dot-in"></span>输入 {{ fmtShort(data.totals.prompt_tokens) }}
            <span class="tk-dot dot-out"></span>输出 {{ fmtShort(data.totals.completion_tokens) }}
          </div>
        </div>

        <div class="tk-card">
          <div class="tk-card-label">调用次数</div>
          <div class="tk-card-value">{{ fmtNum(data.totals.call_count) }}</div>
          <div class="tk-card-foot">{{ data.range.days.length }} 天 · 更新 {{ shortTime(data.updated_at) }}</div>
        </div>

        <div class="tk-card">
          <div class="tk-card-label">缓存命中率</div>
          <div class="tk-card-value" :style="{ color: hitColor(data.totals) }">{{ hitRateText(data.totals) }}</div>
          <div class="tk-progress">
            <div class="tk-progress-fill" :style="{ width: hitRate(data.totals) + '%', background: hitColor(data.totals) }" />
          </div>
          <div class="tk-card-foot">
            命中 {{ fmtShort(data.totals.cache_hit_tokens) }} · 未命中 {{ fmtShort(data.totals.cache_miss_tokens) }}
          </div>
        </div>

        <div class="tk-card">
          <div class="tk-card-label">总花费（人民币）</div>
          <div class="tk-card-value">{{ fmtCost(data.totals.cost_total) }}</div>
          <div class="tk-card-foot">
            输入 {{ fmtCost(data.totals.cost_cache_miss) }} · 缓存 {{ fmtCost(data.totals.cost_cache_hit) }} · 输出 {{ fmtCost(data.totals.cost_output) }}
          </div>
        </div>
      </section>

      <!-- 各 API 提供商（家） -->
      <section class="tk-section">
        <div class="tk-section-head">
          <span>各 API 提供商（家）</span>
          <span class="tk-section-sub">{{ providerRows.length }} 家 · 占比按总 Token</span>
        </div>
        <div class="tk-bars">
          <div v-for="r in providerRows" :key="r.key" class="tk-bar-row">
            <div class="tk-bar-name" :title="r.key">{{ r.key }}</div>
            <div class="tk-bar-track">
              <div class="tk-bar-fill" :style="{ width: share(r.bucket) + '%' }" />
            </div>
            <div class="tk-bar-pct">{{ shareText(r.bucket) }}</div>
            <div class="tk-bar-val">{{ fmtShort(r.bucket.total_tokens) }}</div>
            <div class="tk-bar-hit" :style="{ color: hitColor(r.bucket) }">命中 {{ hitRateText(r.bucket) }}</div>
            <div class="tk-bar-meta">{{ r.bucket.call_count }} 次 · {{ fmtCost(r.bucket.cost_total) }}</div>
          </div>
        </div>
      </section>

      <!-- 各 Agent -->
      <section class="tk-section">
        <div class="tk-section-head">
          <span>各 Agent</span>
          <span class="tk-section-sub">{{ agentRows.length }} 个 · 降序</span>
        </div>
        <div class="tk-bars">
          <div v-for="r in agentRows" :key="r.key" class="tk-bar-row">
            <div class="tk-bar-name" :title="r.key">{{ r.key }}</div>
            <div class="tk-bar-track">
              <div class="tk-bar-fill fill-agent" :style="{ width: share(r.bucket) + '%' }" />
            </div>
            <div class="tk-bar-pct">{{ shareText(r.bucket) }}</div>
            <div class="tk-bar-val">{{ fmtShort(r.bucket.total_tokens) }}</div>
            <div class="tk-bar-hit" :style="{ color: hitColor(r.bucket) }">命中 {{ hitRateText(r.bucket) }}</div>
            <div class="tk-bar-meta">{{ r.bucket.call_count }} 次 · {{ fmtCost(r.bucket.cost_total) }}</div>
          </div>
        </div>
      </section>

      <!-- 各模型 -->
      <section class="tk-section">
        <div class="tk-section-head">
          <span>各模型</span>
          <span class="tk-section-sub">{{ modelRows.length }} 个</span>
        </div>
        <div class="tk-bars">
          <div v-for="r in modelRows" :key="r.key" class="tk-bar-row">
            <div class="tk-bar-name" :title="r.key">{{ r.key }}</div>
            <div class="tk-bar-track">
              <div class="tk-bar-fill fill-model" :style="{ width: share(r.bucket) + '%' }" />
            </div>
            <div class="tk-bar-pct">{{ shareText(r.bucket) }}</div>
            <div class="tk-bar-val">{{ fmtShort(r.bucket.total_tokens) }}</div>
            <div class="tk-bar-hit" :style="{ color: hitColor(r.bucket) }">命中 {{ hitRateText(r.bucket) }}</div>
            <div class="tk-bar-meta">{{ r.bucket.call_count }} 次 · {{ fmtCost(r.bucket.cost_total) }}</div>
          </div>
        </div>
      </section>

      <!-- 按天趋势 -->
      <section class="tk-section">
        <div class="tk-section-head">
          <span>按天趋势</span>
          <span class="tk-section-sub">{{ dayRows.length }} 天 · 柱高按单日峰值</span>
        </div>
        <div class="tk-chart">
          <div v-for="r in dayRows" :key="r.key" class="tk-chart-col" :title="`${r.key}: ${fmtNum(r.bucket.total_tokens)} token · ${r.bucket.call_count} 次`">
            <div class="tk-chart-col-value">{{ fmtShort(r.bucket.total_tokens) }}</div>
            <div class="tk-chart-col-bar-wrap">
              <div class="tk-chart-col-bar" :style="{ height: dayBarHeight(r.bucket) + '%' }" />
            </div>
            <div class="tk-chart-col-label">{{ r.key.slice(5) }}</div>
          </div>
        </div>
      </section>
    </div>
  </AppDialog>
</template>

<style scoped>

.tk-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--bg-surface);
}
.tk-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: 14px;
  color: var(--text-1);
}
.tk-sub {
  font-weight: 400;
  font-size: 11.5px;
  color: var(--text-3);
  margin-left: 8px;
}
.tk-actions { display: flex; align-items: center; gap: 8px; }

.tk-loading,
.tk-empty {
  padding: 80px 20px;
  text-align: center;
  color: var(--text-3);
  font-size: 13px;
}
.tk-empty-sub {
  margin-top: 6px;
  font-size: 11.5px;
  color: var(--text-3);
}

.tk-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 20px 28px 20px;
  background: var(--bg-app);
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* —— 顶部汇总卡片 —— */
.tk-summary {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}
.tk-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-1);
  border-radius: var(--radius);
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.tk-card-label {
  font-size: 11px;
  color: var(--text-3);
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.tk-card-value {
  font-size: 24px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  font-family: var(--font-mono);
  color: var(--text-1);
}
.tk-card-foot {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
  font-size: 10.5px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}
.tk-dot {
  width: 7px;
  height: 7px;
  border-radius: 2px;
  display: inline-block;
}
.tk-dot.dot-in { background: var(--accent); }
.tk-dot.dot-out { background: var(--accent-2); margin-left: 6px; }

/* 汇总卡片内的命中率进度条 */
.tk-progress {
  height: 6px;
  border-radius: 999px;
  background: var(--bg-subtle);
  overflow: hidden;
}
.tk-progress-fill {
  height: 100%;
  border-radius: 999px;
  transition: width 0.3s ease;
}

/* —— 分节 —— */
.tk-section {
  background: var(--bg-surface);
  border: 1px solid var(--border-1);
  border-radius: var(--radius);
  padding: 12px 16px;
}
.tk-section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11.5px;
  color: var(--text-3);
  font-weight: 600;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  margin-bottom: 12px;
}
.tk-section-sub {
  font-size: 11px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}

/* —— 条形排行 —— */
.tk-bars {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.tk-bar-row {
  display: grid;
  grid-template-columns: 160px 1fr 56px 78px 92px 130px;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}
.tk-bar-name {
  font-family: var(--font-mono);
  color: var(--text-1);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tk-bar-track {
  height: 16px;
  background: var(--bg-subtle);
  border-radius: var(--radius-sm);
  overflow: hidden;
}
.tk-bar-fill {
  height: 100%;
  border-radius: var(--radius-sm);
  background: linear-gradient(90deg, var(--accent), var(--accent-hover));
  transition: width 0.3s ease;
}
.tk-bar-fill.fill-agent {
  background: linear-gradient(90deg, var(--tag-success), color-mix(in srgb, var(--tag-success) 72%, white));
}
.tk-bar-fill.fill-model {
  background: linear-gradient(90deg, var(--tag-warning), color-mix(in srgb, var(--tag-warning) 72%, white));
}
.tk-bar-pct {
  text-align: right;
  font-family: var(--font-mono);
  font-weight: 600;
  color: var(--text-1);
  font-variant-numeric: tabular-nums;
}
.tk-bar-val {
  text-align: right;
  font-family: var(--font-mono);
  color: var(--text-2);
  font-variant-numeric: tabular-nums;
}
.tk-bar-hit {
  text-align: right;
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}
.tk-bar-meta {
  text-align: right;
  color: var(--text-3);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

/* —— 按天柱状图 —— */
.tk-chart {
  display: flex;
  align-items: flex-end;
  gap: 6px;
  height: 200px;
  padding: 8px 4px 0 4px;
  overflow-x: auto;
}
.tk-chart-col {
  flex: 1 0 34px;
  min-width: 34px;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}
.tk-chart-col-value {
  font-size: 10px;
  color: var(--text-3);
  font-family: var(--font-mono);
  white-space: nowrap;
}
.tk-chart-col-bar-wrap {
  flex: 1 1 auto;
  width: 100%;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  min-height: 0;
}
.tk-chart-col-bar {
  width: 70%;
  border-radius: 4px 4px 0 0;
  background: linear-gradient(180deg, var(--accent-hover), var(--accent));
  transition: height 0.3s ease;
}
.tk-chart-col-label {
  font-size: 10px;
  color: var(--text-3);
  font-family: var(--font-mono);
  white-space: nowrap;
}
/* Keep header controls clear of the shared close button. */
.tk-header, .tk-title, .tk-actions { flex-wrap: wrap; min-width: 0; gap: 8px; }
</style>