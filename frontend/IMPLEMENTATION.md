# Web 前端实现说明

对应路线图 P1-5（前端）。基于 my-agent-original 的 Vue3 + Vite 前端迁移而来，
适配 data-agent 的真实后端接口，并新增 Skills / MCP 两个管理页。

## 一、功能与本地起法

页面（侧边栏切换）：

| 页面 | 说明 | 主要接口 |
|---|---|---|
| 智能对话 | Text-to-SQL / RAG 对话，支持流式与快速两种模式，可清空会话 | `POST /api/chat_stream`、`POST /api/chat`、`DELETE /api/chat/clear/{id}` |
| 数据源管理 | 接入/同步数据源，生成 AI 语义草稿，逐表审核并预览正式 M-Schema | `/api/datasources*` |
| Skills 管理 | 技能卡片列表（按来源分组），点击看详情（frontmatter + 正文），启停开关 | `GET /api/skills`、`GET /api/skills/{slug}`、`POST /api/skills/{slug}/enable\|disable` |
| MCP 管理 | server 列表，测试连接拉工具列表，启停开关 | `GET /api/mcp/servers`、`POST /api/mcp/servers/{slug}/test\|enable\|disable` |
| 知识库管理 | 上传文档建索引、读取真实 Milvus 文档列表 | `POST /api/upload`、`GET /api/documents` |
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
│       ├── DataSourcesView.vue # 数据源接入与语义审核
│       ├── SkillsView.vue  # Skills 管理（新增）
│       ├── McpView.vue     # MCP 管理（新增）
│       ├── UploadView.vue  # 知识库上传（迁移）
│       └── StatusView.vue  # 系统状态（迁移，修正 Milvus 路径）
```

**页面组织**：沿用迁移源的「单页 + v-if 切换视图」方案，没有引入 vue-router。
页面数量仍较少，用不上路由的懒加载与地址栏同步，一个 `currentView` ref
足够，保持零额外依赖。

**SSE 解析（ChatView）**：后端 `chat_stream` 以 SSE 逐块下发，每条形如
`data: {"type": "content"|"sources"|"done"|"error", "data": "..."}`。其中
`ChatService.chat_stream` 始终输出结构化的 `content`/`sources` 事件，前端用
`ReadableStream` 的 reader 循环读取，关键点：

- 用 `buffer` 跨 `read()` 缓冲不完整的行，按 `\n\n`（SSE 事件分隔）切分，
  `pop()` 出最后一段留到下一轮，避免把半条 JSON 截断后 `JSON.parse` 失败
  （迁移源按 `\n` 切且不缓冲，长回复下会偶发丢字）。
- `content` 追加到当前 assistant 消息并滚动到底；`error` 追加错误文案；
  `done` 不需特殊处理，流结束循环自然退出。

聊天顶部会读取 `/api/datasources` 展示当前工作空间数据源；请求把选中 ID 放进
`ChatRequest.datasource_id`。切换数据源时主动清空当前会话，避免上一数据库的表名、
字段和结果继续留在对话历史。选中值只保存在 localStorage，真正的租户归属仍由后端校验。

**数据源页（DataSourcesView）**：SQLite 填允许目录内路径，PostgreSQL/MySQL 填结构化
连接参数；密码输入框提交后立即清空。页面按 `physical/ai/reviewed` 三层元数据显示
AI 草稿，批准时逐字段提交，拒绝时不让草稿进入正式 M-Schema。连接/同步/删除在后端
仍由 admin 守卫，前端按钮不是权限边界。

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
- 鉴权模式尚无独立登录/密钥管理页；当前数据源管理 UI 主要面向默认 demo 模式，
  开启 `AUTH_ENABLED` 后需由统一网关或后续前端 API Key 注入层补 Authorization header。
- 数据源选择目前只在 ChatView 生效；TasksView/Analysis 任务请求尚未携带 `datasource_id`，
  因此其示例文案和执行路径仍面向演示库。

## 五、v2 页面（任务中心 / 知识图谱 / 知识管理）

在既有「单页 + v-if 切换」骨架上新增三个视图，沿用 `styles/main.css` 的设计
token 与组件类（`.card`/`.btn`/`.badge`/`.input`/`.monogram`/`.modal-*`/
`.markdown-content`），不引入任何新 npm 依赖（图谱可视化为手写 SVG）。导航图标
与既有一致：线性描边、`viewBox="0 0 20 20"`、`stroke=currentColor` 随主题变色。

| 页面 | 导航分组 | 主要接口 |
|---|---|---|
| 任务中心 TasksView | 工作台 | `POST /api/tasks`、`GET /api/tasks/{id}`、SSE `GET /api/tasks/{id}/events` |
| 知识图谱 GraphView | 平台管理 | `GET /api/graph/stats`、`/api/graph/entity/{name}`、`/api/graph/path` |
| 知识管理 KnowledgeView | 平台管理 | `GET\|POST\|DELETE /api/sql-examples[/{id}]`、`/api/terminology[/{term}]` |

### 任务中心与后端事件模型的对应

**任务类型即后端 `TASK_REGISTRY` 的真实 key**：数据分析 = `run_analysis_task`
（params `{question}`），评估跑批 = `eval`（params `{limit?, model?}`）。注意
worker 函数名是 `run_eval_task`，但 `POST /api/tasks` 的 `type` 只认注册表 key
`eval`——按接口真实形状提交，不用函数名。

**两套状态词分工**：元数据 `status` 走 queued/running/**done/failed**（GET 状态用），
事件 `type` 走 started/progress/**done/error**（SSE 用，`events.py` 里终结事件是
done/error）。前端徽章用 status；时间线用 event.type；SSE 收到 done/error 时把本地
status 就地更新为 done/failed。

**事件 schema 单一**（`TaskEvent`：`type/message/progress/payload` + 服务端补的
`ts`）。前端只认这一种结构：`progress`（0~1）驱动进度条，`message` 进时间线，
`payload` 按任务类型解读。

**报告不在事件里**：分析任务的 `done` 事件 payload 只带 `{phase, steps}`，
**完整 Markdown 报告落在 `GET /api/tasks/{id}` 的 `result.report`**（worker 用
`mark_done` 写进元数据 Hash）。因此收到 done 后再拉一次状态取 `result.report`，
用 `marked.parse` 渲染；评估任务则从 `result.summary` / done 事件 payload 取准确率。

**EventSource 而非 fetch-reader**：SSE 端点是 GET，直接用原生 `EventSource`
订阅，后端 `data: {...}` 无 `event:` 字段 → 走默认 `onmessage`。服务端 `stream_sse`
从 stream 头部**回放全部事件**再发终结事件后主动关闭，所以「任务已结束后再打开详情」
也能拿到完整时间线。

**断线不做自动重连（有意）**：`EventSource` 默认会在连接断开时自动重连，本页在
收到终结事件（done/error）或 `onerror` 时**显式 `es.close()`**。理由：任务事件是
一次性、可回放的有限序列，读到 done/error 即完成，重连只会从头重复拉全量、无新增
价值；长任务的实时性由服务端推送保证，不需要客户端补偿重连。断线即视为该次订阅
结束，用户可手动「刷新状态」或重新打开详情重新订阅。

**Redis/worker 未起时的失败态（别白屏）**：Redis 不可达时后端依赖
`get_task_service` 会在 arq 建连重试后失败——实测 `POST /api/tasks` 约 5s 返回
500（响应体是纯文本 `Internal Server Error`，非 JSON）。前端据此：`readError` 兼容
非 JSON 响应回退到 `HTTP 5xx`；提交失败渲染成红色错误卡并附「请确认 Redis 与 arq
worker 已启动」的提示；再叠一层 `AbortController` 超时（POST/GET 12~15s）与
EventSource 打开看门狗（15s 未 `onopen` 即报超时并关闭），确保后端悬挂时也给明确
反馈而不是无限转圈。任务 ID 只在**提交成功**后写入 localStorage
（`data-agent:tasks:v1`），刷新页面按存量逐个 `GET` 回填状态。

### 知识图谱 SVG 放射布局算法

纯手写 SVG（无 d3/无图库），坐标系固定 `viewBox 0 0 760 540`、中心
`(380, 270)`，容器 `width:100%` 等比缩放。

**角度分配**：取所有与中心实体入射的边，按「另一端实体」聚合为邻居（同一邻居的
多条边合并，各记 `dir=in/out`）。设邻居数 `N`，第 `i` 个邻居角度
`θ_i = -π/2 + i·(2π/N)`——从正上方开始、顺时针均分 360°；半径
`R = clamp(120 + 9N, 155, 225)`（邻居越多环越大，缓解 pill 拥挤）。邻居位置
`(cx + R·cosθ, cy + R·sinθ)`。

**连线与方向**：连线两端各留间隙（近中心 46px、近邻居 42px），空出的段用来放
箭头与谓词，避免被 pill 盖住。方向用**单个 marker + `orient="auto-start-reverse"`**
复用：出边（中心→邻居）挂 `marker-end`，入边（邻居→中心）挂 `marker-start`
（auto-start-reverse 让起点箭头反向指回中心）；一个邻居同时有出/入边则两端都出箭头。
谓词文字排在连线中点，出边用强调色、入边用信息色区分，字形加 `paint-order:stroke`
描边光晕保证在深色底上可读。中心与邻居都渲染为 pill（圆角矩形，按标签估算宽度、
超长截断并在 `<title>` 保留全名）；**邻居 pill 可点击 = 下钻该实体子图**。邻居之间
的边（非中心入射）以细弱虚线补画，只示结构、不加箭头标签。

**未命中的近似实体提示**：后端 `GET /api/graph/entity/{name}` 命中 404 时**只返回
`detail`**——`suggest_entities` 仅供 Agent 的 `graph_tool` 内部调用、未开放 HTTP 路由
（且本任务禁改后端）。为满足「近似实体可点击」，前端维护一个本地已知实体索引
（`data-agent:graph-known:v1`，从每次成功的 entity `nodes` / path `path` 累积），
未命中时对该索引做与后端同款的子串匹配给出可点击候选；索引为空时提示「先查一个
已知实体建立本地索引」。这是在不新增后端接口前提下最贴近需求的做法，取舍已在此注明。

**路径链**：`GET /api/graph/path` 返回 `{found, hops, path[], edges[], chain}`。
前端不用现成 `chain` 字符串，而是用 `path` + `edges` 自绘横向链：逐跳判断
`edges[i].subject === path[i]` 决定箭头正/反向，节点渲染为可点击 pill（点击即查其
子图），谓词标在连接线上。顶部 `stats` 徽章取 `entity_count` / `triple_count`。

### 知识管理

两个 Tab 复用同一套表单/列表模式：SQL 示例（question/sql/verified，POST 同问题即
更新、DELETE 按 id）、业务术语（term 唯一键 upsert、synonyms 由顿号/逗号/空格切分
成数组、DELETE 按 term）。校验与错误走 `.input`/`.btn`/`.badge` 体系；`readError`
兼容 FastAPI 422 的 `detail` 数组（拼成一行）。挂载时**并行预取两个列表**，让非活动
Tab 的计数徽章一开始就准确。路径参数一律 `encodeURIComponent`（实体名/术语含中文），
path 查询用 `URLSearchParams` 编码 `from`/`to`（对应后端 alias）。

### 取舍与欠账（v2）

- **零新依赖**：图谱可视化坚持手写 SVG（设计系统一致 + 零依赖），未引入 d3/echarts。
- **对话页「分析这个问题」入口**：任务书列为可选（不强制）。因无 router/store 做跨视图
  传参、且同步 `/api/analysis` 依赖真实 LLM，为不给作为视觉基准的 ChatView 引入回归
  风险，本轮从简未做；后续如需可在 ChatView 增一个跳转任务中心并预填问题的入口。
- **SSE 实时链路**未在本地跑通端到端：需 Redis + `arq` worker 同时在跑。已用 curl
  校验 REST 形状、并验证 Redis 未起时的失败态提示；happy-path 的事件渲染逻辑严格
  对齐 `events.py` / `worker.py` 的事件序列。
- 图谱放射布局对**超大邻居数**（如几十个一跳邻居）会拥挤，演示级图谱（千级三元组、
  个位数邻居）足够；如需可加分页/力导，但那会偏离「零依赖手写」的取舍。
