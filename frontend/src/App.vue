<template>
  <div class="app-container">
    <!-- 侧边栏 -->
    <aside class="sidebar">
      <div class="sidebar-header">
        <div class="brand">
          <div class="brand-mark">
            <!-- 波形 + 数据条：数据分析的抽象标识 -->
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M2 13V8M6 13V3M10 13V6M14 13V9" stroke="currentColor"
                    stroke-width="2.2" stroke-linecap="round"/>
            </svg>
          </div>
          <div>
            <div class="brand-name">Data Agent</div>
            <div class="brand-sub">智能数据分析平台</div>
          </div>
        </div>
      </div>

      <nav class="sidebar-nav">
        <template v-for="group in navGroups" :key="group.label">
          <div class="nav-group-label">{{ group.label }}</div>
          <button
            v-for="item in group.items"
            :key="item.id"
            :class="['nav-item', { active: currentView === item.id }]"
            @click="currentView = item.id"
          >
            <span class="nav-icon" v-html="icons[item.id]"></span>
            <span class="nav-text">{{ item.name }}</span>
          </button>
        </template>
      </nav>

      <div class="sidebar-footer">
        <div class="status-indicator" :class="connectionStatus">
          <span class="status-dot"></span>
          <span>{{ connectionStatus === 'connected' ? '服务已连接' : '服务未连接' }}</span>
        </div>
      </div>
    </aside>

    <!-- 主内容区 -->
    <main class="main-content">
      <div v-if="currentView === 'chat'" class="view-container">
        <ChatView />
      </div>
      <div v-if="currentView === 'tasks'" class="view-container">
        <TasksView />
      </div>
      <div v-if="currentView === 'skills'" class="view-container">
        <SkillsView />
      </div>
      <div v-if="currentView === 'mcp'" class="view-container">
        <McpView />
      </div>
      <div v-if="currentView === 'upload'" class="view-container">
        <UploadView />
      </div>
      <div v-if="currentView === 'graph'" class="view-container">
        <GraphView />
      </div>
      <div v-if="currentView === 'knowledge'" class="view-container">
        <KnowledgeView />
      </div>
      <div v-if="currentView === 'status'" class="view-container">
        <StatusView />
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import ChatView from './views/ChatView.vue'
import TasksView from './views/TasksView.vue'
import SkillsView from './views/SkillsView.vue'
import McpView from './views/McpView.vue'
import UploadView from './views/UploadView.vue'
import GraphView from './views/GraphView.vue'
import KnowledgeView from './views/KnowledgeView.vue'
import StatusView from './views/StatusView.vue'

const currentView = ref('chat')
const connectionStatus = ref('disconnected')

const navGroups = [
  {
    label: '工作台',
    items: [
      { id: 'chat', name: '智能对话' },
      { id: 'tasks', name: '任务中心' },
      { id: 'upload', name: '知识库' },
    ],
  },
  {
    label: '平台管理',
    items: [
      { id: 'graph', name: '知识图谱' },
      { id: 'knowledge', name: '知识管理' },
      { id: 'skills', name: 'Skills 技能' },
      { id: 'mcp', name: 'MCP 工具' },
      { id: 'status', name: '系统状态' },
    ],
  },
]

// 线性风格 SVG 图标（stroke=currentColor，随主题变色）
const icons = {
  chat: `<svg viewBox="0 0 20 20" fill="none"><path d="M17 9.5a6.5 6.5 0 0 1-9.3 5.9L3 17l1.6-4.2A6.5 6.5 0 1 1 17 9.5Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M7 8.5h6M7 11h3.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>`,
  tasks: `<svg viewBox="0 0 20 20" fill="none"><path d="M3.5 5.6l1.5 1.5L7.8 4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M3.5 11.4l1.5 1.5L7.8 9.8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M11 6h5.5M11 12h5.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>`,
  upload: `<svg viewBox="0 0 20 20" fill="none"><ellipse cx="10" cy="4.5" rx="6.5" ry="2.5" stroke="currentColor" stroke-width="1.6"/><path d="M3.5 4.5v11c0 1.4 2.9 2.5 6.5 2.5s6.5-1.1 6.5-2.5v-11" stroke="currentColor" stroke-width="1.6"/><path d="M3.5 10c0 1.4 2.9 2.5 6.5 2.5s6.5-1.1 6.5-2.5" stroke="currentColor" stroke-width="1.6"/></svg>`,
  graph: `<svg viewBox="0 0 20 20" fill="none"><circle cx="5" cy="6" r="2.1" stroke="currentColor" stroke-width="1.6"/><circle cx="15" cy="5" r="2.1" stroke="currentColor" stroke-width="1.6"/><circle cx="12" cy="15" r="2.1" stroke="currentColor" stroke-width="1.6"/><path d="M7 6.4l5.8-.9M6.6 7.7l4.6 5.5M13.7 7l-1.4 6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>`,
  knowledge: `<svg viewBox="0 0 20 20" fill="none"><path d="M10 5.6C8.5 4.4 6.4 4.1 4 4.4v10c2.4-.3 4.5 0 6 1.2 1.5-1.2 3.6-1.5 6-1.2v-10c-2.4-.3-4.5 0-6 1.2Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M10 5.6v10" stroke="currentColor" stroke-width="1.6"/></svg>`,
  skills: `<svg viewBox="0 0 20 20" fill="none"><path d="M10 2.5l1.8 3.6 4 .6-2.9 2.8.7 4-3.6-1.9-3.6 1.9.7-4L4.2 6.7l4-.6L10 2.5Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M16.5 14.5l.6 1.2 1.4.2-1 1 .2 1.3-1.2-.6-1.2.6.2-1.3-1-1 1.4-.2.6-1.2Z" fill="currentColor"/></svg>`,
  mcp: `<svg viewBox="0 0 20 20" fill="none"><path d="M7 3v4M13 3v4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><path d="M5 7h10v3a5 5 0 0 1-4 4.9V17h-2v-2.1A5 5 0 0 1 5 10V7Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>`,
  status: `<svg viewBox="0 0 20 20" fill="none"><path d="M2.5 10h3.2l2-5 3.6 10 2.2-5h4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
}

onMounted(async () => {
  try {
    const res = await fetch('/health')
    if (res.ok) {
      connectionStatus.value = 'connected'
    }
  } catch (e) {
    connectionStatus.value = 'disconnected'
  }
})
</script>
