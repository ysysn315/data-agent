<template>
  <div class="skills-view">
    <!-- 顶部：标题 + 搜索 + 刷新 -->
    <div class="page-header card">
      <div class="page-header-left">
        <h3 class="card-title">🧩 Skills 管理</h3>
        <p class="page-desc">技能以目录形式挂载（SKILL.md + 可选脚本），启用后其声明的门控工具对 Agent 可见。</p>
      </div>
      <div class="page-header-actions">
        <input v-model="searchQuery" class="input search-input" placeholder="搜索技能名称 / slug / 描述..." />
        <button class="btn btn-secondary" @click="fetchSkills" :disabled="loading">刷新</button>
      </div>
    </div>

    <!-- 加载 / 空态 -->
    <div v-if="loading" class="loading-state card">
      <div class="loading-spinner"></div>
    </div>
    <div v-else-if="visibleGroups.length === 0" class="empty-state card">
      <p>{{ searchQuery ? '没有匹配的 Skill，换个关键词试试。' : '还没有可用的 Skill。' }}</p>
    </div>

    <!-- 按来源分组展示卡片 -->
    <template v-else>
      <div v-for="group in visibleGroups" :key="group.key" class="skill-group">
        <div class="group-title">{{ group.title }}（{{ group.skills.length }}）</div>
        <div class="skill-grid">
          <div
            v-for="skill in group.skills"
            :key="skill.slug"
            class="skill-card"
            :class="{ disabled: !skill.enabled }"
            @click="openDetail(skill)"
          >
            <div class="skill-card-head">
              <span class="skill-icon">🪄</span>
              <div class="skill-name-area">
                <div class="skill-name">{{ skill.name }}</div>
                <div class="skill-slug">{{ skill.slug }}</div>
              </div>
              <span class="badge" :class="skill.enabled ? 'badge-success' : 'badge-error'">
                {{ skill.enabled ? '已启用' : '已禁用' }}
              </span>
            </div>
            <p class="skill-desc">{{ skill.description || '暂无描述' }}</p>
            <div class="skill-card-foot">
              <span class="badge badge-neutral">{{ sourceLabel(skill.source_type) }}</span>
              <button
                class="btn small"
                :class="skill.enabled ? 'btn-danger' : 'btn-primary'"
                :disabled="togglingSlug === skill.slug"
                @click.stop="toggleEnabled(skill)"
              >
                <span v-if="togglingSlug === skill.slug" class="loading-spinner small"></span>
                <span v-else>{{ skill.enabled ? '禁用' : '启用' }}</span>
              </button>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- 详情弹窗：frontmatter + 正文 markdown -->
    <div v-if="detailVisible" class="modal-mask" @click.self="closeDetail">
      <div class="modal-panel card">
        <div class="modal-header">
          <div>
            <div class="modal-title">{{ detail?.name || activeSlug }}</div>
            <div class="modal-sub">
              <span class="badge badge-neutral">{{ sourceLabel(detail?.source_type) }}</span>
              <span v-if="detail" class="badge" :class="detail.enabled ? 'badge-success' : 'badge-error'">
                {{ detail.enabled ? '已启用' : '已禁用' }}
              </span>
            </div>
          </div>
          <button class="btn btn-secondary small" @click="closeDetail">关闭</button>
        </div>

        <div class="modal-body">
          <div v-if="detailLoading" class="loading-state">
            <div class="loading-spinner"></div>
          </div>
          <template v-else-if="detail">
            <!-- frontmatter 元数据 -->
            <div v-if="frontmatterEntries.length" class="frontmatter-block">
              <div class="frontmatter-title">元数据（frontmatter）</div>
              <table class="frontmatter-table">
                <tr v-for="[key, val] in frontmatterEntries" :key="key">
                  <td class="fm-key">{{ key }}</td>
                  <td class="fm-val">{{ formatValue(val) }}</td>
                </tr>
              </table>
            </div>
            <!-- 正文 markdown -->
            <div class="markdown-content skill-body" v-html="renderMarkdown(detail.body)"></div>
          </template>
          <div v-else class="empty-state">
            <p>{{ detailError || '读取失败' }}</p>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { marked } from 'marked'
import hljs from 'highlight.js'

marked.setOptions({
  highlight: (code, lang) => {
    if (lang && hljs.getLanguage(lang)) {
      return hljs.highlight(code, { language: lang }).value
    }
    return hljs.highlightAuto(code).value
  }
})

const skills = ref([])
const loading = ref(false)
const searchQuery = ref('')
const togglingSlug = ref('')

const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref(null)
const detailError = ref('')
const activeSlug = ref('')

const SOURCE_LABELS = { builtin: '内置', upload: '上传', remote: '远程' }
const sourceLabel = (type) => SOURCE_LABELS[type] || type || '未知'

const renderMarkdown = (text) => marked.parse(text || '')

const formatValue = (val) => {
  if (Array.isArray(val)) return val.join('、')
  if (val && typeof val === 'object') return JSON.stringify(val)
  return String(val ?? '')
}

const frontmatterEntries = computed(() => {
  const fm = detail.value?.frontmatter
  if (!fm || typeof fm !== 'object') return []
  return Object.entries(fm).filter(([, v]) => v !== null && v !== undefined && v !== '')
})

const matchesSearch = (skill) => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return true
  return [skill.name, skill.slug, skill.description]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
    .includes(q)
}

// 按来源（内置 / 上传 / 远程）分组，只展示非空分组
const visibleGroups = computed(() => {
  const filtered = skills.value.filter(matchesSearch)
  return [
    { key: 'builtin', title: '内置', skills: filtered.filter((s) => s.source_type === 'builtin') },
    { key: 'upload', title: '上传', skills: filtered.filter((s) => s.source_type === 'upload') },
    { key: 'remote', title: '远程', skills: filtered.filter((s) => s.source_type === 'remote') }
  ].filter((g) => g.skills.length > 0)
})

const fetchSkills = async () => {
  loading.value = true
  try {
    // enabled_only=false 拿到全部技能（含被禁用的），以便在页面里启停
    const res = await fetch('/api/skills?enabled_only=false')
    skills.value = res.ok ? await res.json() : []
  } catch (e) {
    skills.value = []
  } finally {
    loading.value = false
  }
}

const toggleEnabled = async (skill) => {
  if (togglingSlug.value) return
  togglingSlug.value = skill.slug
  const action = skill.enabled ? 'disable' : 'enable'
  try {
    const res = await fetch(`/api/skills/${skill.slug}/${action}`, { method: 'POST' })
    if (res.ok) {
      const updated = await res.json()
      const idx = skills.value.findIndex((s) => s.slug === skill.slug)
      if (idx > -1) skills.value[idx] = updated
      if (detail.value?.slug === skill.slug) detail.value.enabled = updated.enabled
    } else {
      alert(`操作失败：${res.status}`)
    }
  } catch (e) {
    alert('操作失败：' + e.message)
  } finally {
    togglingSlug.value = ''
  }
}

const openDetail = async (skill) => {
  activeSlug.value = skill.slug
  detail.value = null
  detailError.value = ''
  detailLoading.value = true
  detailVisible.value = true
  try {
    const res = await fetch(`/api/skills/${skill.slug}`)
    if (res.ok) {
      detail.value = await res.json()
    } else {
      detailError.value = `读取失败：${res.status}`
    }
  } catch (e) {
    detailError.value = '读取失败：' + e.message
  } finally {
    detailLoading.value = false
  }
}

const closeDetail = () => {
  detailVisible.value = false
}

fetchSkills()
</script>

<style scoped>
.skills-view {
  display: flex;
  flex-direction: column;
  gap: 20px;
  padding: 20px;
  height: 100%;
  overflow-y: auto;
}

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.page-desc {
  color: var(--text-secondary);
  font-size: 0.9rem;
  margin-top: 6px;
}

.page-header-actions {
  display: flex;
  gap: 12px;
  flex-shrink: 0;
}

.search-input {
  width: 260px;
}

.skill-group {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.group-title {
  color: var(--text-secondary);
  font-size: 0.9rem;
  font-weight: 600;
}

.skill-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 16px;
}

.skill-card {
  background-color: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  padding: 16px;
  cursor: pointer;
  transition: border-color 0.2s, transform 0.2s;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.skill-card:hover {
  border-color: var(--primary-color);
  transform: translateY(-2px);
}

.skill-card.disabled {
  opacity: 0.7;
}

.skill-card-head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.skill-icon {
  font-size: 1.4rem;
}

.skill-name-area {
  flex: 1;
  min-width: 0;
}

.skill-name {
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.skill-slug {
  font-size: 0.75rem;
  color: var(--text-muted);
  font-family: monospace;
}

.skill-desc {
  color: var(--text-secondary);
  font-size: 0.85rem;
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 2.6em;
}

.skill-card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.badge-neutral {
  background-color: var(--bg-tertiary);
  color: var(--text-secondary);
}

.btn.small {
  padding: 6px 14px;
  font-size: 0.8rem;
}

.loading-spinner.small {
  width: 14px;
  height: 14px;
  border-width: 2px;
}

.loading-state,
.empty-state {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px;
  color: var(--text-muted);
}

/* 详情弹窗 */
.modal-mask {
  position: fixed;
  inset: 0;
  background-color: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
  padding: 24px;
}

.modal-panel {
  width: 100%;
  max-width: 720px;
  max-height: 82vh;
  display: flex;
  flex-direction: column;
}

.modal-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.modal-title {
  font-size: 1.15rem;
  font-weight: 700;
}

.modal-sub {
  display: flex;
  gap: 8px;
  margin-top: 6px;
}

.modal-body {
  overflow-y: auto;
  flex: 1;
}

.frontmatter-block {
  background-color: var(--bg-tertiary);
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 16px;
}

.frontmatter-title {
  color: var(--text-secondary);
  font-size: 0.85rem;
  font-weight: 600;
  margin-bottom: 8px;
}

.frontmatter-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}

.frontmatter-table td {
  padding: 4px 8px;
  vertical-align: top;
}

.fm-key {
  color: var(--text-muted);
  font-family: monospace;
  white-space: nowrap;
  width: 1%;
}

.fm-val {
  color: var(--text-primary);
  word-break: break-word;
}

.skill-body {
  padding: 4px;
}
</style>
