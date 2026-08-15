<template>
  <div class="knowledge-view">
    <!-- 顶部 -->
    <div class="page-header card">
      <div class="page-header-left">
        <h3 class="card-title">知识管理</h3>
        <p class="page-desc">运营闭环：把答对的问答存进 SQL 示例库（越攒越准），维护业务术语口径（GMV / 复购率 / 客单价…）。</p>
      </div>
      <button class="btn btn-secondary" @click="reloadActive" :disabled="loading">
        <span v-if="loading" class="loading-spinner small"></span>
        <span v-else>刷新</span>
      </button>
    </div>

    <!-- Tab 切换 -->
    <div class="tab-bar">
      <button :class="['tab', { active: activeTab === 'examples' }]" @click="switchTab('examples')">
        SQL 示例<span class="tab-count">{{ examples.length }}</span>
      </button>
      <button :class="['tab', { active: activeTab === 'terms' }]" @click="switchTab('terms')">
        业务术语<span class="tab-count">{{ terms.length }}</span>
      </button>
    </div>

    <!-- ============ SQL 示例 ============ -->
    <template v-if="activeTab === 'examples'">
      <div class="card">
        <div class="card-header"><span class="card-title">新增 SQL 示例</span></div>
        <div class="form-grid">
          <label class="field field-wide">
            <span class="field-label">自然语言问题 <span class="req">*</span></span>
            <textarea v-model="exForm.question" class="input" rows="2" placeholder="例：2018 年各月的 GMV 趋势"></textarea>
          </label>
          <label class="field field-wide">
            <span class="field-label">对应 SQL <span class="req">*</span></span>
            <textarea v-model="exForm.sql" class="input mono" rows="3" placeholder="SELECT ..."></textarea>
          </label>
          <label class="check-field">
            <input type="checkbox" v-model="exForm.verified" />
            <span>已人工确认结果正确</span>
          </label>
        </div>
        <div class="form-foot">
          <span v-if="exError" class="inline-error">{{ exError }}</span>
          <span v-else></span>
          <button class="btn btn-primary" @click="addExample" :disabled="exSubmitting">
            <span v-if="exSubmitting" class="loading-spinner small"></span>
            <span v-else>保存示例</span>
          </button>
        </div>
      </div>

      <div v-if="loading" class="loading-state card"><div class="loading-spinner"></div></div>
      <div v-else-if="examples.length === 0" class="empty-state card"><p>还没有 SQL 示例。答对的问答存进来，检索时即可命中复用。</p></div>
      <template v-else>
        <!-- 候选示例：对话待确认 / 评测失败导入，人工转正后才进 few-shot -->
        <template v-if="candidateExamples.length > 0">
          <div class="group-title">候选示例（{{ candidateExamples.length }}）—— 转正后才会注入 few-shot</div>
          <div class="item-list">
            <div v-for="ex in candidateExamples" :key="ex.id" class="card item-card candidate">
              <div class="item-head">
                <div class="item-question">{{ ex.question }}</div>
                <div class="item-actions">
                  <span class="badge" :class="ex.source === 'eval' ? 'badge-info' : 'badge-warning'">{{ ex.source === 'eval' ? '评测导入' : '待确认' }}</span>
                  <button class="btn small btn-primary" @click="verifyExample(ex)" :disabled="deletingId === ex.id">转正</button>
                  <button class="btn small btn-danger" @click="deleteExample(ex)" :disabled="deletingId === ex.id">
                    <span v-if="deletingId === ex.id" class="loading-spinner small"></span>
                    <span v-else>丢弃</span>
                  </button>
                </div>
              </div>
              <pre class="item-sql"><code>{{ ex.sql }}</code></pre>
              <!-- 评测导入的错误模式标注：模型当时的错误 SQL 与报错，辅助人工判断 -->
              <div v-if="ex.source === 'eval' && ex.meta && ex.meta.pred_sql" class="eval-meta">
                <span class="eval-meta-label">模型当时生成（错误）：</span>
                <pre class="item-sql muted"><code>{{ ex.meta.pred_sql }}</code></pre>
                <p v-if="ex.meta.error" class="eval-error">{{ ex.meta.error }}</p>
              </div>
              <div class="item-id">{{ ex.id }}</div>
            </div>
          </div>
        </template>

        <div v-if="verifiedExamples.length > 0" class="group-title">已生效示例（{{ verifiedExamples.length }}）—— 参与 few-shot 注入</div>
        <div class="item-list">
          <div v-for="ex in verifiedExamples" :key="ex.id" class="card item-card">
            <div class="item-head">
              <div class="item-question">{{ ex.question }}</div>
              <div class="item-actions">
                <span class="badge badge-success">已确认</span>
                <button class="btn small btn-danger" @click="deleteExample(ex)" :disabled="deletingId === ex.id">
                  <span v-if="deletingId === ex.id" class="loading-spinner small"></span>
                  <span v-else>删除</span>
                </button>
              </div>
            </div>
            <pre class="item-sql"><code>{{ ex.sql }}</code></pre>
            <div class="item-id">{{ ex.id }}</div>
          </div>
        </div>
      </template>
    </template>

    <!-- ============ 业务术语 ============ -->
    <template v-else>
      <div class="card">
        <div class="card-header"><span class="card-title">新增 / 更新术语</span></div>
        <div class="form-grid">
          <label class="field">
            <span class="field-label">术语 <span class="req">*</span></span>
            <input v-model="termForm.term" class="input" placeholder="如 GMV" />
          </label>
          <label class="field">
            <span class="field-label">同义词 <span class="field-hint">（顿号 / 逗号分隔）</span></span>
            <input v-model="termForm.synonyms" class="input" placeholder="成交额、总销售额" />
          </label>
          <label class="field field-wide">
            <span class="field-label">口径定义</span>
            <textarea v-model="termForm.definition" class="input" rows="2" placeholder="商品交易总额，SUM(order_items.price)"></textarea>
          </label>
          <label class="field field-wide">
            <span class="field-label">SQL 计算提示 <span class="field-hint">（可选）</span></span>
            <textarea v-model="termForm.sql_hint" class="input mono" rows="2" placeholder="SUM(oi.price) ..."></textarea>
          </label>
        </div>
        <div class="form-foot">
          <span v-if="termError" class="inline-error">{{ termError }}</span>
          <span v-else class="upsert-hint">term 为唯一键，重名即更新</span>
          <button class="btn btn-primary" @click="addTerm" :disabled="termSubmitting">
            <span v-if="termSubmitting" class="loading-spinner small"></span>
            <span v-else>保存术语</span>
          </button>
        </div>
      </div>

      <div v-if="loading" class="loading-state card"><div class="loading-spinner"></div></div>
      <div v-else-if="terms.length === 0" class="empty-state card"><p>还没有业务术语。补录 GMV / 复购率 等口径，可提升 Text-to-SQL 的术语理解。</p></div>
      <div v-else class="item-list">
        <div v-for="t in terms" :key="t.term" class="card item-card">
          <div class="item-head">
            <div class="term-name">{{ t.term }}</div>
            <button class="btn small btn-danger" @click="deleteTerm(t)" :disabled="deletingId === t.term">
              <span v-if="deletingId === t.term" class="loading-spinner small"></span>
              <span v-else>删除</span>
            </button>
          </div>
          <div v-if="t.synonyms && t.synonyms.length" class="term-syn">
            <span class="syn-label">同义词</span>
            <span v-for="(s, i) in t.synonyms" :key="i" class="source-tag">{{ s }}</span>
          </div>
          <p v-if="t.definition" class="term-def">{{ t.definition }}</p>
          <pre v-if="t.sql_hint" class="item-sql"><code>{{ t.sql_hint }}</code></pre>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'

const activeTab = ref('examples')
const loading = ref(false)
const deletingId = ref('')

// ---- SQL 示例 ----
const examples = ref([])
const exForm = ref({ question: '', sql: '', verified: true })
const exError = ref('')
const exSubmitting = ref(false)

// 候选（verified=false：评测导入/待确认）与已生效分组渲染
const verifiedExamples = computed(() => examples.value.filter((e) => e.verified))
const candidateExamples = computed(() => examples.value.filter((e) => !e.verified))

async function loadExamples() {
  loading.value = true
  try {
    const res = await fetch('/api/sql-examples')
    examples.value = res.ok ? await res.json() : []
  } catch (e) {
    examples.value = []
  } finally {
    loading.value = false
  }
}

async function addExample() {
  exError.value = ''
  const question = exForm.value.question.trim()
  const sql = exForm.value.sql.trim()
  if (!question) { exError.value = '请填写问题'; return }
  if (!sql) { exError.value = '请填写 SQL'; return }
  exSubmitting.value = true
  try {
    const res = await fetch('/api/sql-examples', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, sql, verified: exForm.value.verified }),
    })
    if (!res.ok) { exError.value = await readError(res); return }
    const created = await res.json()
    // 同问题为更新：先按 id 去重再插入
    examples.value = [created, ...examples.value.filter((e) => e.id !== created.id)]
    exForm.value = { question: '', sql: '', verified: true }
  } catch (e) {
    exError.value = `请求异常：${e.message}`
  } finally {
    exSubmitting.value = false
  }
}

async function verifyExample(ex) {
  // 转正 = 同作用域同问题覆盖写入 verified=true（后端 upsert 语义）
  if (deletingId.value) return
  deletingId.value = ex.id
  try {
    const res = await fetch('/api/sql-examples', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question: ex.question,
        sql: ex.sql,
        verified: true,
        datasource_id: ex.datasource_id ?? null,
        source: ex.source === 'eval' ? 'manual' : (ex.source || 'manual'),
      }),
    })
    if (res.ok) {
      const created = await res.json()
      examples.value = examples.value.filter((e) => e.id !== created.id)
      examples.value = [created, ...examples.value]
    } else {
      alert(`转正失败：${await readError(res)}`)
    }
  } catch (e) {
    alert(`转正失败：${e.message}`)
  } finally {
    deletingId.value = ''
  }
}

async function deleteExample(ex) {
  if (deletingId.value) return
  deletingId.value = ex.id
  try {
    const res = await fetch(`/api/sql-examples/${encodeURIComponent(ex.id)}`, { method: 'DELETE' })
    if (res.ok || res.status === 204) {
      examples.value = examples.value.filter((e) => e.id !== ex.id)
    } else {
      alert(`删除失败：${await readError(res)}`)
    }
  } catch (e) {
    alert(`删除失败：${e.message}`)
  } finally {
    deletingId.value = ''
  }
}

// ---- 业务术语 ----
const terms = ref([])
const termForm = ref({ term: '', synonyms: '', definition: '', sql_hint: '' })
const termError = ref('')
const termSubmitting = ref(false)

async function loadTerms() {
  loading.value = true
  try {
    const res = await fetch('/api/terminology')
    terms.value = res.ok ? await res.json() : []
  } catch (e) {
    terms.value = []
  } finally {
    loading.value = false
  }
}

function parseSynonyms(raw) {
  return (raw || '')
    .split(/[、,，\s]+/)
    .map((s) => s.trim())
    .filter(Boolean)
}

async function addTerm() {
  termError.value = ''
  const term = termForm.value.term.trim()
  if (!term) { termError.value = '请填写术语'; return }
  termSubmitting.value = true
  try {
    const body = {
      term,
      synonyms: parseSynonyms(termForm.value.synonyms),
      definition: termForm.value.definition.trim(),
      sql_hint: termForm.value.sql_hint.trim() || null,
    }
    const res = await fetch('/api/terminology', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) { termError.value = await readError(res); return }
    const created = await res.json()
    // term 唯一键，upsert：去重后置顶
    terms.value = [created, ...terms.value.filter((t) => t.term !== created.term)]
    termForm.value = { term: '', synonyms: '', definition: '', sql_hint: '' }
  } catch (e) {
    termError.value = `请求异常：${e.message}`
  } finally {
    termSubmitting.value = false
  }
}

async function deleteTerm(t) {
  if (deletingId.value) return
  deletingId.value = t.term
  try {
    const res = await fetch(`/api/terminology/${encodeURIComponent(t.term)}`, { method: 'DELETE' })
    if (res.ok || res.status === 204) {
      terms.value = terms.value.filter((x) => x.term !== t.term)
    } else {
      alert(`删除失败：${await readError(res)}`)
    }
  } catch (e) {
    alert(`删除失败：${e.message}`)
  } finally {
    deletingId.value = ''
  }
}

// ---- 公共 ----
async function readError(res) {
  try {
    const d = await res.json()
    if (Array.isArray(d.detail)) return d.detail.map((x) => x.msg || JSON.stringify(x)).join('；')
    return d.detail || `HTTP ${res.status}`
  } catch (e) {
    return `HTTP ${res.status}`
  }
}

function switchTab(tab) {
  if (activeTab.value === tab) return
  activeTab.value = tab
  reloadActive()
}
function reloadActive() {
  if (activeTab.value === 'examples') loadExamples()
  else loadTerms()
}

// 挂载即并行预取两个列表，使 tab 计数一开始就准确（非活动 tab 也显示真实数量）
loadExamples()
loadTerms()
</script>

<style scoped>
.knowledge-view { display: flex; flex-direction: column; gap: 16px; padding: 20px; height: 100%; overflow-y: auto; }
.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 0; }
.card { margin-bottom: 0; }

/* Tab 栏 */
.tab-bar { display: flex; gap: 4px; border-bottom: 1px solid var(--border-color); }
.tab {
  position: relative; padding: 10px 18px; background: transparent; border: none; cursor: pointer;
  color: var(--text-secondary); font-size: 13.5px; font-weight: 600; font-family: inherit;
  display: flex; align-items: center; gap: 8px; transition: color .15s ease;
}
.tab:hover { color: var(--text-primary); }
.tab.active { color: var(--accent); }
.tab.active::after {
  content: ''; position: absolute; left: 12px; right: 12px; bottom: -1px; height: 2px;
  background: var(--accent); border-radius: 2px;
}
.tab-count {
  font-size: 11px; font-family: var(--font-mono);
  background: var(--bg-raised); border: 1px solid var(--border-strong);
  border-radius: 999px; padding: 0 7px; color: var(--text-secondary);
}
.tab.active .tab-count { background: var(--accent-soft); border-color: var(--accent-ring); color: var(--accent); }

/* 表单 */
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.field-wide { grid-column: 1 / -1; }
.field-label { font-size: 12.5px; color: var(--text-secondary); font-weight: 600; }
.field-hint { color: var(--text-muted); font-weight: 400; }
.req { color: var(--error-color); }
.mono { font-family: var(--font-mono); font-size: 12.5px; }
.check-field { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-secondary); cursor: pointer; }
.check-field input { width: 15px; height: 15px; accent-color: var(--accent-strong); }
.form-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 14px; }
.upsert-hint { font-size: 12px; color: var(--text-muted); }
.inline-error {
  padding: 6px 10px; border-radius: var(--radius-sm); font-size: 12.5px;
  background: rgba(248, 113, 113, .1); border: 1px solid rgba(248, 113, 113, .3); color: var(--error-color);
}

/* 列表 */
.item-list { display: flex; flex-direction: column; gap: 12px; }
.item-card { display: flex; flex-direction: column; gap: 10px; }
.item-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.item-question { font-size: 14px; font-weight: 600; color: var(--text-primary); line-height: 1.5; }
.item-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.item-sql {
  background: var(--bg-inset); border: 1px solid var(--border-color); border-radius: var(--radius-sm);
  padding: 10px 12px; overflow-x: auto; margin: 0;
}
.item-sql code { font-family: var(--font-mono); font-size: 12.5px; color: #a5f3e8; white-space: pre; }
.item-id { font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); }

/* 分组标题与候选卡片 */
.group-title {
  font-size: 12.5px; font-weight: 600; color: var(--text-secondary);
  padding: 4px 2px 0;
}
.item-card.candidate { border-left: 3px solid rgba(251, 191, 36, .6); }
.eval-meta { display: flex; flex-direction: column; gap: 6px; }
.eval-meta-label { font-size: 12px; color: var(--text-muted); }
.item-sql.muted code { color: var(--text-muted); }
.eval-error { font-size: 12px; color: var(--error-color); margin: 0; font-family: var(--font-mono); }

.term-name { font-size: 15px; font-weight: 700; color: var(--text-primary); }
.term-syn { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.syn-label { font-size: 11.5px; color: var(--text-muted); }
.term-def { font-size: 13px; color: var(--text-secondary); line-height: 1.6; }

@media (max-width: 640px) {
  .form-grid { grid-template-columns: 1fr; }
}
</style>
