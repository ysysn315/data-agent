<template>
  <div class="tasks-view">
    <!-- 顶部：标题 -->
    <div class="page-header card">
      <div class="page-header-left">
        <h3 class="card-title">任务中心</h3>
        <p class="page-desc">提交长耗时任务（后台 worker 执行），进度经 SSE 实时回传。任务 ID 记在本地，刷新页面仍可回看。</p>
      </div>
      <button class="btn btn-secondary" @click="refreshAll" :disabled="refreshing">
        <span v-if="refreshing" class="loading-spinner small"></span>
        <span v-else>刷新状态</span>
      </button>
    </div>

    <!-- 提交区 -->
    <div class="card submit-card">
      <div class="card-header">
        <span class="card-title">提交新任务</span>
      </div>

      <div class="submit-grid">
        <label class="field">
          <span class="field-label">任务类型</span>
          <select v-model="taskType" class="input">
            <option v-for="t in TASK_TYPES" :key="t.value" :value="t.value">{{ t.label }}</option>
          </select>
        </label>

        <!-- 数据分析参数 -->
        <template v-if="taskType === 'run_analysis_task'">
          <label class="field field-wide">
            <span class="field-label">分析问题</span>
            <textarea
              v-model="analysisQuestion"
              class="input"
              rows="2"
              placeholder="例：分析各州（customer_state）的销售额分布并给出建议"
            ></textarea>
          </label>
        </template>

        <!-- 评估跑批参数 -->
        <template v-else-if="taskType === 'eval'">
          <label class="field">
            <span class="field-label">用例数上限 <span class="field-hint">（留空=全量）</span></span>
            <input v-model="evalLimit" class="input" type="number" min="1" placeholder="如 5" />
          </label>
          <label class="field">
            <span class="field-label">模型 <span class="field-hint">（可选，留空用默认）</span></span>
            <input v-model="evalModel" class="input" placeholder="如 deepseek-chat" />
          </label>
        </template>
      </div>

      <div class="submit-foot">
        <span class="type-desc">{{ activeTypeDesc }}</span>
        <button class="btn btn-primary" @click="submitTask" :disabled="submitting || !canSubmit">
          <span v-if="submitting" class="loading-spinner small"></span>
          <template v-else>
            <svg viewBox="0 0 20 20" fill="none" width="14" height="14">
              <path d="M3 10l14-6-4.5 13-2.8-5.2L3 10Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>
            </svg>
            提交任务
          </template>
        </button>
      </div>

      <div v-if="submitError" class="inline-error">
        <strong>提交失败：</strong>{{ submitError }}
        <div class="inline-error-hint">异步任务依赖 Redis 与 arq worker。请确认二者已启动（<code>arq app.tasks.worker.WorkerSettings</code>）。</div>
      </div>
    </div>

    <!-- 任务列表 -->
    <div class="group-title">本会话任务（{{ tasks.length }}）</div>

    <div v-if="tasks.length === 0" class="empty-state card">
      <p>还没有提交任何任务。填写上方表单并提交，任务会出现在这里。</p>
    </div>

    <div v-else class="task-grid">
      <div
        v-for="task in tasks"
        :key="task.task_id"
        class="task-card"
        @click="openTask(task)"
      >
        <div class="task-card-head">
          <span class="monogram" :class="{ dim: task.status === 'failed' || task.status === 'missing' }">
            {{ typeInitial(task.type) }}
          </span>
          <div class="task-name-area">
            <div class="task-name">{{ typeLabel(task.type) }}</div>
            <div class="task-id">{{ task.task_id.slice(0, 12) }}…</div>
          </div>
          <span class="badge" :class="statusBadge(task.status).cls">{{ statusBadge(task.status).text }}</span>
        </div>
        <p class="task-summary">{{ task.summary || '（无参数摘要）' }}</p>
        <div class="task-card-foot">
          <span class="task-time">{{ formatTime(task.createdAt) }}</span>
          <button class="btn small btn-danger" @click.stop="removeTask(task)">移除</button>
        </div>
      </div>
    </div>

    <!-- 详情弹窗 -->
    <div v-if="detailVisible" class="modal-mask" @click.self="closeDetail">
      <div class="modal-panel card">
        <div class="modal-header">
          <div>
            <div class="modal-title">{{ typeLabel(active.type) }}</div>
            <div class="modal-sub">
              <span class="badge" :class="statusBadge(active.status).cls">{{ statusBadge(active.status).text }}</span>
              <span class="session-label">{{ active.task_id }}</span>
            </div>
          </div>
          <button class="btn btn-secondary small" @click="closeDetail">关闭</button>
        </div>

        <div class="modal-body">
          <!-- 进度条 -->
          <div class="progress-block">
            <div class="progress-track">
              <div
                class="progress-fill"
                :class="{ indeterminate: progress === null && !isTerminal, failed: active.status === 'failed' }"
                :style="progress !== null ? { width: (progress * 100).toFixed(1) + '%' } : {}"
              ></div>
            </div>
            <span class="progress-pct">{{ progress !== null ? Math.round(progress * 100) + '%' : (isTerminal ? '—' : '…') }}</span>
          </div>

          <!-- 连接/错误提示 -->
          <div v-if="detailError" class="inline-error">
            {{ detailError }}
            <div class="inline-error-hint">若刚提交，稍候片刻；持续失败请确认 Redis / worker 已启动。</div>
          </div>

          <!-- 事件时间线 -->
          <div class="timeline-head">事件时间线（{{ events.length }}）</div>
          <div class="timeline" ref="timelineEl">
            <div v-if="events.length === 0 && !detailError" class="timeline-empty">
              <span class="loading-spinner small"></span> 正在订阅事件…
            </div>
            <div v-for="(ev, i) in events" :key="i" class="event-row" :class="'event-' + ev.type">
              <span class="event-dot"></span>
              <div class="event-body">
                <div class="event-msg">
                  <span class="badge event-type-badge" :class="eventBadge(ev.type)">{{ ev.type }}</span>
                  {{ ev.message }}
                </div>
                <div v-if="ev.progress !== null && ev.progress !== undefined" class="event-progress">进度 {{ Math.round(ev.progress * 100) }}%</div>
              </div>
              <span class="event-ts">{{ formatClock(ev.ts) }}</span>
            </div>
          </div>

          <!-- 分析报告（done 后拉取 result.report 渲染 markdown） -->
          <template v-if="report">
            <div class="timeline-head">分析报告</div>
            <div class="markdown-content report-body" v-html="renderMarkdown(report)"></div>
          </template>

          <!-- 评估摘要 -->
          <template v-if="evalSummary">
            <div class="timeline-head">评估结果</div>
            <div class="eval-summary">
              <div class="eval-stat">
                <span class="eval-value">{{ (evalSummary.accuracy * 100).toFixed(1) }}%</span>
                <span class="eval-label">执行准确率</span>
              </div>
              <div class="eval-stat">
                <span class="eval-value">{{ evalSummary.correct }}/{{ evalSummary.total }}</span>
                <span class="eval-label">正确 / 总数</span>
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onBeforeUnmount, nextTick } from 'vue'
import { marked } from 'marked'

// 任务类型：value 为后端 TASK_REGISTRY 真实 key（run_analysis_task / eval）。
const TASK_TYPES = [
  { value: 'run_analysis_task', label: '数据分析', desc: 'Plan-Operation-Reflection 工作流，产出结构化 Markdown 报告。' },
  { value: 'eval', label: '评估跑批', desc: 'Text-to-SQL 执行准确率评估，逐例回传进度与最终准确率。' },
]
const TYPE_LABELS = Object.fromEntries(TASK_TYPES.map((t) => [t.value, t.label]))

const STORAGE_KEY = 'data-agent:tasks:v1'
const TERMINAL_EVENTS = new Set(['done', 'error'])

// ---- 表单状态 ----
const taskType = ref('run_analysis_task')
const analysisQuestion = ref('')
const evalLimit = ref('')
const evalModel = ref('')
const submitting = ref(false)
const submitError = ref('')

const activeTypeDesc = computed(() => TASK_TYPES.find((t) => t.value === taskType.value)?.desc || '')
const canSubmit = computed(() => {
  if (taskType.value === 'run_analysis_task') return analysisQuestion.value.trim().length > 0
  return true // eval：参数都可选
})

// ---- 任务列表（localStorage 持久化本会话提交的 task_id）----
const tasks = ref(loadTasks())
const refreshing = ref(false)

function loadTasks() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr : []
  } catch (e) {
    return []
  }
}
function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tasks.value))
  } catch (e) { /* 存储不可用则忽略，纯前端记忆非关键路径 */ }
}

const typeLabel = (t) => TYPE_LABELS[t] || t
const typeInitial = (t) => (t === 'eval' ? 'E' : 'A')

const STATUS_MAP = {
  queued: { text: '排队中', cls: 'badge-neutral' },
  running: { text: '运行中', cls: 'badge-warning' },
  done: { text: '已完成', cls: 'badge-success' },
  failed: { text: '失败', cls: 'badge-error' },
  missing: { text: '已过期', cls: 'badge-neutral' },
}
const statusBadge = (s) => STATUS_MAP[s] || { text: s || '未知', cls: 'badge-neutral' }
const eventBadge = (t) => ({ started: 'badge-accent', progress: 'badge-neutral', done: 'badge-success', error: 'badge-error' }[t] || 'badge-neutral')

function formatTime(ms) {
  if (!ms) return ''
  const d = new Date(ms)
  return d.toLocaleString('zh-CN', { hour12: false })
}
function formatClock(ts) {
  if (!ts) return ''
  const d = new Date(ts)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleTimeString('zh-CN', { hour12: false })
}

// 带超时的 fetch：Redis/worker 不可达时后端可能挂起数秒，超时兜底避免前端无限转圈
async function fetchWithTimeout(url, options = {}, ms = 15000) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), ms)
  try {
    return await fetch(url, { ...options, signal: ctrl.signal })
  } finally {
    clearTimeout(timer)
  }
}
const errMsg = (e) => (e.name === 'AbortError' ? '请求超时（后端无响应，请确认 Redis/worker 已启动）' : e.message)

// ---- 提交 ----
async function submitTask() {
  if (submitting.value || !canSubmit.value) return
  submitError.value = ''
  submitting.value = true

  const type = taskType.value
  let params = {}
  let summary = ''
  if (type === 'run_analysis_task') {
    params = { question: analysisQuestion.value.trim() }
    summary = params.question
  } else {
    if (String(evalLimit.value).trim() !== '') params.limit = Number(evalLimit.value)
    if (evalModel.value.trim() !== '') params.model = evalModel.value.trim()
    summary = `评估${params.limit ? ` · 前 ${params.limit} 例` : ' · 全量'}${params.model ? ` · ${params.model}` : ''}`
  }

  try {
    const res = await fetchWithTimeout('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type, params }),
    })
    if (!res.ok) {
      submitError.value = await readError(res)
      return
    }
    const data = await res.json()
    const task = {
      task_id: data.task_id,
      type,
      summary,
      status: 'queued',
      createdAt: Date.now(),
    }
    tasks.value.unshift(task)
    persist()
    // 清空分析问题输入，评估参数保留以便连跑
    if (type === 'run_analysis_task') analysisQuestion.value = ''
    openTask(task)
  } catch (e) {
    submitError.value = errMsg(e)
  } finally {
    submitting.value = false
  }
}

async function readError(res) {
  try {
    const data = await res.json()
    return data.detail || `HTTP ${res.status}`
  } catch (e) {
    return `HTTP ${res.status}`
  }
}

// ---- 状态刷新 ----
async function refreshOne(task) {
  try {
    const res = await fetchWithTimeout(`/api/tasks/${encodeURIComponent(task.task_id)}`, {}, 12000)
    if (res.status === 404) {
      updateLocal(task.task_id, { status: 'missing' })
      return null
    }
    if (!res.ok) return null
    const doc = await res.json()
    updateLocal(task.task_id, { status: doc.status })
    return doc
  } catch (e) {
    return null
  }
}
async function refreshAll() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    await Promise.all(tasks.value.map(refreshOne))
  } finally {
    refreshing.value = false
  }
}
function updateLocal(taskId, patch) {
  const idx = tasks.value.findIndex((t) => t.task_id === taskId)
  if (idx > -1) {
    tasks.value[idx] = { ...tasks.value[idx], ...patch }
    persist()
  }
  if (active.task_id === taskId) Object.assign(active, patch)
}
function removeTask(task) {
  tasks.value = tasks.value.filter((t) => t.task_id !== task.task_id)
  persist()
}

// ---- 详情 + SSE ----
const detailVisible = ref(false)
const active = reactive({ task_id: '', type: '', status: '' })
const events = ref([])
const progress = ref(null)
const report = ref('')
const evalSummary = ref(null)
const detailError = ref('')
const timelineEl = ref(null)
let es = null

const isTerminal = computed(() => TERMINAL_EVENTS.has(active.status === 'failed' ? 'error' : active.status) || active.status === 'done' || active.status === 'failed')

const renderMarkdown = (t) => marked.parse(t || '')

async function openTask(task) {
  closeES()
  Object.assign(active, { task_id: task.task_id, type: task.type, status: task.status })
  events.value = []
  progress.value = null
  report.value = ''
  evalSummary.value = null
  detailError.value = ''
  detailVisible.value = true

  // 先同步一次状态；若已完成，稍后 done 事件也会触发结果拉取
  const doc = await refreshOne(task)
  if (doc && doc.status === 'done') await loadResult(task.task_id, doc)

  subscribe(task.task_id)
}

// EventSource 订阅：后端会从 stream 头部回放全部事件，再发终结事件后主动关闭。
// 断线不做自动重连（EventSource 默认会重连，这里在终结/错误时显式 close 掉）——
// 任务事件是一次性回放，重连只会重复拉全量，无收益；见 IMPLEMENTATION.md。
function subscribe(taskId) {
  let terminated = false
  let opened = false
  try {
    es = new EventSource(`/api/tasks/${encodeURIComponent(taskId)}/events`)
  } catch (e) {
    detailError.value = `无法建立事件连接：${e.message}`
    return
  }
  // 打开超时看门狗：Redis/worker 不可达时连接会长时间悬挂，超时给出清晰提示而非空转
  const watchdog = setTimeout(() => {
    if (!opened && !terminated) {
      detailError.value = '连接事件流超时（后端无响应或 Redis/worker 未就绪）'
      closeES()
    }
  }, 15000)
  es.onopen = () => { opened = true; clearTimeout(watchdog) }
  es.onmessage = (e) => {
    opened = true
    clearTimeout(watchdog)
    let data
    try { data = JSON.parse(e.data) } catch (err) { return }
    events.value.push(data)
    if (data.progress !== null && data.progress !== undefined) progress.value = data.progress
    scrollTimeline()

    if (TERMINAL_EVENTS.has(data.type)) {
      terminated = true
      if (data.type === 'done') {
        updateLocal(taskId, { status: 'done' })
        loadResult(taskId, null, data)
      } else {
        updateLocal(taskId, { status: 'failed' })
        detailError.value = data.message || '任务失败'
      }
      closeES()
    }
  }
  es.onerror = () => {
    clearTimeout(watchdog)
    // 正常终结时我们已 close()，此处只处理真正的连接异常
    if (!terminated) {
      detailError.value = '事件流连接中断（后端不可达或 Redis/worker 未就绪）'
      closeES()
    }
  }
}

// done 后拉取结果：分析任务渲染 result.report；评估任务展示准确率摘要。
async function loadResult(taskId, doc, doneEvent) {
  try {
    if (!doc) {
      const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`)
      if (!res.ok) return
      doc = await res.json()
    }
    const result = doc.result || {}
    if (active.type === 'run_analysis_task') {
      report.value = result.report || ''
    } else if (active.type === 'eval') {
      const s = result.summary || {}
      const acc = s.accuracy !== undefined ? s.accuracy : (doneEvent?.payload?.accuracy ?? result.accuracy)
      if (acc !== undefined && acc !== null) {
        evalSummary.value = {
          accuracy: acc,
          correct: s.correct ?? doneEvent?.payload?.correct ?? 0,
          total: s.total ?? doneEvent?.payload?.total ?? 0,
        }
      }
    }
  } catch (e) { /* 结果拉取失败不阻塞时间线展示 */ }
}

function scrollTimeline() {
  nextTick(() => {
    if (timelineEl.value) timelineEl.value.scrollTop = timelineEl.value.scrollHeight
  })
}
function closeES() {
  if (es) { es.close(); es = null }
}
function closeDetail() {
  closeES()
  detailVisible.value = false
}

onBeforeUnmount(closeES)

// 挂载即刷新一次历史任务状态
refreshAll()
</script>

<style scoped>
.tasks-view { display: flex; flex-direction: column; gap: 16px; padding: 20px; height: 100%; overflow-y: auto; }

.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 0; }

/* 提交区 */
.submit-card { margin-bottom: 0; }
.submit-grid { display: grid; grid-template-columns: 200px 1fr; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.field-wide { grid-column: 2 / -1; }
.field-label { font-size: 12.5px; color: var(--text-secondary); font-weight: 600; }
.field-hint { color: var(--text-muted); font-weight: 400; }
.submit-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 14px; }
.type-desc { font-size: 12px; color: var(--text-muted); }

.inline-error {
  margin-top: 12px; padding: 10px 12px; border-radius: var(--radius-sm);
  background: rgba(248, 113, 113, .1); border: 1px solid rgba(248, 113, 113, .3);
  color: var(--error-color); font-size: 13px;
}
.inline-error-hint { margin-top: 4px; color: var(--text-secondary); font-size: 12px; }
.inline-error code { font-family: var(--font-mono); font-size: 11.5px; color: var(--text-primary); }

/* 任务卡片 */
.task-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.task-card {
  background: var(--bg-surface); border: 1px solid var(--border-color);
  border-radius: var(--radius-lg); padding: 14px 16px; cursor: pointer;
  display: flex; flex-direction: column; gap: 10px;
  transition: border-color .15s ease, transform .15s ease, box-shadow .15s ease;
}
.task-card:hover { border-color: var(--border-strong); transform: translateY(-1px); box-shadow: var(--shadow); }
.task-card-head { display: flex; align-items: center; gap: 10px; }
.task-name-area { flex: 1; min-width: 0; }
.task-name { font-weight: 600; color: var(--text-primary); font-size: 13.5px; }
.task-id { font-size: 11.5px; color: var(--text-muted); font-family: var(--font-mono); }
.task-summary {
  font-size: 12.5px; color: var(--text-secondary); line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; min-height: 2.4em;
}
.task-card-foot { display: flex; align-items: center; justify-content: space-between; }
.task-time { font-size: 11.5px; color: var(--text-muted); font-family: var(--font-mono); }

/* 进度条 */
.progress-block { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
.progress-track { flex: 1; height: 8px; background: var(--bg-inset); border-radius: 999px; overflow: hidden; border: 1px solid var(--border-color); }
.progress-fill {
  height: 100%; border-radius: 999px;
  background: linear-gradient(90deg, var(--accent-strong), var(--accent));
  transition: width .3s ease;
}
.progress-fill.failed { background: var(--error-color); }
.progress-fill.indeterminate {
  width: 35%; background: linear-gradient(90deg, transparent, var(--accent), transparent);
  animation: slide 1.2s ease-in-out infinite;
}
@keyframes slide { 0% { margin-left: -35%; } 100% { margin-left: 100%; } }
.progress-pct { font-size: 12.5px; color: var(--text-secondary); font-family: var(--font-mono); min-width: 36px; text-align: right; }

/* 时间线 */
.timeline-head { font-size: 12px; font-weight: 600; letter-spacing: .4px; color: var(--text-muted); margin: 16px 0 8px; }
.timeline { max-height: 240px; overflow-y: auto; display: flex; flex-direction: column; gap: 2px; padding-right: 4px; }
.timeline-empty { display: flex; align-items: center; gap: 8px; color: var(--text-muted); font-size: 13px; padding: 8px 0; }
.event-row { display: flex; align-items: flex-start; gap: 10px; padding: 7px 8px; border-radius: var(--radius-sm); }
.event-row:hover { background: var(--bg-raised); }
.event-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-muted); margin-top: 6px; flex-shrink: 0; }
.event-started .event-dot { background: var(--accent); }
.event-done .event-dot { background: var(--success-color); }
.event-error .event-dot { background: var(--error-color); }
.event-body { flex: 1; min-width: 0; }
.event-msg { font-size: 13px; color: var(--text-primary); display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.event-type-badge { font-family: var(--font-mono); font-size: 10.5px; padding: 1px 7px; }
.event-progress { font-size: 11.5px; color: var(--text-muted); margin-top: 2px; }
.event-ts { font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); flex-shrink: 0; margin-top: 2px; }

.report-body { margin-top: 4px; padding: 14px 16px; background: var(--bg-inset); border: 1px solid var(--border-color); border-radius: var(--radius-sm); }

/* 评估摘要 */
.eval-summary { display: flex; gap: 40px; padding: 12px 4px; }
.eval-stat { display: flex; flex-direction: column; gap: 2px; }
.eval-value { font-size: 24px; font-weight: 700; color: var(--accent); font-family: var(--font-mono); }
.eval-label { font-size: 12px; color: var(--text-muted); }

@media (max-width: 640px) {
  .submit-grid { grid-template-columns: 1fr; }
  .field-wide { grid-column: 1 / -1; }
}
</style>
