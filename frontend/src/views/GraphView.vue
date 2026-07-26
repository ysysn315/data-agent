<template>
  <div class="graph-view">
    <!-- 顶部：标题 + stats 徽章 -->
    <div class="page-header card">
      <div class="page-header-left">
        <h3 class="card-title">知识图谱</h3>
        <p class="page-desc">业务实体与指标口径的关系图。搜索实体看一跳邻居子图，或查两实体间的谓词链路径。</p>
      </div>
      <div class="stats-badges">
        <span class="badge badge-accent">{{ stats.entity_count ?? '—' }} 实体</span>
        <span class="badge badge-neutral">{{ stats.triple_count ?? '—' }} 三元组</span>
        <button class="btn btn-secondary small" @click="loadStats">刷新</button>
      </div>
    </div>

    <!-- 实体搜索 -->
    <div class="card">
      <div class="card-header">
        <span class="card-title">实体子图</span>
      </div>
      <div class="search-row">
        <input
          v-model="entityQuery"
          class="input entity-input"
          placeholder="输入实体名，如 GMV / 订单 / 客单价"
          @keydown.enter="searchEntity()"
        />
        <button class="btn btn-primary" @click="searchEntity()" :disabled="entityLoading || !entityQuery.trim()">
          <span v-if="entityLoading" class="loading-spinner small"></span>
          <span v-else>查询</span>
        </button>
      </div>
      <div class="example-chips">
        <span class="chip-label">示例</span>
        <button v-for="ex in EXAMPLE_ENTITIES" :key="ex" class="mini-chip" @click="searchEntity(ex)">{{ ex }}</button>
      </div>

      <!-- 未命中：后端 detail + 本地近似实体提示 -->
      <div v-if="entityError" class="miss-block">
        <div class="inline-error">{{ entityError }}</div>
        <div v-if="entitySuggestions.length" class="suggest-row">
          <span class="chip-label">你是不是想查</span>
          <button v-for="s in entitySuggestions" :key="s" class="mini-chip" @click="searchEntity(s)">{{ s }}</button>
        </div>
        <div v-else class="suggest-empty">本地暂无近似实体（先查询一个已知实体即可积累本地索引）。</div>
      </div>

      <!-- 放射子图 SVG -->
      <div v-if="entityData && layout" class="graph-canvas">
        <svg :viewBox="`0 0 ${W} ${H}`" class="graph-svg" preserveAspectRatio="xMidYMid meet">
          <defs>
            <marker id="graph-arrow" viewBox="0 0 10 10" refX="8.5" refY="5"
                    markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0 0 L10 5 L0 10 z" class="arrow-head" />
            </marker>
            <linearGradient id="center-grad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="#2dd4bf" />
              <stop offset="120%" stop-color="#0ea5e9" />
            </linearGradient>
          </defs>

          <!-- 邻居之间的边（非中心入射）：细弱虚线，仅示结构 -->
          <line
            v-for="(l, i) in layout.interLinks" :key="'il' + i"
            :x1="l.x1" :y1="l.y1" :x2="l.x2" :y2="l.y2" class="inter-link"
          />

          <!-- 中心放射连线 + 谓词标签 -->
          <g v-for="node in layout.nodes" :key="'spoke' + node.name">
            <line
              :x1="node.lineStart.x" :y1="node.lineStart.y"
              :x2="node.lineEnd.x" :y2="node.lineEnd.y"
              class="spoke"
              :marker-end="node.hasOut ? 'url(#graph-arrow)' : null"
              :marker-start="node.hasIn ? 'url(#graph-arrow)' : null"
            />
            <text
              v-for="(rel, ri) in node.rels" :key="'p' + ri"
              :x="node.mid.x"
              :y="node.mid.y + (ri - (node.rels.length - 1) / 2) * 15"
              text-anchor="middle" dominant-baseline="middle"
              class="predicate-label" :class="rel.dir === 'out' ? 'predicate-out' : 'predicate-in'"
            >{{ rel.predicate }}</text>
          </g>

          <!-- 邻居节点（pill，可点击跳转） -->
          <g
            v-for="node in layout.nodes" :key="'node' + node.name"
            class="node-g" @click="searchEntity(node.name)"
          >
            <rect
              :x="node.x - node.w / 2" :y="node.y - 17" :width="node.w" height="34" rx="17"
              class="node-pill"
            />
            <text :x="node.x" :y="node.y" text-anchor="middle" dominant-baseline="central" class="node-label">{{ node.display }}</text>
            <title>{{ node.name }}（点击查看其子图）</title>
          </g>

          <!-- 中心实体（最后画，压在最上层） -->
          <g class="center-g">
            <rect
              :x="CX - layout.center.w / 2" :y="CY - 20" :width="layout.center.w" height="40" rx="20"
              class="center-pill"
            />
            <text :x="CX" :y="CY" text-anchor="middle" dominant-baseline="central" class="center-label">{{ layout.center.display }}</text>
            <title>{{ layout.center.name }}</title>
          </g>
        </svg>
        <div class="graph-legend">
          <span class="legend-item"><span class="legend-swatch out"></span>出边（中心 → 邻居）</span>
          <span class="legend-item"><span class="legend-swatch in"></span>入边（邻居 → 中心）</span>
          <span class="legend-hint">点击任一邻居节点可下钻其子图</span>
        </div>
      </div>
    </div>

    <!-- 路径查询 -->
    <div class="card">
      <div class="card-header">
        <span class="card-title">路径查询</span>
      </div>
      <div class="path-row">
        <input v-model="pathFrom" class="input" placeholder="起点，如 GMV" @keydown.enter="findPath" />
        <span class="path-arrow-glyph">→</span>
        <input v-model="pathTo" class="input" placeholder="终点，如 订单项" @keydown.enter="findPath" />
        <button class="btn btn-primary" @click="findPath" :disabled="pathLoading || !pathFrom.trim() || !pathTo.trim()">
          <span v-if="pathLoading" class="loading-spinner small"></span>
          <span v-else>查路径</span>
        </button>
      </div>
      <div class="example-chips">
        <span class="chip-label">示例</span>
        <button class="mini-chip" @click="pathFrom = 'GMV'; pathTo = '订单项'; findPath()">GMV → 订单项</button>
        <button class="mini-chip" @click="pathFrom = '客单价'; pathTo = '支付记录'; findPath()">客单价 → 支付记录</button>
      </div>

      <div v-if="pathError" class="inline-error path-error">{{ pathError }}</div>

      <div v-if="pathData && pathData.found" class="path-result">
        <div class="path-meta">共 {{ pathData.hops }} 跳</div>
        <div class="path-chain">
          <template v-for="(hop, i) in pathHops" :key="i">
            <button class="path-node" @click="searchEntity(hop.node)">{{ hop.node }}</button>
            <span v-if="hop.predicate" class="path-link">
              <span class="path-pred">{{ hop.predicate }}</span>
              <span class="path-line" :class="{ reverse: hop.reverse }">
                <span class="path-line-bar"></span>
                <span class="path-line-tip">{{ hop.reverse ? '◄' : '►' }}</span>
              </span>
            </span>
          </template>
        </div>
      </div>
      <div v-else-if="pathData && !pathData.found && !pathError" class="inline-note">
        两实体均在图中，但无可达路径。
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed } from 'vue'

// SVG 画布坐标系（等比缩放，responsive）
const W = 760
const H = 540
const CX = 380
const CY = 270

const EXAMPLE_ENTITIES = ['GMV', '客单价', '订单', '订单项', '客户']
const KNOWN_KEY = 'data-agent:graph-known:v1'

// ---- stats ----
const stats = reactive({ entity_count: null, triple_count: null })
async function loadStats() {
  try {
    const res = await fetch('/api/graph/stats')
    if (res.ok) {
      const d = await res.json()
      stats.entity_count = d.entity_count
      stats.triple_count = d.triple_count
    }
  } catch (e) { /* stats 拉取失败不阻塞主功能 */ }
}

// ---- 本地已知实体索引（后端 entity 接口 404 不返回近似实体，此处用本地积累的实体做子串提示）----
const knownEntities = ref(loadKnown())
function loadKnown() {
  try {
    const raw = localStorage.getItem(KNOWN_KEY)
    const arr = raw ? JSON.parse(raw) : []
    return new Set(Array.isArray(arr) ? arr : [])
  } catch (e) { return new Set() }
}
function seedKnown(names) {
  let changed = false
  for (const n of names || []) {
    if (n && !knownEntities.value.has(n)) { knownEntities.value.add(n); changed = true }
  }
  if (changed) {
    try { localStorage.setItem(KNOWN_KEY, JSON.stringify([...knownEntities.value])) } catch (e) { /* ignore */ }
  }
}
function localSuggest(keyword) {
  const kw = (keyword || '').trim().toLowerCase()
  if (!kw) return []
  return [...knownEntities.value].filter((n) => n.toLowerCase().includes(kw)).slice(0, 8)
}

// ---- 实体子图 ----
const entityQuery = ref('')
const entityData = ref(null)
const entityError = ref('')
const entitySuggestions = ref([])
const entityLoading = ref(false)

async function searchEntity(name) {
  const q = (name !== undefined ? name : entityQuery.value).trim()
  if (!q) return
  entityQuery.value = q
  entityLoading.value = true
  entityError.value = ''
  entitySuggestions.value = []
  try {
    const res = await fetch(`/api/graph/entity/${encodeURIComponent(q)}`)
    if (res.status === 404) {
      entityData.value = null
      const d = await res.json().catch(() => ({}))
      entityError.value = d.detail || `图谱中不存在实体: ${q}`
      entitySuggestions.value = localSuggest(q)
      return
    }
    if (!res.ok) {
      entityData.value = null
      entityError.value = `查询失败：HTTP ${res.status}`
      return
    }
    const data = await res.json()
    entityData.value = data
    seedKnown(data.nodes)
  } catch (e) {
    entityData.value = null
    entityError.value = `请求异常：${e.message}（后端未启动或网络不可达）`
  } finally {
    entityLoading.value = false
  }
}

// 文本宽度估算（CJK ~13px / 拉丁 ~7.5px @ 13px 字号），用于 pill 尺寸
function textWidth(str) {
  let w = 0
  for (const ch of str) w += ch.charCodeAt(0) > 255 ? 13 : 7.5
  return w
}
function truncate(str, n) {
  return [...str].length > n ? [...str].slice(0, n).join('') + '…' : str
}

// 放射布局：中心 + 一跳邻居；角度均分 360°，从正上方顺时针铺开
const layout = computed(() => {
  const data = entityData.value
  if (!data) return null
  const center = data.entity
  const edges = data.edges || []
  const neighborMap = new Map()
  const order = []
  for (const e of edges) {
    let other, dir
    if (e.subject === center && e.object === center) continue
    if (e.subject === center) { other = e.object; dir = 'out' }
    else if (e.object === center) { other = e.subject; dir = 'in' }
    else continue // 邻居-邻居边，另行处理
    if (!neighborMap.has(other)) { neighborMap.set(other, []); order.push(other) }
    neighborMap.get(other).push({ predicate: e.predicate, dir })
  }

  const N = order.length
  const R = Math.max(155, Math.min(225, 120 + N * 9))
  const pos = new Map()
  const nodes = order.map((name, i) => {
    const ang = -Math.PI / 2 + (N ? (i * 2 * Math.PI) / N : 0)
    const ux = Math.cos(ang)
    const uy = Math.sin(ang)
    const x = CX + R * ux
    const y = CY + R * uy
    pos.set(name, { x, y })
    const rels = neighborMap.get(name)
    const display = truncate(name, 8)
    return {
      name, x, y, display,
      w: Math.max(56, textWidth(display) + 22),
      hasOut: rels.some((r) => r.dir === 'out'),
      hasIn: rels.some((r) => r.dir === 'in'),
      lineStart: { x: CX + ux * 46, y: CY + uy * 46 },
      lineEnd: { x: x - ux * 42, y: y - uy * 42 },
      mid: { x: (CX + ux * 46 + x - ux * 42) / 2, y: (CY + uy * 46 + y - uy * 42) / 2 },
      rels,
    }
  })

  const interLinks = []
  for (const e of edges) {
    if (e.subject !== center && e.object !== center && pos.has(e.subject) && pos.has(e.object)) {
      const a = pos.get(e.subject)
      const b = pos.get(e.object)
      interLinks.push({ x1: a.x, y1: a.y, x2: b.x, y2: b.y })
    }
  }

  const cdisplay = truncate(center, 10)
  return {
    center: { name: center, display: cdisplay, w: Math.max(72, textWidth(cdisplay) + 30) },
    nodes,
    interLinks,
  }
})

// ---- 路径查询 ----
const pathFrom = ref('')
const pathTo = ref('')
const pathData = ref(null)
const pathError = ref('')
const pathLoading = ref(false)

async function findPath() {
  const from = pathFrom.value.trim()
  const to = pathTo.value.trim()
  if (!from || !to) return
  pathLoading.value = true
  pathError.value = ''
  pathData.value = null
  try {
    const qs = new URLSearchParams({ from, to })
    const res = await fetch(`/api/graph/path?${qs.toString()}`)
    if (res.status === 404) {
      const d = await res.json().catch(() => ({}))
      pathError.value = d.detail || '端点不存在'
      return
    }
    if (!res.ok) {
      pathError.value = `查询失败：HTTP ${res.status}`
      return
    }
    const data = await res.json()
    pathData.value = data
    if (data.found) seedKnown(data.path)
  } catch (e) {
    pathError.value = `请求异常：${e.message}（后端未启动或网络不可达）`
  } finally {
    pathLoading.value = false
  }
}

// 把 path + edges 转成横向链：每跳标注谓词与真实方向
const pathHops = computed(() => {
  const d = pathData.value
  if (!d || !d.found) return []
  const nodes = d.path || []
  const edges = d.edges || []
  return nodes.map((node, i) => {
    if (i >= edges.length) return { node, predicate: '', reverse: false }
    const e = edges[i]
    // edges[i] 保留真实方向；若 subject 即当前节点则为正向，否则为反向
    const reverse = e.subject !== node
    return { node, predicate: e.predicate, reverse }
  })
})

loadStats()
</script>

<style scoped>
.graph-view { display: flex; flex-direction: column; gap: 16px; padding: 20px; height: 100%; overflow-y: auto; }
.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 0; }
.card { margin-bottom: 0; }
.stats-badges { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }

.search-row { display: flex; gap: 10px; }
.entity-input { flex: 1; }
.path-row { display: flex; align-items: center; gap: 10px; }
.path-row .input { flex: 1; }
.path-arrow-glyph { color: var(--text-muted); font-size: 16px; flex-shrink: 0; }

.example-chips { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
.chip-label { font-size: 12px; color: var(--text-muted); }
.mini-chip {
  padding: 4px 11px; border-radius: 999px; cursor: pointer;
  background: var(--bg-raised); border: 1px solid var(--border-strong);
  color: var(--text-secondary); font-size: 12px; font-family: inherit;
  transition: all .15s ease;
}
.mini-chip:hover { border-color: var(--accent-strong); color: var(--accent); background: var(--accent-soft); }

.miss-block { margin-top: 12px; }
.inline-error {
  padding: 10px 12px; border-radius: var(--radius-sm);
  background: rgba(248, 113, 113, .1); border: 1px solid rgba(248, 113, 113, .3);
  color: var(--error-color); font-size: 13px;
}
.suggest-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
.suggest-empty { margin-top: 8px; font-size: 12.5px; color: var(--text-muted); }
.path-error { margin-top: 12px; }
.inline-note { margin-top: 12px; font-size: 13px; color: var(--text-secondary); }

/* SVG 画布 */
.graph-canvas { margin-top: 16px; }
.graph-svg {
  width: 100%; height: auto; max-height: 560px;
  background: radial-gradient(600px 400px at 50% 40%, rgba(45,212,191,.04), transparent 70%), var(--bg-inset);
  border: 1px solid var(--border-color); border-radius: var(--radius);
}
.arrow-head { fill: var(--text-secondary); }
.inter-link { stroke: var(--border-strong); stroke-width: 1; stroke-dasharray: 3 4; opacity: .5; }
.spoke { stroke: var(--border-strong); stroke-width: 1.6; }

.predicate-label {
  font-size: 12px; font-family: var(--font-sans);
  paint-order: stroke; stroke: var(--bg-inset); stroke-width: 3.5px; stroke-linejoin: round;
}
.predicate-out { fill: var(--accent); }
.predicate-in { fill: var(--info-color); }

.node-g { cursor: pointer; }
.node-pill { fill: var(--bg-raised); stroke: var(--border-strong); stroke-width: 1.5; transition: stroke .15s ease, fill .15s ease; }
.node-g:hover .node-pill { stroke: var(--accent); fill: #1d2634; }
.node-label {
  font-size: 13px; font-weight: 600; fill: var(--text-primary); pointer-events: none;
  paint-order: stroke; stroke: var(--bg-raised); stroke-width: 2.5px; stroke-linejoin: round;
}
.center-pill { fill: url(#center-grad); stroke: rgba(255,255,255,.25); stroke-width: 1.5; }
.center-label { font-size: 14px; font-weight: 700; fill: #03201b; pointer-events: none; }

.graph-legend {
  display: flex; align-items: center; gap: 18px; flex-wrap: wrap;
  margin-top: 10px; padding: 0 4px; font-size: 12px; color: var(--text-secondary);
}
.legend-item { display: flex; align-items: center; gap: 6px; }
.legend-swatch { width: 18px; height: 3px; border-radius: 2px; }
.legend-swatch.out { background: var(--accent); }
.legend-swatch.in { background: var(--info-color); }
.legend-hint { color: var(--text-muted); }

/* 路径链 */
.path-result { margin-top: 16px; }
.path-meta { font-size: 12px; color: var(--text-muted); margin-bottom: 10px; }
.path-chain { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.path-node {
  padding: 8px 14px; border-radius: var(--radius-sm); cursor: pointer;
  background: var(--bg-raised); border: 1px solid var(--border-strong);
  color: var(--text-primary); font-size: 13px; font-weight: 600; font-family: inherit;
  transition: all .15s ease; white-space: nowrap;
}
.path-node:hover { border-color: var(--accent-strong); color: var(--accent); background: var(--accent-soft); }
.path-link { display: inline-flex; flex-direction: column; align-items: center; gap: 2px; padding: 0 2px; }
.path-pred { font-size: 11px; color: var(--accent); white-space: nowrap; }
.path-line { display: inline-flex; align-items: center; color: var(--border-strong); }
.path-line-bar { width: 26px; height: 2px; background: var(--border-strong); }
.path-line-tip { font-size: 11px; color: var(--text-muted); margin-left: -2px; }
.path-line.reverse { flex-direction: row-reverse; }
.path-line.reverse .path-line-tip { margin-left: 0; margin-right: -2px; }

@media (max-width: 640px) {
  .path-row { flex-wrap: wrap; }
  .path-row .input { flex: 1 1 40%; }
}
</style>
