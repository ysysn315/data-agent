# Skills 系统 - OpenSpec

## 1. 模块定位

Skills 系统是 Data Agent 的**插件化能力扩展机制**，参考 Yuxi 设计，允许通过 `SKILL.md` 文件定义可复用的技能包，动态挂载到 Agent 上。

## 2. 核心设计

### 2.1 SKILL.md 格式

```yaml
---
name: schema retrieval          # 显示名称
slug: schema-retrieval          # 唯一标识（URL 友好）
description: "检索数据库表结构"   # 一句话描述
version: "1.0.0"                # 版本
author: "data-agent"            # 作者
---

# Schema 检索技能

## 操作流程
1. 用户提问 → embedding → 与表 schema 计算余弦相似度
2. 取 top-N 表（N=5），返回表名、字段、注释

## 约束
- 只返回查询相关的表，不返回全库 schema
- 表注释必须来自数据字典，不编造

## 允许的工具
- schema_search: 检索表结构
- get_table_relation: 获取表关系
```

### 2.2 依赖声明

Skills 可以声明依赖：
```yaml
---
name: data analysis
slug: data-analysis
description: "数据分析报告生成"
dependencies:
  tools:
    - sql_execute          # 依赖工具
    - chart_render
  mcps:
    - chart-mcp            # 依赖 MCP server
  skills:
    - schema-retrieval     # 依赖其他 skill
---
```

### 2.3 注入机制（Middleware）

Skills 通过 LangGraph middleware 注入 Agent：
1. 用户输入 → 匹配 Skills（slug/关键词/语义）
2. 展开依赖（tools → mcps → skills 递归）
3. 组装 system message（Skills 提示词 + 工具列表）
4. 注入 Agent 执行

## 3. 文件结构

```
app/skills/
├── __init__.py              # 模块入口
├── models.py                # Skill 数据模型
├── repository.py            # 数据库访问层
├── service.py               # 业务逻辑（加载、解析、依赖展开）
├── middleware.py            # LangGraph middleware 注入
├── remote_install.py        # 远程安装（二期）
└── buildin/                 # 内置 Skills
    ├── schema-retrieval/
    │   └── SKILL.md
    ├── sql-generation/
    │   └── SKILL.md
    └── data-visualization/
        └── SKILL.md
```

## 4. 数据库表

```sql
CREATE TABLE skills (
    id SERIAL PRIMARY KEY,
    slug VARCHAR(64) UNIQUE NOT NULL,      -- 唯一标识
    name VARCHAR(128) NOT NULL,            -- 显示名称
    description TEXT,                      -- 描述
    content TEXT NOT NULL,                 -- SKILL.md 完整内容
    source_type VARCHAR(16) DEFAULT 'builtin', -- builtin/upload/remote
    enabled BOOLEAN DEFAULT TRUE,
    user_id INTEGER,                       -- 创建者（NULL=系统内置）
    share_config JSONB,                    -- 分享配置
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
```

## 5. API 设计

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/skills` | GET | 列表（支持按 slug/名称/启用状态过滤） |
| `/api/skills/{slug}` | GET | 详情 |
| `/api/skills` | POST | 创建（上传 SKILL.md） |
| `/api/skills/{slug}` | PUT | 更新 |
| `/api/skills/{slug}` | DELETE | 删除 |
| `/api/skills/{slug}/enable` | POST | 启用 |
| `/api/skills/{slug}/disable` | POST | 禁用 |
| `/api/skills/install` | POST | 远程安装（二期） |

## 6. 与 Agent 集成

```python
# Agent 初始化时挂载 Skills middleware
from app.skills.middleware import SkillsMiddleware

agent = ChatAgent(
    llm=llm,
    tools=tools,
    middlewares=[
        SkillsMiddleware(
            enabled_skills=["schema-retrieval", "sql-generation"],
            auto_expand_dependencies=True
        )
    ]
)
```

## 7. 内置 Skills 规划

### 7.1 schema-retrieval（schema 检索）
- 功能：根据用户问题检索相关表结构
- 工具：schema_search, get_table_relation
- 输出：M-Schema 格式表结构

### 7.2 sql-generation（SQL 生成）
- 功能：基于 schema + 术语库 + few-shot 生成 SQL
- 工具：sql_validate, sql_execute
- 依赖：schema-retrieval

### 7.3 data-visualization（数据可视化）
- 功能：生成图表（柱状图/折线图/饼图）
- 工具：chart_render
- MCP：chart-mcp

### 7.4 report-generation（报告生成）
- 功能：整合 SQL 结果 + 业务洞察 → Markdown 报告
- 工具：markdown_render, file_save
- 依赖：sql-generation, data-visualization

## 8. 开发里程碑

- [ ] Week 1：SKILL.md 解析 + 数据库模型 + repository
- [ ] Week 2：service（加载、依赖展开）+ middleware 注入
- [ ] Week 3：内置 Skills（schema-retrieval、sql-generation）
- [ ] Week 4：API 路由 + 前端集成

## 9. 参考实现

- Yuxi Skills：`/Users/ysn/projects/yuxi-reference/backend/package/yuxi/agents/skills/`
- 重点参考：`service.py`（加载解析）、`middlewares/skills.py`（注入机制）、`buildin/mysql-reporter/SKILL.md`（格式示例）
