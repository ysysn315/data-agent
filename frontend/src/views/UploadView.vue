<template>
  <div class="upload-view">
    <!-- 上传区域 -->
    <div class="upload-section card">
      <h3 class="card-title">知识库</h3>
      <p class="card-desc">上传运维文档、故障排查手册等，AI 将自动学习并在对话中引用。</p>
      
      <div 
        class="upload-area"
        :class="{ dragging: isDragging }"
        @dragover.prevent="isDragging = true"
        @dragleave="isDragging = false"
        @drop.prevent="handleDrop"
        @click="triggerFileInput"
      >
        <input
          type="file"
          ref="fileInput"
          accept=".txt,.md,.pdf,.docx,.html,.htm,.csv,.json,.xlsx,.xls"
          @change="handleFileSelect"
          hidden
        >
        <div class="upload-icon"><svg viewBox="0 0 20 20" fill="none" width="28" height="28"><path d="M10 13V4m0 0L6.5 7.5M10 4l3.5 3.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M3.5 13v2.5a1.5 1.5 0 0 0 1.5 1.5h10a1.5 1.5 0 0 0 1.5-1.5V13" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></div>
        <p class="upload-text">拖拽文件到此处，或点击选择文件</p>
        <p class="upload-hint">支持 .txt, .md, .pdf, .docx, .html, .csv, .json, .xlsx, .xls 格式</p>
      </div>
      
      <div v-if="uploading" class="upload-progress">
        <div class="loading-spinner"></div>
        <span>正在上传并建立索引...</span>
      </div>
    </div>

    <!-- 上传结果 -->
    <div v-if="uploadResult" class="upload-result card">
      <h3 class="card-title">📤 上传结果</h3>
      <div :class="['result-content', uploadResult.success ? 'success' : 'error']">
        <span class="result-icon">{{ uploadResult.success ? '✅' : '❌' }}</span>
        <div class="result-info">
          <p class="result-filename">{{ uploadResult.filename }}</p>
          <p class="result-message">{{ uploadResult.message }}</p>
          <p v-if="uploadResult.chunks" class="result-detail">
            已分割为 {{ uploadResult.chunks }} 个文档块
          </p>
        </div>
      </div>
    </div>

    <!-- 文档列表 -->
    <div class="documents-section card">
      <div class="card-header">
        <h3 class="card-title">已上传文档</h3>
        <button class="btn btn-secondary" @click="refreshDocuments">刷新</button>
      </div>
      
      <div v-if="loadingDocs" class="loading-state">
        <div class="loading-spinner"></div>
      </div>
      
      <div v-else-if="documents.length === 0" class="empty-state">
        <p>暂无文档，请上传运维知识文档</p>
      </div>
      
      <div v-else class="document-list">
        <div v-for="doc in documents" :key="doc.id" class="document-item">
          <span class="doc-icon"><svg viewBox="0 0 20 20" fill="none" width="18" height="18"><path d="M5 2.5h6.5L16 7v10.5H5V2.5Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M11.5 2.5V7H16" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg></span>
          <div class="doc-info">
            <p class="doc-name">{{ doc.name }}</p>
            <p class="doc-meta">{{ doc.chunks || 0 }} 个文档块</p>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'

const fileInput = ref(null)
const isDragging = ref(false)
const uploading = ref(false)
const uploadResult = ref(null)
const documents = ref([])
const loadingDocs = ref(false)

const triggerFileInput = () => {
  fileInput.value?.click()
}

const handleDrop = (e) => {
  isDragging.value = false
  const files = e.dataTransfer.files
  if (files.length > 0) {
    uploadFile(files[0])
  }
}

const handleFileSelect = (e) => {
  const files = e.target.files
  if (files.length > 0) {
    uploadFile(files[0])
  }
}

const uploadFile = async (file) => {
  // 检查文件格式
  const ext = file.name.split('.').pop().toLowerCase()
  const allowedExts = ['txt', 'md', 'pdf', 'docx', 'html', 'htm', 'csv', 'json', 'xlsx', 'xls']
  if (!allowedExts.includes(ext)) {
    uploadResult.value = {
      success: false,
      filename: file.name,
      message: '不支持的文件格式，支持 .txt, .md, .pdf, .docx, .html, .csv, .json, .xlsx, .xls'
    }
    return
  }

  uploading.value = true
  uploadResult.value = null

  try {
    const formData = new FormData()
    formData.append('file', file)

    const response = await fetch('/api/upload', {
      method: 'POST',
      body: formData
    })

    const data = await response.json()

    uploadResult.value = {
      success: response.ok,
      filename: file.name,
      message: data.message || (response.ok ? '上传成功' : '上传失败'),
      chunks: data.chunks
    }

    if (response.ok) {
      refreshDocuments()
    }
  } catch (error) {
    uploadResult.value = {
      success: false,
      filename: file.name,
      message: `上传失败: ${error.message}`
    }
  } finally {
    uploading.value = false
  }
}

const refreshDocuments = async () => {
  // 这里可以添加获取文档列表的 API
  // 目前先显示模拟数据
  documents.value = []
}

onMounted(() => {
  refreshDocuments()
})
</script>

<style scoped>
.upload-view {
  display: flex;
  flex-direction: column;
  gap: 20px;
  padding: 20px;
  height: 100%;
  overflow-y: auto;
}

.card-desc {
  color: var(--text-secondary);
  margin-bottom: 16px;
}

.upload-area {
  border: 2px dashed var(--border-color);
  border-radius: 12px;
  padding: 40px;
  text-align: center;
  cursor: pointer;
  transition: all 0.3s;
}

.upload-area:hover,
.upload-area.dragging {
  border-color: var(--primary-color);
  background-color: rgba(79, 70, 229, 0.05);
}

.upload-icon {
  font-size: 3rem;
  margin-bottom: 12px;
}

.upload-text {
  color: var(--text-primary);
  margin-bottom: 8px;
}

.upload-hint {
  color: var(--text-muted);
  font-size: 0.85rem;
}

.upload-progress {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 20px;
  color: var(--text-secondary);
}

.result-content {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 16px;
  border-radius: 8px;
}

.result-content.success {
  background-color: rgba(16, 185, 129, 0.1);
}

.result-content.error {
  background-color: rgba(239, 68, 68, 0.1);
}

.result-icon {
  font-size: 1.5rem;
}

.result-info {
  flex: 1;
}

.result-filename {
  font-weight: 600;
  margin-bottom: 4px;
}

.result-message {
  color: var(--text-secondary);
}

.result-detail {
  color: var(--text-muted);
  font-size: 0.85rem;
  margin-top: 4px;
}

.loading-state {
  display: flex;
  justify-content: center;
  padding: 40px;
}

.empty-state {
  text-align: center;
  padding: 40px;
  color: var(--text-muted);
}

.document-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.document-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px;
  background-color: var(--bg-tertiary);
  border-radius: 8px;
}

.doc-icon {
  font-size: 1.5rem;
}

.doc-name {
  font-weight: 500;
}

.doc-meta {
  color: var(--text-muted);
  font-size: 0.85rem;
}
</style>