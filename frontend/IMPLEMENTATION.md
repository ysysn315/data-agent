# Web 前端实现说明

对应路线图 P1-5（前端）。基于 my-agent-original 的 Vue3 + Vite 前端迁移而来，
适配 data-agent 的真实后端接口，并新增 Skills / MCP 两个管理页。

## 一、功能与本地起法

页面（侧边栏切换）：

| 页面 | 说明 | 主要接口 |
|---|---|---|
| 智能对话 | Text-to-SQL / RAG 对话，支持流式与快速两种模式，可清空会话 | `POST /api/chat_stream`、`POST /api/chat`、`DELETE /api/chat/clear/{id}` |
| Skills 管理 | 技能卡片列表（按来源分组），点击看详情（frontmatter + 正文），启停开关 | `GET /api/skills`、`GET /api/skills/{slug}`、`POST /api/skills/{slug}/enable\|disable` |
| MCP 管理 | server 列表，测试连接拉工具列表，启停开关 | `GET /api/mcp/servers`、`POST /api/mcp/servers/{slug}/test\|enable\|disable` |
| 知识库管理 | 上传文档建索引（迁移自 my-agent，接口一致） | `POST /api/upload` |
| 系统状态 | API / Milvus / Redis 健康与会话统计 | `GET /health`、`GET /api/milvus/health`、`GET /api/chat/sessions` |

本地启动：

```bash
# 1) 起后端（默认 9900，chat 不依赖 Milvus）
cd data-agent
.venv/bin/uvicorn app.main:app --reload --port 9900

# 2) 起前端（另开一个终端）
cd data-agent/frontend
npm install
npm run dev      # 访问 http://localhost:3000
```

生产构建：`npm run build`，产物在 `dist/`（已 gitignore）。

## 二、结构与关键实现

```
frontend/
├── index.html            # 入口，标题为「智能数据分析 Agent 平台」
├── vite.config.js        # dev server + /api、/health 反向代理
├── package.json          # 依赖：vue / marked / highlight.js
├── src/
│   ├── main.js           # createApp 挂载
│   ├── App.vue           # 侧边栏导航 + 视图切换（无路由，v-if 切换）
│   ├── styles/main.css   # 全局暗色主题，CSS 变量
│   └── views/
│       ├── ChatView.vue    # 对话（SSE 解析）
│       ├── SkillsView.vue  # Skills 管理（新增）
│       ├── McpView.vue     # MCP 管理（新增）
│       ├── UploadView.vue  # 知识库上传（迁移）
│       └── StatusView.vue  # 系统状态（迁移，修正 Milvus 路径）
```

**页面组织**：沿用迁移源的「单页 + v-if 切换视图」方案，没有引入 vue-router。
页面数量少（5 个），用不上路由的懒加载与地址栏同步，一个 `currentView` ref
足够，保持零额外依赖。

**SSE 解析（ChatView）**：后端 `chat_stream` 以 SSE 逐块下发，每条形如
`data: {"type": "content"|"done"|"error", "data": "..."}`。前端用
`ReadableStream` 的 reader 循环读取，关键点：

- 用 `buffer` 跨 `read()` 缓冲不完整的行，按 `\n\n`（SSE 事件分隔）切分，
  `pop()` 出最后一段留到下一轮，避免把半条 JSON 截断后 `JSON.parse` 失败
  （迁移源按 `\n` 切且不缓冲，长回复下会偶发丢字）。
- `content` 追加到当前 assistant 消息并滚动到底；`error` 追加错误文案；
  `done` 不需特殊处理，流结束循环自然退出。

**Skills 页**：`GET /api/skills?enabled_only=false` 一次取全量（含被禁用的，
否则默认只返回启用的、页面上无法再启用它们）。按 `source_type`（builtin/upload/
remote）分组渲染卡片；点击卡片调 `GET /api/skills/{slug}` 取详情，
frontmatter 以表格展示、body 用 marked 渲染为 markdown；卡片上的启停按钮直接
调 `enable`/`disable` 并用返回值就地更新本地状态，避免整表刷新。

**MCP 页**：`GET /api/mcp/servers` 列出 server，按 transport 决定展示 url（http 类）
或 command（stdio）。「测试连接」调 `POST /{slug}/test`，成功返回
`{tool_count, tools:[{name, description}]}` 渲染成工具列表；后端连接失败返回
502、错误信息在 `detail` 字段，前端据此展示红色错误块。测试允许对未启用的
server 执行（与后端语义一致）。

**代理配置（vite.config.js）**：dev server 把 `/api` 与 `/health` 反代到
`http://localhost:9900`，浏览器侧免跨域、直连 FastAPI。相比迁移源去掉了单独的
`/milvus` 代理项 —— data-agent 里 Milvus 健康检查已挂到 `/api/milvus/health`
（统一在 `/api` 前缀下），走 `/api` 代理即可。

## 三、参考来源

- **迁移自 my-agent-original/frontend**：整体技术栈（Vue3 `<script setup>` +
  Vite + marked + highlight.js）、暗色主题样式（`styles/main.css` 原样保留）、
  侧边栏布局与 `App.vue` 骨架、`ChatView`/`UploadView`/`StatusView` 三个视图。
- **Skills 管理 UI 参考 Yuxi**：`yuxi-reference/web/src/components/extensions/
  SkillCardList.vue` 与 `SkillDetailView.vue` 的交互思路 —— 卡片网格、按来源
  分组（内置/上传/远程）、点击卡片弹出详情并渲染 SKILL.md、启停开关就地更新。
  只借鉴交互结构，不搬其实现。

## 四、区别与取舍

**为什么不用 Yuxi 的 antd 体系**：Yuxi 前端是 ant-design-vue + less + vue-router +
lucide 图标 + 独立的 `apis/` 层，是一套完整中后台框架。本项目迁移基座是
my-agent 的「零 UI 框架、手写 CSS 变量」的轻量栈。为保持技术栈一致、避免为两个
管理页引入一整套组件库（打包体积、样式冲突、学习成本），这里用原生 Vue + 既有
CSS 变量复刻了 Yuxi 的卡片/详情/开关交互，视觉与对话页统一。

**从迁移源砍掉的东西**：

- **AIOps 故障分析页（AIOpsView）**：这是 my-agent OnCall 场景的专属功能，
  data-agent 后端没有 `ai_ops` 接口，按任务要求整页删除，导航项一并移除。
- **README 里的 `/api/ai_ops*` 接口说明**：随 AIOps 页删除。
- **单独的 `/milvus` 代理**：后端已把 milvus 路由收拢到 `/api/milvus`，
  StatusView 的健康检查 URL 相应改为 `/api/milvus/health`。

**保留但已知的欠账**：

- 无 vue-router，页面状态不进地址栏、刷新回到对话页 —— 页面少，暂不需要。
- Skills 页仅做「浏览 + 启停 + 看详情」，未迁移 Yuxi 的远程安装/批量删除/上传
  （后端虽有 `/remote/*` 接口，但涉及 GitHub 拉取与生效范围，超出 P1-5 范围）。
- 打包为单 chunk（~1MB，主要是 highlight.js）,构建有体积告警但不影响使用；
  如需优化可按需引入 highlight.js 语言包或做代码分割。
- 会话列表来自后端进程内存（SessionStore），后端重启即清空。
