<template>
  <div class="datasource-view">
    <div class="page-header card">
      <div>
        <h3 class="card-title">数据源与语义目录</h3>
        <p class="page-desc">自动扫描真实结构，AI 生成待审业务语义；只有人工审核结果会进入 Text-to-SQL。</p>
      </div>
      <button class="btn btn-secondary small" @click="loadSources" :disabled="loading">刷新</button>
    </div>

    <div v-if="error" class="inline-error">{{ error }}</div>
    <div v-if="notice" class="inline-note">{{ notice }}</div>

    <div class="datasource-layout">
      <aside class="source-panel card">
        <div class="card-header"><span class="card-title">数据源</span></div>
        <button
          v-for="source in sources"
          :key="source.id"
          :class="['source-item', { active: selectedId === source.id }]"
          @click="selectSource(source.id)"
        >
          <span>
            <strong>{{ source.name }}</strong>
            <small>{{ source.kind }} · {{ source.connection_summary }}</small>
          </span>
          <span :class="['badge', source.status === 'ready' ? 'badge-success' : 'badge-error']">
            {{ source.status }}
          </span>
        </button>
        <div v-if="!sources.length && !loading" class="empty-compact">暂无数据源</div>

        <div class="group-title">接入数据源</div>
        <div class="form-stack">
          <input v-model="form.name" class="input" placeholder="数据源名称" />
          <select v-model="form.kind">
            <option value="sqlite">SQLite</option>
            <option value="postgresql">PostgreSQL</option>
            <option value="mysql">MySQL</option>
          </select>
          <template v-if="form.kind === 'sqlite'">
            <input v-model="form.path" class="input" placeholder="data/datasources 内路径，如 sales.db" />
          </template>
          <template v-else>
            <input v-model="form.host" class="input" placeholder="数据库主机" />
            <div class="form-pair">
              <input v-model.number="form.port" class="input" type="number" placeholder="端口" />
              <input v-model="form.database" class="input" placeholder="数据库名" />
            </div>
            <input v-if="form.kind === 'postgresql'" v-model="form.schema" class="input" placeholder="Schema（可选，默认 public）" />
            <div class="form-pair">
              <input v-model="form.username" class="input" autocomplete="username" placeholder="只读用户名" />
              <input
                v-model="form.password"
                class="input"
                type="password"
                autocomplete="new-password"
                placeholder="密码"
              />
            </div>
            <select v-model="form.ssl_mode">
              <option value="require">要求 TLS</option>
              <option value="disable">关闭 TLS（仅可信网络）</option>
            </select>
          </template>
          <button class="btn btn-primary" @click="createSource" :disabled="saving || !form.name.trim()">
            <span v-if="saving" class="loading-spinner small"></span>
            <span v-else>连接、扫描并保存</span>
          </button>
        </div>
      </aside>

      <section class="catalog-panel">
        <div v-if="!selectedId" class="card empty-state">选择左侧数据源查看语义目录。</div>
        <template v-else>
          <div class="card detail-toolbar">
            <div>
              <div class="detail-title">{{ selectedSource?.name }}</div>
              <div class="page-desc">{{ selectedSource?.connection_summary }}</div>
            </div>
            <div class="toolbar-actions">
              <button class="btn btn-secondary small" @click="syncSource" :disabled="busy">同步结构</button>
              <button class="btn btn-primary small" @click="generateDrafts" :disabled="busy">AI 补充语义</button>
              <button class="btn btn-secondary small" @click="loadMSchema" :disabled="busy">预览 M-Schema</button>
              <button class="btn btn-danger small" @click="deleteSource" :disabled="busy">删除</button>
            </div>
          </div>

          <div v-if="catalogLoading" class="card loading-state"><span class="loading-spinner"></span>读取目录...</div>
          <div v-else class="table-list">
            <article v-for="table in catalogTables" :key="table.id" class="card table-card">
              <div class="table-head">
                <button class="table-toggle" @click="table.expanded = !table.expanded">
                  <span>{{ table.expanded ? '▾' : '▸' }}</span>
                  <strong>{{ table.table_name }}</strong>
                  <small>{{ table.schema_name }} · {{ table.columns.length }} 字段</small>
                </button>
                <span :class="['badge', reviewBadge(table.review_status)]">{{ table.review_status }}</span>
              </div>

              <div v-if="table.expanded" class="review-body">
                <label class="field-label">表业务语义</label>
                <textarea v-model="table.edit_comment" rows="2" placeholder="AI 草稿或人工修订后的表含义"></textarea>
                <div class="column-grid column-grid-head">
                  <span>字段 / 类型</span><span>业务语义</span><span>同义词（逗号分隔）</span>
                </div>
                <div v-for="column in table.columns" :key="column.id" class="column-grid">
                  <div class="column-name">
                    <strong>{{ column.column_name }}</strong>
                    <small>{{ column.data_type }}<template v-if="column.primary_key"> · PK</template></small>
                  </div>
                  <textarea v-model="column.edit_comment" rows="2"></textarea>
                  <input v-model="column.edit_synonyms" class="input" placeholder="订单号, 订单ID" />
                </div>
                <div class="review-actions">
                  <span class="review-hint">保存批准后才会进入 Agent；拒绝后仅保留数据库原生注释。</span>
                  <button class="btn btn-danger small" @click="reviewTable(table, 'rejected')" :disabled="busy">拒绝草稿</button>
                  <button class="btn btn-primary small" @click="reviewTable(table, 'approved')" :disabled="busy">审核并保存</button>
                </div>
              </div>
            </article>
            <div v-if="!catalogTables.length" class="card empty-state">当前 Schema 没有可用表。</div>
          </div>
        </template>
      </section>
    </div>

    <div v-if="mSchema" class="modal-mask" @click.self="mSchema = ''">
      <div class="modal-panel">
        <div class="modal-header">
          <div><div class="modal-title">正式 M-Schema</div><div class="modal-sub">只包含已审核语义与物理注释</div></div>
          <button class="btn btn-secondary small" @click="mSchema = ''">关闭</button>
        </div>
        <div class="modal-body"><pre class="schema-preview">{{ mSchema }}</pre></div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'

const SELECTED_KEY = 'data-agent:selected-datasource'
const sources = ref([])
const selectedId = ref(Number(localStorage.getItem(SELECTED_KEY)) || null)
const catalogTables = ref([])
const loading = ref(false)
const catalogLoading = ref(false)
const saving = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const mSchema = ref('')

const form = reactive({
  name: '', kind: 'sqlite', path: '', host: '', port: 5432,
  database: '', schema: '', username: '', password: '', ssl_mode: 'require',
})

const selectedSource = computed(() => sources.value.find((source) => source.id === selectedId.value))

watch(() => form.kind, (kind) => {
  form.port = kind === 'postgresql' ? 5432 : kind === 'mysql' ? 3306 : null
  form.ssl_mode = kind === 'sqlite' ? '' : 'require'
})

function reviewBadge(status) {
  if (status === 'approved') return 'badge-success'
  if (status === 'rejected') return 'badge-error'
  return 'badge-warning'
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`)
  return payload
}

function hydrateTables(tables) {
  return (tables || []).map((table) => ({
    ...table,
    expanded: table.review_status === 'pending',
    edit_comment: table.reviewed_comment || table.ai_comment || table.physical_comment || '',
    columns: table.columns.map((column) => ({
      ...column,
      edit_comment: column.reviewed_comment || column.ai_comment || column.physical_comment || '',
      edit_synonyms: (column.reviewed_synonyms?.length ? column.reviewed_synonyms : column.ai_synonyms || []).join(', '),
    })),
  }))
}

async function loadSources() {
  loading.value = true; error.value = ''
  try {
    sources.value = await api('/api/datasources')
    if (selectedId.value && !sources.value.some((source) => source.id === selectedId.value)) selectedId.value = null
    if (!selectedId.value && sources.value.length) {
      selectedId.value = sources.value[0].id
      localStorage.setItem(SELECTED_KEY, String(selectedId.value))
    }
    if (selectedId.value) await loadCatalog()
  } catch (e) { error.value = `加载数据源失败：${e.message}` }
  finally { loading.value = false }
}

async function selectSource(id) {
  selectedId.value = id
  localStorage.setItem(SELECTED_KEY, String(id))
  await loadCatalog()
}

async function loadCatalog() {
  if (!selectedId.value) return
  catalogLoading.value = true; error.value = ''
  try {
    const catalog = await api(`/api/datasources/${selectedId.value}/metadata`)
    catalogTables.value = hydrateTables(catalog.tables)
  } catch (e) { error.value = `读取目录失败：${e.message}` }
  finally { catalogLoading.value = false }
}

async function createSource() {
  saving.value = true; error.value = ''; notice.value = ''
  try {
    const payload = form.kind === 'sqlite'
      ? { name: form.name, kind: form.kind, path: form.path }
      : { name: form.name, kind: form.kind, host: form.host, port: form.port, database: form.database,
          schema: form.schema || null, username: form.username, password: form.password, ssl_mode: form.ssl_mode }
    const created = await api('/api/datasources', { method: 'POST', body: JSON.stringify(payload) })
    form.password = ''
    await loadSources()
    await selectSource(created.id)
    notice.value = '数据源已连接并完成首次结构扫描。下一步可生成 AI 语义草稿。'
  } catch (e) { error.value = `接入失败：${e.message}` }
  finally { saving.value = false }
}

async function syncSource() {
  busy.value = true; error.value = ''
  try {
    await api(`/api/datasources/${selectedId.value}/sync`, { method: 'POST' })
    await loadCatalog(); await loadSources()
    notice.value = '物理结构已同步；新增或变化对象已重新置为待审核。'
  } catch (e) { error.value = `同步失败：${e.message}` }
  finally { busy.value = false }
}

async function generateDrafts() {
  busy.value = true; error.value = ''
  try {
    await api(`/api/datasources/${selectedId.value}/semantic-draft`, { method: 'POST', body: '{}' })
    await loadCatalog()
    notice.value = 'AI 语义草稿已生成，请逐表核对后保存。'
  } catch (e) { error.value = `生成草稿失败：${e.message}` }
  finally { busy.value = false }
}

async function reviewTable(table, decision) {
  busy.value = true; error.value = ''
  try {
    const columns = decision === 'approved' ? table.columns.map((column) => ({
      name: column.column_name,
      comment: column.edit_comment,
      synonyms: column.edit_synonyms.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
    })) : []
    await api(`/api/datasources/${selectedId.value}/metadata/${table.id}/review`, {
      method: 'PUT',
      body: JSON.stringify({ decision, table_comment: table.edit_comment, columns }),
    })
    await loadCatalog()
    notice.value = decision === 'approved' ? `${table.table_name} 语义已审核并生效。` : `${table.table_name} 草稿已拒绝。`
  } catch (e) { error.value = `审核失败：${e.message}` }
  finally { busy.value = false }
}

async function loadMSchema() {
  busy.value = true; error.value = ''
  try { mSchema.value = (await api(`/api/datasources/${selectedId.value}/m-schema`)).m_schema }
  catch (e) { error.value = `读取 M-Schema 失败：${e.message}` }
  finally { busy.value = false }
}

async function deleteSource() {
  if (!confirm(`确认删除数据源「${selectedSource.value?.name}」及其语义元数据？`)) return
  busy.value = true; error.value = ''
  try {
    await api(`/api/datasources/${selectedId.value}`, { method: 'DELETE' })
    localStorage.removeItem(SELECTED_KEY)
    selectedId.value = null; catalogTables.value = []
    await loadSources()
  } catch (e) { error.value = `删除失败：${e.message}` }
  finally { busy.value = false }
}

onMounted(loadSources)
</script>

<style scoped>
.datasource-view { display: flex; flex-direction: column; gap: 14px; }
.page-header, .detail-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.datasource-layout { display: grid; grid-template-columns: 310px minmax(0, 1fr); gap: 14px; align-items: start; }
.source-panel { position: sticky; top: 0; }
.source-item { width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 10px; border: 1px solid transparent; border-radius: 8px; background: transparent; color: var(--text-primary); text-align: left; cursor: pointer; }
.source-item:hover, .source-item.active { background: var(--bg-raised); border-color: var(--border-strong); }
.source-item strong, .source-item small { display: block; }
.source-item small { color: var(--text-muted); margin-top: 2px; max-width: 190px; overflow: hidden; text-overflow: ellipsis; }
.form-stack { display: flex; flex-direction: column; gap: 8px; }
.form-pair { display: grid; grid-template-columns: 1fr 1.5fr; gap: 8px; }
.catalog-panel, .table-list { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.toolbar-actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
.detail-title { font-weight: 650; font-size: 15px; }
.table-card { padding: 0; overflow: hidden; }
.table-head { display: flex; align-items: center; justify-content: space-between; padding: 12px 14px; }
.table-toggle { display: flex; align-items: center; gap: 8px; border: 0; background: transparent; color: var(--text-primary); cursor: pointer; text-align: left; }
.table-toggle small { color: var(--text-muted); font-family: var(--font-mono); }
.review-body { border-top: 1px solid var(--border-color); padding: 14px; display: flex; flex-direction: column; gap: 10px; }
.field-label { font-size: 12px; color: var(--text-muted); }
.column-grid { display: grid; grid-template-columns: minmax(130px, .7fr) minmax(220px, 1.5fr) minmax(170px, 1fr); gap: 10px; align-items: center; }
.column-grid-head { color: var(--text-muted); font-size: 11px; border-bottom: 1px solid var(--border-color); padding-bottom: 6px; }
.column-name strong, .column-name small { display: block; }
.column-name small { color: var(--text-muted); font-family: var(--font-mono); }
.review-actions { display: flex; justify-content: flex-end; align-items: center; gap: 8px; padding-top: 4px; }
.review-hint { margin-right: auto; color: var(--text-muted); font-size: 12px; }
.schema-preview { white-space: pre-wrap; font-family: var(--font-mono); font-size: 12px; line-height: 1.7; color: var(--text-secondary); }
.empty-compact { color: var(--text-muted); padding: 12px; text-align: center; }
@media (max-width: 980px) {
  .datasource-layout { grid-template-columns: 1fr; }
  .source-panel { position: static; }
  .column-grid { grid-template-columns: 1fr; padding: 8px 0; border-bottom: 1px solid var(--border-color); }
  .column-grid-head { display: none; }
}
</style>
