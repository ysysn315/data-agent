<template>
  <div class="mcp-view">
    <!-- 顶部：标题 + 刷新 -->
    <div class="page-header card">
      <div class="page-header-left">
        <h3 class="card-title">🔌 MCP Server 管理</h3>
        <p class="page-desc">MCP server 以标准协议接入外部工具。stdio 传输等价于在服务器上执行命令，务必只连可信来源。</p>
      </div>
      <button class="btn btn-secondary" @click="fetchServers" :disabled="loading">刷新</button>
    </div>

    <!-- 加载 / 空态 -->
    <div v-if="loading" class="loading-state card">
      <div class="loading-spinner"></div>
    </div>
    <div v-else-if="servers.length === 0" class="empty-state card">
      <p>还没有注册任何 MCP server。</p>
    </div>

    <!-- server 列表 -->
    <div v-else class="server-list">
      <div v-for="server in servers" :key="server.slug" class="server-card card">
        <div class="server-head">
          <span class="server-icon">🔌</span>
          <div class="server-name-area">
            <div class="server-name">{{ server.name || server.slug }}</div>
            <div class="server-slug">{{ server.slug }}</div>
          </div>
          <span class="badge badge-neutral transport-badge">{{ server.transport }}</span>
          <span class="badge" :class="server.enabled ? 'badge-success' : 'badge-error'">
            {{ server.enabled ? '已启用' : '已禁用' }}
          </span>
        </div>

        <p v-if="server.description" class="server-desc">{{ server.description }}</p>

        <!-- 连接目标：http 类展示 url，stdio 展示命令 -->
        <div class="server-target">
          <template v-if="server.transport === 'stdio'">
            <span class="target-label">命令</span>
            <code class="target-value">{{ server.command }} {{ (server.args || []).join(' ') }}</code>
          </template>
          <template v-else>
            <span class="target-label">地址</span>
            <code class="target-value">{{ server.url }}</code>
          </template>
        </div>

        <div class="server-actions">
          <button
            class="btn btn-secondary small"
            :disabled="testingSlug === server.slug"
            @click="testServer(server)"
          >
            <span v-if="testingSlug === server.slug" class="loading-spinner small"></span>
            <span v-else>测试连接</span>
          </button>
          <button
            class="btn small"
            :class="server.enabled ? 'btn-danger' : 'btn-primary'"
            :disabled="togglingSlug === server.slug"
            @click="toggleEnabled(server)"
          >
            <span v-if="togglingSlug === server.slug" class="loading-spinner small"></span>
            <span v-else>{{ server.enabled ? '禁用' : '启用' }}</span>
          </button>
        </div>

        <!-- 测试结果：工具列表或错误 -->
        <div v-if="testResults[server.slug]" class="test-result" :class="{ error: testResults[server.slug].error }">
          <template v-if="testResults[server.slug].error">
            <span class="result-icon">❌</span>
            <span>{{ testResults[server.slug].error }}</span>
          </template>
          <template v-else>
            <div class="result-summary">
              ✅ 连接成功，发现 {{ testResults[server.slug].tool_count }} 个工具
            </div>
            <div class="tool-list">
              <div v-for="tool in testResults[server.slug].tools" :key="tool.name" class="tool-item">
                <span class="tool-name">{{ tool.name }}</span>
                <span v-if="tool.description" class="tool-desc">{{ tool.description }}</span>
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'

const servers = ref([])
const loading = ref(false)
const testingSlug = ref('')
const togglingSlug = ref('')
const testResults = reactive({})

const fetchServers = async () => {
  loading.value = true
  try {
    const res = await fetch('/api/mcp/servers')
    servers.value = res.ok ? await res.json() : []
  } catch (e) {
    servers.value = []
  } finally {
    loading.value = false
  }
}

const toggleEnabled = async (server) => {
  if (togglingSlug.value) return
  togglingSlug.value = server.slug
  const action = server.enabled ? 'disable' : 'enable'
  try {
    const res = await fetch(`/api/mcp/servers/${server.slug}/${action}`, { method: 'POST' })
    if (res.ok) {
      const updated = await res.json()
      const idx = servers.value.findIndex((s) => s.slug === server.slug)
      if (idx > -1) servers.value[idx] = updated
    } else {
      alert(`操作失败：${res.status}`)
    }
  } catch (e) {
    alert('操作失败：' + e.message)
  } finally {
    togglingSlug.value = ''
  }
}

const testServer = async (server) => {
  if (testingSlug.value) return
  testingSlug.value = server.slug
  delete testResults[server.slug]
  try {
    const res = await fetch(`/api/mcp/servers/${server.slug}/test`, { method: 'POST' })
    const data = await res.json()
    if (res.ok) {
      testResults[server.slug] = data
    } else {
      // 后端连接失败返回 502，错误信息在 detail 字段
      testResults[server.slug] = { error: data.detail || `连接失败：${res.status}` }
    }
  } catch (e) {
    testResults[server.slug] = { error: '连接失败：' + e.message }
  } finally {
    testingSlug.value = ''
  }
}

fetchServers()
</script>

<style scoped>
.mcp-view {
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

.server-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.server-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.server-icon {
  font-size: 1.4rem;
}

.server-name-area {
  flex: 1;
  min-width: 0;
}

.server-name {
  font-weight: 600;
  color: var(--text-primary);
}

.server-slug {
  font-size: 0.75rem;
  color: var(--text-muted);
  font-family: monospace;
}

.transport-badge {
  font-family: monospace;
}

.badge-neutral {
  background-color: var(--bg-tertiary);
  color: var(--text-secondary);
}

.server-desc {
  color: var(--text-secondary);
  font-size: 0.9rem;
  margin-bottom: 10px;
}

.server-target {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  font-size: 0.85rem;
}

.target-label {
  color: var(--text-muted);
  flex-shrink: 0;
}

.target-value {
  background-color: var(--bg-tertiary);
  padding: 4px 10px;
  border-radius: 6px;
  font-family: monospace;
  color: var(--text-primary);
  word-break: break-all;
}

.server-actions {
  display: flex;
  gap: 12px;
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

.test-result {
  margin-top: 14px;
  padding: 14px;
  border-radius: 8px;
  background-color: rgba(16, 185, 129, 0.1);
  font-size: 0.85rem;
}

.test-result.error {
  background-color: rgba(239, 68, 68, 0.1);
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--error-color);
}

.result-icon {
  font-size: 1.1rem;
}

.result-summary {
  color: var(--success-color);
  font-weight: 600;
  margin-bottom: 10px;
}

.tool-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.tool-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
  background-color: var(--bg-tertiary);
  border-radius: 6px;
}

.tool-name {
  font-family: monospace;
  color: var(--text-primary);
  font-weight: 600;
}

.tool-desc {
  color: var(--text-muted);
  font-size: 0.8rem;
}
</style>
