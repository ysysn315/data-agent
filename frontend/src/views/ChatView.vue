<template>
  <div class="chat-view">
    <!-- 顶部工具条：会话信息 + 清空 -->
    <div class="chat-toolbar">
      <span class="session-label">会话：{{ sessionId }}</span>
      <button class="btn btn-secondary small" @click="clearSession" :disabled="isLoading">
        清空会话
      </button>
    </div>

    <!-- 对话历史 -->
    <div class="chat-messages" ref="messagesContainer">
      <div v-if="messages.length === 0" class="welcome-message">
        <h2>👋 你好！我是智能数据分析 Agent</h2>
        <p>我可以基于你的数据库做 Text-to-SQL 查询、知识库检索与数据分析。</p>
      </div>

      <div
        v-for="(msg, index) in messages"
        :key="index"
        :class="['message', msg.role]"
      >
        <div class="message-avatar">
          {{ msg.role === 'user' ? '👤' : '🤖' }}
        </div>
        <div class="message-content">
          <div class="markdown-content" v-html="renderMarkdown(msg.content)"></div>
          <div v-if="msg.sources && msg.sources.length > 0" class="message-sources">
            <span class="sources-label">📚 参考来源:</span>
            <span v-for="(source, i) in msg.sources" :key="i" class="source-tag">
              {{ source }}
            </span>
          </div>
        </div>
      </div>

      <div v-if="isLoading" class="message assistant loading">
        <div class="message-avatar">🤖</div>
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
            <span v-else>发送</span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, nextTick } from 'vue'
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

const messages = ref([])
const inputText = ref('')
const chatMode = ref('stream')
const isLoading = ref(false)
const messagesContainer = ref(null)

// 单页会话 ID，对应后端 ChatRequest.Id
const sessionId = 'session-' + Date.now()

const renderMarkdown = (text) => marked.parse(text || '')

const scrollToBottom = () => {
  nextTick(() => {
    if (messagesContainer.value) {
      messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
    }
  })
}

// 解析后端 SSE：每条形如 `data: {"type": "...", "data": "..."}`。
// 后端定义了三种 type：content（增量文本）/ done（结束）/ error（异常）。
// 逐块读取时需要跨 read 缓冲不完整的行，避免把 JSON 截断。
const streamChat = async (question, assistantMessage) => {
  const response = await fetch('/api/chat_stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ Id: sessionId, Question: question })
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
    body: JSON.stringify({ Id: sessionId, Question: question })
  })
  const data = await response.json()
  assistantMessage.content = data.answer || ''
  assistantMessage.sources = data.sources || []
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
</script>

<style scoped>
.chat-view {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.chat-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 20px;
  border-bottom: 1px solid var(--border-color);
  background-color: var(--bg-secondary);
}

.session-label {
  font-size: 0.85rem;
  color: var(--text-muted);
  font-family: monospace;
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
}

.welcome-message {
  text-align: center;
  padding: 60px 20px;
  color: var(--text-secondary);
}

.welcome-message h2 {
  color: var(--text-primary);
  margin-bottom: 12px;
}

.message {
  display: flex;
  gap: 12px;
  margin-bottom: 20px;
  animation: fadeIn 0.3s ease;
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}

.message.user {
  flex-direction: row-reverse;
}

.message-avatar {
  width: 36px;
  height: 36px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 1.2rem;
  background-color: var(--bg-tertiary);
  flex-shrink: 0;
}

.message.user .message-avatar {
  background-color: var(--primary-color);
}

.message-content {
  max-width: 70%;
  padding: 12px 16px;
  border-radius: 12px;
  background-color: var(--bg-secondary);
  border: 1px solid var(--border-color);
}

.message.user .message-content {
  background-color: var(--primary-color);
  border-color: var(--primary-color);
}

.message-sources {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border-color);
  font-size: 0.85rem;
}

.sources-label {
  color: var(--text-muted);
  margin-right: 8px;
}

.source-tag {
  display: inline-block;
  background-color: var(--bg-tertiary);
  padding: 2px 8px;
  border-radius: 4px;
  margin-right: 4px;
  font-size: 0.8rem;
}

.typing-indicator {
  display: flex;
  gap: 4px;
}

.typing-indicator span {
  width: 8px;
  height: 8px;
  background-color: var(--text-muted);
  border-radius: 50%;
  animation: bounce 1.4s infinite ease-in-out;
}

.typing-indicator span:nth-child(1) { animation-delay: -0.32s; }
.typing-indicator span:nth-child(2) { animation-delay: -0.16s; }

@keyframes bounce {
  0%, 80%, 100% { transform: scale(0); }
  40% { transform: scale(1); }
}

.chat-input-area {
  padding: 16px 20px;
  border-top: 1px solid var(--border-color);
  background-color: var(--bg-secondary);
}

.input-wrapper {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.input-wrapper .input {
  resize: none;
  min-height: 44px;
  max-height: 120px;
}

.input-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 12px;
}

.mode-select {
  padding: 8px 12px;
  background-color: var(--bg-tertiary);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  color: var(--text-primary);
  font-size: 0.9rem;
}

.btn.small {
  padding: 6px 12px;
  font-size: 0.85rem;
}

.loading-spinner.small {
  width: 16px;
  height: 16px;
  border-width: 2px;
}
</style>
