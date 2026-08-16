<template>
  <div class="chat-view">
    <!-- 顶部工具条：会话信息 + 清空 -->
    <div class="chat-toolbar">
      <span class="session-label">会话：{{ sessionId }}</span>
      <div class="toolbar-right">
        <select v-model="selectedDatasource" class="datasource-select" @change="changeDatasource" :disabled="isLoading">
          <option value="">演示数据源</option>
          <option v-for="source in datasources" :key="source.id" :value="String(source.id)">{{ source.name }}</option>
        </select>
        <button class="btn btn-secondary small" @click="clearSession" :disabled="isLoading">清空会话</button>
      </div>
    </div>

    <!-- 对话历史 -->
    <div class="chat-messages" ref="messagesContainer">
      <div v-if="messages.length === 0" class="welcome-message">
        <div class="welcome-mark">
          <svg width="26" height="26" viewBox="0 0 16 16" fill="none">
            <path d="M2 13V8M6 13V3M10 13V6M14 13V9" stroke="currentColor"
                  stroke-width="2.2" stroke-linecap="round"/>
          </svg>
        </div>
        <h2>你好，我是数据分析 Agent</h2>
        <p>基于你的数据库做 Text-to-SQL 查询、知识库检索与数据分析，可以直接用业务语言提问。</p>
        <div class="suggestion-chips">
          <button
            v-for="s in suggestions"
            :key="s"
            class="suggestion-chip"
            @click="inputText = s"
          >{{ s }}</button>
        </div>
      </div>

      <div
        v-for="(msg, index) in messages"
        :key="index"
        :class="['message', msg.role]"
      >
        <div class="message-avatar" :class="msg.role">
          <svg v-if="msg.role === 'user'" viewBox="0 0 20 20" fill="none" width="16" height="16">
            <circle cx="10" cy="6.5" r="3.2" stroke="currentColor" stroke-width="1.6"/>
            <path d="M3.5 17c.8-3.2 3.4-5 6.5-5s5.7 1.8 6.5 5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
          </svg>
          <svg v-else viewBox="0 0 16 16" fill="none" width="15" height="15">
            <path d="M2 13V8M6 13V3M10 13V6M14 13V9" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>
          </svg>
        </div>
        <div class="message-content">
          <div class="markdown-content" v-html="renderMarkdown(msg.content)"></div>
          <!-- 可解释性面板：本轮工具调用与检索命中（默认折叠；摘要按 hit_key 去重计数） -->
          <details v-if="msg.contextHits" class="context-hits">
            <summary>
              调用 {{ msg.contextHits.tool_calls.length }} 个工具
              <template v-if="contextSummary(msg.contextHits)">，命中 {{ contextSummary(msg.contextHits) }}</template>
              <span v-if="msg.contextHits.truncated" class="hits-truncated">（已截断）</span>
            </summary>
            <div v-for="call in msg.contextHits.tool_calls" :key="call.call_id" class="hit-call">
              <div class="hit-call-head">
                <span class="badge" :class="call.status === 'success' ? 'badge-success' : 'badge-warning'">
                  {{ call.status }}
                </span>
                <code class="hit-tool-name">{{ call.name }}</code>
                <span v-if="call.duration_ms != null" class="hit-duration">{{ call.duration_ms }}ms</span>
                <span v-if="call.error_code" class="hit-error">{{ call.public_message }}</span>
                <code v-if="call.args" class="hit-args" :title="call.args">{{ call.args }}</code>
              </div>
              <div v-if="call.hits.terms.length || call.hits.examples.length || call.hits.docs.length || call.hits.graph.length" class="hit-lists">
                <div v-for="t in call.hits.terms" :key="`t-${t.hit_key}`" class="hit-row">
                  <span class="hit-kind">术语</span>{{ t.term }}<span class="hit-sub"> —— {{ t.definition }}</span>
                </div>
                <div v-for="e in call.hits.examples" :key="`e-${e.hit_key}`" class="hit-row">
                  <span class="hit-kind">示例</span>{{ e.question }}
                  <pre class="hit-sql">{{ e.sql }}</pre>
                </div>
                <div v-for="d in call.hits.docs" :key="`d-${d.hit_key}`" class="hit-row">
                  <span class="hit-kind">文档</span>{{ d.source }}<span class="hit-sub"> —— {{ d.snippet }}</span>
                </div>
                <div v-for="g in call.hits.graph" :key="`g-${g.hit_key}`" class="hit-row">
                  <span class="hit-kind">图谱</span>{{ g.query }}<span class="hit-sub"> —— {{ g.summary }}（{{ g.result_count }}）</span>
                </div>
              </div>
            </div>
          </details>
          <div v-if="msg.sources && msg.sources.length > 0" class="message-sources">
            <span class="sources-label">参考来源</span>
            <span v-for="(source, i) in msg.sources" :key="i" class="source-tag">
              {{ source }}
            </span>
          </div>
          <!-- 本轮执行过 SQL：一键沉淀为已验证示例（知识回流，人工确认后才入库） -->
          <div v-if="msg.sqlResult && !msg.sqlResult.saved" class="sql-sink">
            <span class="sql-sink-label">本轮执行了 SQL（{{ msg.sqlResult.row_count }} 行）</span>
            <button class="btn btn-secondary small" @click="saveExample(msg.sqlResult)" :disabled="msg.sqlResult.saving">
              <span v-if="msg.sqlResult.saving" class="loading-spinner small"></span>
              <template v-else>沉淀为示例</template>
            </button>
          </div>
          <div v-else-if="msg.sqlResult && msg.sqlResult.saved" class="sql-sink saved">
            ✓ 已沉淀为 SQL 示例
          </div>
        </div>
      </div>

      <div v-if="isLoading" class="message assistant loading">
        <div class="message-avatar assistant">
          <svg viewBox="0 0 16 16" fill="none" width="15" height="15">
            <path d="M2 13V8M6 13V3M10 13V6M14 13V9" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>
          </svg>
        </div>
        <div class="message-content">
          <div class="typing-indicator">
            <span></span><span></span><span></span>
          </div>
        </div>
      </div>
    </div>

    <!-- 输入区域 -->
    <div class="chat-input-area">
      <div class="input-wrapper">
        <textarea
          v-model="inputText"
          placeholder="输入你的问题..."
          class="input"
          rows="1"
          @keydown.enter.exact.prevent="sendMessage"
          :disabled="isLoading"
        ></textarea>
        <div class="input-actions">
          <select v-model="chatMode" class="mode-select">
            <option value="stream">流式模式</option>
            <option value="quick">快速模式</option>
          </select>
          <button
            class="btn btn-primary"
            @click="sendMessage"
            :disabled="!inputText.trim() || isLoading"
          >
            <span v-if="isLoading" class="loading-spinner small"></span>
            <template v-else>
              <svg viewBox="0 0 20 20" fill="none" width="14" height="14">
                <path d="M3 10l14-6-4.5 13-2.8-5.2L3 10Z" stroke="currentColor"
                      stroke-width="1.6" stroke-linejoin="round"/>
              </svg>
              发送
            </template>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, nextTick, onMounted } from 'vue'
import { marked } from 'marked'
import hljs from 'highlight.js'

// 配置 marked：代码块走 highlight.js
marked.setOptions({
  highlight: (code, lang) => {
    if (lang && hljs.getLanguage(lang)) {
      return hljs.highlight(code, { language: lang }).value
    }
    return hljs.highlightAuto(code).value
  }
})

const demoSuggestions = [
  '各州（customer_state）的销售额 Top5',
  '2018 年各月的 GMV 趋势',
  '复购率是多少？',
  '各支付方式的订单量占比',
]

const datasourceSuggestions = [
  '这个数据源包含哪些主要业务表？',
  '概括当前数据源可以分析的业务主题',
  '选择一个核心指标并分析其分布情况',
  '找出值得进一步分析的数据异常或趋势',
]

const messages = ref([])
const inputText = ref('')
const chatMode = ref('stream')
const isLoading = ref(false)
const messagesContainer = ref(null)
const datasources = ref([])
const selectedDatasource = ref(localStorage.getItem('data-agent:selected-datasource') || '')
const suggestions = computed(() => selectedDatasource.value ? datasourceSuggestions : demoSuggestions)

// 单页会话 ID，对应后端 ChatRequest.Id
const sessionId = 'session-' + Date.now()

const renderMarkdown = (text) => marked.parse(text || '')

const requestBody = (question) => ({
  Id: sessionId,
  Question: question,
  datasource_id: selectedDatasource.value ? Number(selectedDatasource.value) : null,
})

const scrollToBottom = () => {
  nextTick(() => {
    if (messagesContainer.value) {
      messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
    }
  })
}

// 解析后端 SSE：每条形如 `data: {"type": "...", "data": "..."}`。
// 后端定义了四种 type：content（增量文本）/ sources（来源）/ done（结束）/ error（异常）。
// 逐块读取时需要跨 read 缓冲不完整的行，避免把 JSON 截断。
const streamChat = async (question, assistantMessage) => {
  const response = await fetch('/api/chat_stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(requestBody(question))
  })

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    // 按 SSE 事件分隔符切分，保留最后一段（可能不完整）继续缓冲
    const parts = buffer.split('\n\n')
    buffer = parts.pop()

    for (const part of parts) {
      for (const line of part.split('\n')) {
        if (!line.startsWith('data: ')) continue
        let payload
        try {
          payload = JSON.parse(line.slice(6))
        } catch (e) {
          continue
        }
        if (payload.type === 'content') {
          assistantMessage.content += payload.data
          scrollToBottom()
        } else if (payload.type === 'sources') {
          assistantMessage.sources = payload.data || []
        } else if (payload.type === 'sql_result') {
          assistantMessage.sqlResult = payload.data
        } else if (payload.type === 'context_hits') {
          assistantMessage.contextHits = payload.data
        } else if (payload.type === 'error') {
          assistantMessage.content += `\n\n❌ ${payload.data}`
        }
        // type === 'done' 无需处理，循环随流结束自然退出
      }
    }
  }
}

const quickChat = async (question, assistantMessage) => {
  const response = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(requestBody(question))
  })
  const data = await response.json()
  assistantMessage.content = data.answer || ''
  assistantMessage.sources = data.sources || []
  if (data.sql_result) assistantMessage.sqlResult = data.sql_result
  if (data.context_hits) assistantMessage.contextHits = data.context_hits
}

// 摘要行：按 hit_key 去重的计数（后端 summary 字段），数量 0 的分组不进摘要
const contextSummary = (hits) => {
  const parts = []
  const labels = { examples: '示例', terms: '术语', docs: '文档', graph: '图谱查询' }
  for (const [kind, label] of Object.entries(labels)) {
    const n = hits.summary?.[kind] || 0
    if (n > 0) parts.push(`${n} ${label}`)
  }
  return parts.join(' / ')
}

// 一键沉淀：人工确认（verified=true）后经示例库接口入库，同作用域同问题覆盖
const saveExample = async (sqlResult) => {
  sqlResult.saving = true
  try {
    const response = await fetch('/api/sql-examples', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question: sqlResult.question,
        sql: sqlResult.sql,
        verified: true,
        datasource_id: sqlResult.datasource_id,
        source: 'chat',
      })
    })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    sqlResult.saved = true
  } catch (error) {
    alert(`沉淀失败: ${error.message}`)
  } finally {
    sqlResult.saving = false
  }
}

const sendMessage = async () => {
  const text = inputText.value.trim()
  if (!text || isLoading.value) return

  messages.value.push({ role: 'user', content: text })
  inputText.value = ''
  isLoading.value = true
  scrollToBottom()

  const assistantMessage = { role: 'assistant', content: '', sources: [] }
  messages.value.push(assistantMessage)

  try {
    if (chatMode.value === 'stream') {
      await streamChat(text, assistantMessage)
    } else {
      await quickChat(text, assistantMessage)
    }
  } catch (error) {
    assistantMessage.content += `\n\n❌ 请求失败: ${error.message}`
  } finally {
    isLoading.value = false
    scrollToBottom()
  }
}

const clearSession = async () => {
  try {
    await fetch(`/api/chat/clear/${sessionId}`, { method: 'DELETE' })
  } catch (e) {
    // 清空失败不阻塞前端重置
  }
  messages.value = []
}

const changeDatasource = async () => {
  if (selectedDatasource.value) localStorage.setItem('data-agent:selected-datasource', selectedDatasource.value)
  else localStorage.removeItem('data-agent:selected-datasource')
  await clearSession()
}

onMounted(async () => {
  try {
    const response = await fetch('/api/datasources')
    if (!response.ok) return
    datasources.value = await response.json()
    if (selectedDatasource.value && !datasources.value.some((source) => String(source.id) === selectedDatasource.value)) {
      selectedDatasource.value = ''
      localStorage.removeItem('data-agent:selected-datasource')
    }
  } catch (e) { /* 数据源列表失败时仍可使用演示库 */ }
})
</script>

<style scoped>
.chat-view { display: flex; flex-direction: column; height: 100%; margin: -24px -28px; }

/* 顶栏 */
.chat-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 24px;
  border-bottom: 1px solid var(--border-color);
  background: color-mix(in srgb, var(--bg-surface) 70%, transparent);
  backdrop-filter: blur(8px);
}
.toolbar-right { display: flex; align-items: center; gap: 8px; }
.datasource-select { max-width: 220px; padding: 5px 9px; font-size: 12px; }

/* 消息区 */
.chat-messages {
  flex: 1; overflow-y: auto;
  padding: 28px 24px 12px;
  display: flex; flex-direction: column; gap: 20px;
}

.message { display: flex; gap: 12px; max-width: 780px; width: 100%; margin: 0 auto; }
.message.user { flex-direction: row-reverse; }

.message-avatar {
  width: 30px; height: 30px; border-radius: 9px; flex-shrink: 0;
  display: grid; place-items: center; margin-top: 2px;
}
.message-avatar.assistant {
  background: linear-gradient(135deg, #2dd4bf, #0ea5e9);
  color: #03201b;
  box-shadow: 0 4px 10px -4px rgba(45,212,191,.45);
}
.message-avatar.user {
  background: var(--bg-raised);
  border: 1px solid var(--border-strong);
  color: var(--text-secondary);
}

.message-content {
  max-width: calc(100% - 90px);
  padding: 10px 14px;
  border-radius: 14px;
  font-size: 14px;
}
.message.assistant .message-content {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-top-left-radius: 4px;
}
.message.user .message-content {
  background: linear-gradient(180deg, rgba(45,212,191,.16), rgba(20,184,166,.1));
  border: 1px solid rgba(45,212,191,.25);
  border-top-right-radius: 4px;
}

.message-sources {
  margin-top: 8px; padding-top: 8px;
  border-top: 1px dashed var(--border-color);
}
.sources-label { font-size: 11px; color: var(--text-muted); margin-right: 6px; }

/* 可解释性面板（默认折叠） */
.context-hits {
  margin-top: 8px; padding-top: 8px;
  border-top: 1px dashed var(--border-color);
  font-size: 12px;
}
.context-hits summary {
  cursor: pointer; color: var(--text-secondary); user-select: none;
}
.context-hits[open] summary { margin-bottom: 8px; }
.hits-truncated { color: var(--text-muted); }
.hit-call { display: flex; flex-direction: column; gap: 6px; padding: 6px 0; }
.hit-call + .hit-call { border-top: 1px dashed var(--border-color); }
.hit-call-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.hit-tool-name { font-family: var(--font-mono); font-size: 11.5px; color: var(--accent); }
.hit-duration { color: var(--text-muted); font-family: var(--font-mono); font-size: 11px; }
.hit-error { color: var(--error-color); }
.hit-args {
  font-family: var(--font-mono); font-size: 10.5px; color: var(--text-muted);
  max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 120px;
}
.hit-lists { display: flex; flex-direction: column; gap: 4px; padding-left: 4px; }
.hit-row { color: var(--text-primary); line-height: 1.5; }
.hit-kind {
  display: inline-block; margin-right: 6px; padding: 0 5px;
  background: var(--bg-raised); border: 1px solid var(--border-strong);
  border-radius: 4px; font-size: 10px; color: var(--text-muted);
}
.hit-sub { color: var(--text-secondary); }
.hit-sql {
  margin: 2px 0 0; padding: 4px 8px;
  background: var(--bg-inset); border: 1px solid var(--border-color); border-radius: 4px;
  font-family: var(--font-mono); font-size: 11px; color: #a5f3e8; overflow-x: auto;
}

/* SQL 沉淀条 */
.sql-sink {
  margin-top: 8px; padding-top: 8px;
  border-top: 1px dashed var(--border-color);
  display: flex; align-items: center; gap: 10px;
}
.sql-sink-label { font-size: 12px; color: var(--text-secondary); }
.sql-sink.saved { font-size: 12px; color: var(--accent); }

/* 欢迎态 */
.welcome-message {
  margin: auto; max-width: 560px; text-align: center; padding: 40px 20px;
}
.welcome-mark {
  width: 52px; height: 52px; border-radius: 15px; margin: 0 auto 18px;
  display: grid; place-items: center; color: #03201b;
  background: linear-gradient(135deg, #2dd4bf, #0ea5e9);
  box-shadow: 0 12px 32px -10px rgba(45,212,191,.55);
}
.welcome-message h2 { font-size: 19px; font-weight: 700; margin-bottom: 8px; }
.welcome-message p { font-size: 13.5px; color: var(--text-secondary); margin-bottom: 22px; }

.suggestion-chips { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }
.suggestion-chip {
  padding: 7px 14px; border-radius: 999px;
  background: var(--bg-surface); border: 1px solid var(--border-strong);
  color: var(--text-secondary); font-size: 12.5px; font-family: inherit;
  cursor: pointer; transition: all .15s ease;
}
.suggestion-chip:hover {
  border-color: var(--accent-strong); color: var(--accent);
  background: var(--accent-soft); transform: translateY(-1px);
}

/* 输入区 */
.chat-input-area { padding: 12px 24px 20px; }
.input-wrapper {
  max-width: 780px; margin: 0 auto;
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  border-radius: 16px;
  padding: 10px 12px;
  transition: border-color .15s ease, box-shadow .15s ease;
}
.input-wrapper:focus-within {
  border-color: var(--accent-strong);
  box-shadow: 0 0 0 3px var(--accent-ring);
}
.input-wrapper .input {
  width: 100%; border: none; background: transparent;
  box-shadow: none; padding: 4px 6px; font-size: 14px;
  max-height: 140px;
}
.input-wrapper .input:focus { border: none; box-shadow: none; }
.input-actions {
  display: flex; align-items: center; justify-content: space-between;
  margin-top: 8px;
}
.mode-select { padding: 4px 8px; font-size: 12px; border-radius: 7px; }
</style>
