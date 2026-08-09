# 智能数据分析 Agent 平台 - 初始需求基线（历史）

> **文档状态：历史基线，不代表当前实现。** 本文保留项目启动时的目标、参考对照和阶段计划，
> 因此后文的“未实现”“二期”和 checklist 不随代码进展逐项改写。当前能力、可信数字与已知边界请看
> [README.md](README.md)，后续计划请看 [docs/openspec/roadmap.md](docs/openspec/roadmap.md)。
> 不要从本文直接生成简历或当前状态说明。

## 0. 参考项目

本项目融合以下三个开源项目的核心能力：

| 项目 | 地址 | 借鉴内容 |
|------|------|---------|
| **Yuxi（语析）** | https://github.com/xerrors/Yuxi | Agent 平台架构、Skills 系统、MCP 集成、异步执行（ARQ）、多租户、知识图谱、沙箱、评估体系 |
| **SQLBot** | https://github.com/dataease/SQLBot | Text-to-SQL、schema embedding 检索、SQL 示例训练、行列级权限、多数据源、术语库、提示词模板分层 |
| **my-agent（原项目）** | https://github.com/ysysn315/my-agent | LangGraph 工作流（P-O-R）、RAG 链路（分块/混合检索/BGE 重排）、Chat Agent、evals 评估体系 |

## 1. 项目定位

基于 Yuxi 架构思想，融合 SQLBot 数据场景，保留 my-agent 核心能力，构建**企业级智能数据分析 Agent 平台**。

**一句话**：一个支持自然语言查询、多数据源接入、Skills 插件化扩展的数据分析 Agent。

## 2. 核心目标

1. **Text-to-SQL 能力**：自然语言 → 精准 SQL → 执行 → 可视化结果
2. **Agent 平台架构**：Skills 插件化、MCP 标准化工具、异步执行
3. **RAG 增强**：schema 检索 + 业务知识库 + SQL 示例训练
4. **可 demo**：基于公开数据集（Kaggle Brazilian E-Commerce）

## 3. 技术架构

### 3.1 整体架构（参考 Yuxi）

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   前端 Vue   │────▶│  FastAPI    │────▶│  LangGraph  │
│  (对话界面)  │     │  (API 网关)  │     │  (Agent 工作流)│
└─────────────┘     └─────────────┘     └─────────────┘
                           │                    │
                           ▼                    ▼
                    ┌─────────────┐     ┌─────────────┐
                    │   ARQ       │     │  Skills/    │
                    │  (异步任务)  │     │  MCP 工具   │
                    └─────────────┘     └─────────────┘
                           │                    │
                           ▼                    ▼
                    ┌─────────────┐     ┌─────────────┐
                    │   Redis     │     │  多数据源    │
                    │  (事件流)   │     │ (MySQL/PG/  │
                    └─────────────┘     │   SQLite)   │
                                         └─────────────┘
```

### 3.2 模块划分

| 模块 | 职责 | 技术选型 | 来源 |
|------|------|---------|------|
| **Chat Agent** | 轻量对话交互、意图识别、简单查询 | LangGraph ReAct | 保留 my-agent |
| **Analysis Agent** | 复杂数据分析、多步推理、报告生成 | LangGraph P-O-R | 保留 my-agent |
| **LLM 抽象层** | 统一模型调用，支持自定义 base_url/api_key | OpenAI 兼容接口 | **新增**（解决 my-agent 绑定 ChatTongyi 问题） |
| **Skills 系统** | 插件化能力扩展（schema 检索、SQL 生成、图表生成） | SKILL.md + middleware | 抄 Yuxi |
| **MCP 集成** | 标准化工具接入（数据库连接、图表渲染） | langchain_mcp_adapters | 抄 Yuxi |
| **RAG 链路** | schema embedding 检索 + 业务知识库 + SQL 示例 | Milvus + BGE + BM25 | 保留 my-agent + 抄 SQLBot |
| **Text-to-SQL** | 自然语言 → SQL → 执行 → 结果 | sqlglot + prompt 模板 | 抄 SQLBot |
| **异步执行** | 长任务异步化、事件流推送 | ARQ + Redis | 抄 Yuxi |
| **权限系统** | 行列级数据权限、API Key 管理 | JSONB 规则引擎 | 抄 SQLBot |

### 3.3 LLM 抽象层设计（新增，解决绑定问题）

**问题**：my-agent 当前绑定死 `ChatTongyi`（通义千问），无法切换到美团 FRIDAY API 或其他 OpenAI 兼容接口。

**方案**：引入 LLM 抽象层，支持自定义 `base_url` + `api_key`，兼容 OpenAI 接口规范。

```python
# app/core/llm.py
from langchain_openai import ChatOpenAI
from typing import Optional


class LLMFactory:
    """LLM 工厂，支持自定义 base_url 和 api_key"""

    @staticmethod
    def create_llm(
        model: str,
        api_key: str,
        base_url: Optional[str] = None,  # 自定义 endpoint，如美团 FRIDAY
        temperature: float = 0.1,
        streaming: bool = False,
        **kwargs,
    ):
        """
        创建 LLM 实例

        参数:
            model: 模型名称（如 "glm-5.2", "qwen-max", "gpt-4"）
            api_key: API Key
            base_url: 自定义 API 地址（如 "https://aigc.sankuai.com/v1/openai/native"）
                     不传则默认 OpenAI 官方地址
            temperature: 温度
            streaming: 是否流式
        """
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,  # 关键：支持自定义 endpoint
            temperature=temperature,
            streaming=streaming,
            **kwargs,
        )


# 使用示例
# 1. 调用美团 FRIDAY API
mt_llm = LLMFactory.create_llm(
    model="glm-5.2", api_key="220641...73", base_url="https://aigc.sankuai.com/v1/openai/native"
)

# 2. 调用 OpenAI 官方
openai_llm = LLMFactory.create_llm(
    model="gpt-4",
    api_key="sk-...",
    # base_url 不传，默认 OpenAI
)

# 3. 调用本地 Ollama
ollama_llm = LLMFactory.create_llm(
    model="bge-m3",
    api_key="ollama",  # Ollama 不需要真实 key
    base_url="http://localhost:11434/v1",
)
```

**配置方式**（环境变量或配置文件）：
```yaml
# config.yaml
llm:
  default:
    model: "glm-5.2"
    api_key: "${MT_FRIDAY_API_KEY}"
    base_url: "https://aigc.sankuai.com/v1/openai/native"
  fallback:
    model: "qwen-max"
    api_key: "${DASHSCOPE_API_KEY}"
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
  embedding:
    model: "bge-m3"
    api_key: "ollama"
    base_url: "http://localhost:11434/v1"
```

**优势**：
1. **解耦**：不再绑定特定厂商（通义/OpenAI），任何 OpenAI 兼容接口都能接
2. **可切换**：通过配置切换美团 FRIDAY / OpenAI / 本地 Ollama
3. **可扩展**：新增模型只需加配置，不改代码

## 4. 核心功能

### 4.1 数据分析流程

```
用户提问 → Chat Agent 意图识别
  ├─ 简单查询 → 直接生成 SQL → 执行 → 返回结果
  └─ 复杂分析 → 创建 Analysis Agent 工作流
       → Planner 拆解任务（查哪些表、做什么分析、生成什么图表）
       → Operation 执行（schema 检索 → SQL 生成 → 执行 → 可视化）
       → Reflection 检查结果（数据合理性、图表清晰度）
       → 生成 Markdown 分析报告
```

### 4.2 Skills 设计（抄 Yuxi）

**内置 Skills**：
1. `schema-retrieval`：表结构检索，根据问题找相关表
2. `sql-generation`：SQL 生成，基于 schema + 术语库 + few-shot
3. `data-visualization`：图表生成，调用 MCP 图表工具
4. `report-generation`：分析报告生成，整合 SQL 结果 + 业务洞察
5. `knowledge-base`：业务知识库问答，术语解释、指标口径

**SKILL.md 示例**（schema-retrieval）：
```yaml
---
name: schema retrieval
slug: schema-retrieval
description: "检索数据库表结构，根据用户问题找到相关的表和字段"
---

# Schema 检索技能

## 操作流程
1. 用户提问 → embedding → 与表 schema 计算余弦相似度
2. 取 top-N 表（N=5），返回表名、字段、注释
3. 如果涉及多表，补充表关系（外键关联）

## 约束
- 只返回查询相关的表，不返回全库 schema
- 表注释必须来自数据字典，不编造
```

### 4.3 RAG 链路设计（融合 my-agent + SQLBot）

**结构化检索（抄 SQLBot）**：
- schema embedding：表结构向量化，pgvector 存储，余弦相似度召回
- SQL 示例：历史 question→SQL 对，few-shot 注入 prompt
- 术语库：业务术语 + 同义词映射，精确匹配

**非结构化检索（保留 my-agent）**：
- 文档分块：recursive/markdown 策略
- 混合检索：Milvus 向量 + BM25
- BGE 重排：精排 top-k

### 4.4 Text-to-SQL 设计（抄 SQLBot）

**Prompt 模板分层**：
- 主模板：Instruction + Rules + Process
- 方言模板：MySQL/PostgreSQL/SQLite 各自规则（引号、LIMIT、函数）
- 零容忍规则：默认 LIMIT 1000、多表必须限定别名、禁止增删改

**Schema 格式**（M-Schema）：
```
# Table: ecommerce.orders, 订单表
[
(order_id:INTEGER, 订单ID),
(customer_id:INTEGER, 客户ID),
(order_status:VARCHAR, 订单状态),
(order_purchase_timestamp:TIMESTAMP, 下单时间)
]
```

### 4.5 多数据源支持

**演示数据源**：
- SQLite：Kaggle Brazilian E-Commerce（主数据）
- PostgreSQL：分析结果存储、SQL 示例库
- MySQL（可选）：展示多数据源能力

**数据源管理**：
- 连接配置加密存储（AES）
- schema 自动同步（定时任务）
- 行列级权限（抄 SQLBot）

## 5. 数据准备

### 5.1 主数据集：Kaggle Brazilian E-Commerce

**表结构**：
- orders（订单）：order_id, customer_id, order_status, timestamps
- order_items（订单项）：order_id, product_id, seller_id, price
- customers（客户）：customer_id, city, state
- products（商品）：product_id, category, name
- sellers（卖家）：seller_id, city, state
- payments（支付）：order_id, payment_type, value

**导入方式**：
```bash
# 下载 Kaggle 数据集 → 导入 SQLite
sqlite3 ecommerce.db
.mode csv
.import orders.csv orders
.import order_items.csv order_items
...
```

### 5.2 知识库数据

**业务术语库**：
- GMV = 成交总额
- 复购率 = 购买 2 次以上的客户占比
- 客单价 = 总金额 / 订单数

**SQL 示例库**：
- 历史分析案例（question → SQL → 结果）
- 持续运营沉淀

## 6. 非功能需求

### 6.1 性能
- 简单查询响应 < 3s
- 复杂分析异步执行，事件流推送进度
- schema embedding 预计算，查询时实时召回

### 6.2 可扩展
- Skills 插件化：新增能力只需写 SKILL.md
- MCP 标准化：外部工具即插即用
- 多数据源：新增数据源只需配置连接

### 6.3 安全
- API Key 鉴权
- 行列级数据权限
- SQL 注入防护（sqlglot 解析校验）

## 7. 里程碑

### Week 1：架构搭建
- [ ] 项目骨架（FastAPI + Vue + LangGraph）
- [ ] Skills 系统（SKILL.md 格式 + middleware 注入）
- [ ] MCP 集成（langchain_mcp_adapters）

### Week 2：核心能力
- [ ] Text-to-SQL（prompt 模板 + schema 检索 + SQL 生成）
- [ ] RAG 链路（schema embedding + 混合检索 + BGE 重排）
- [ ] 多数据源接入（SQLite + PostgreSQL）

### Week 3：差异化功能
- [ ] 行列级权限（抄 SQLBot）
- [ ] 异步执行（ARQ + Redis 事件流）
- [ ] 分析报告生成（Markdown + 图表）

### Week 4：数据与收尾
- [ ] Kaggle 数据导入 + 知识库建设
- [ ] Docker Compose 一键部署
- [ ] README + demo 视频

## 8. 简历描述

> 基于 LangGraph 和 Yuxi 架构设计智能数据分析 Agent 平台，支持 Skills 插件化扩展与 MCP 标准化工具接入；实现 Text-to-SQL 能力，通过 schema embedding 检索和 RAG 增强生成精准 SQL；基于 Kaggle 电商公开数据集构建演示场景，支持自然语言查询销售趋势、用户行为分析、商品洞察等复杂分析任务；设计行列级数据权限与异步执行架构，具备企业级数据平台能力。

## 9. 技术债务与风险

1. **沙箱环境**：Yuxi 的沙箱太重，先用本地 subprocess 替代，二期再考虑容器化
2. **多租户**：先做单用户 + API Key，多租户二期扩展
3. **知识图谱**：Yuxi 的 Neo4j 图谱太重，先用 Milvus 向量检索替代
4. **数据量**：Kaggle 数据集 10 万+条，够演示，不够生产级

## 10. 参考项目功能对照（未纳入本期但值得二期考虑）

以下功能在参考项目中存在，本期未实现，但可作为二期扩展方向：

### 10.1 Yuxi 有但本期未加

| 功能 | Yuxi 实现 | 二期价值 | 优先级 |
|------|----------|---------|--------|
| **多租户/部门体系** | 用户/部门/工作空间隔离、OIDC 登录、操作审计 | 企业级背书，体现权限设计能力 | ★★★★☆ |
| **知识图谱** | LLM 抽取实体三元组 → Neo4j + Milvus 双存储 → 图谱推理 | 关联分析（"这个商品和那个供应商有什么关系"） | ★★★☆☆ |
| **Agent 沙箱** | 独立 provisioner 容器、虚拟路径隔离、每 thread 一个沙箱 | Agent 执行代码的安全环境 | ★★☆☆☆（太重） |
| **评估体系** | RAG benchmark 生成、检索/生成质量评估 | 证明系统有效性，简历差异化 | ★★★★★ |
| **子智能体（SubAgent）** | LangGraph 子图、任务拆解给其他 Agent | 复杂任务编排 | ★★★★☆ |
| **文档解析（MinerU/PaddleX）** | PDF/Word/Excel 解析、OCR | 知识库数据来源 | ★★★☆☆ |
| **Langfuse 集成** | 调用链追踪、可观测性 | 调试和优化 | ★★★☆☆ |

### 10.2 SQLBot 有但本期未加

| 功能 | SQLBot 实现 | 二期价值 | 优先级 |
|------|------------|---------|--------|
| **数据训练（SQL 示例运营）** | 历史 question→SQL 对持续沉淀，越问越准 | 提升准确率的核心手段 | ★★★★★ |
| **术语库** | 业务术语 + 同义词映射（"GMV"="成交总额"） | 业务理解能力 | ★★★★☆ |
| **自定义提示词** | 用户按场景覆盖默认规则 | 灵活性 | ★★★☆☆ |
| **看板/仪表盘** | 拖拽式画布、图表组件（G2）、数据大屏 | 可视化交付 | ★★★☆☆ |
| **嵌入式集成** | Web iframe/弹窗/MCP/API Key 多种嵌入 | 集成到第三方系统 | ★★★☆☆ |
| **审计日志** | 操作记录、登录日志 | 企业合规 | ★★☆☆☆ |
| **多语言 i18n** | 中英韩繁四语言 | 国际化 | ★☆☆☆☆ |
| **12 种数据源方言** | MySQL/PG/Oracle/MSSQL/ClickHouse/Doris/Hive/ES/达梦/人大金仓/Redshift/StarRocks | 数据源覆盖广度 | ★★☆☆☆（3 种够 demo） |

### 10.3 my-agent 有但本期需保留/强化

| 功能 | my-agent 实现 | 本期处理 |
|------|--------------|---------|
| **evals 评估体系** | retrieval/generation 评估：hit@k、recall@k、MRR、baseline 对比 | **保留并强化**，这是简历差异化亮点 |
| **噪音文档测试** | aiops-docs-noise 目录，测试检索鲁棒性 | 保留，改测数据分析场景 |
| **BGE 重排** | 本地 BGE reranker，CUDA/CPU 自适应 | 保留 |
| **混合检索** | Milvus 向量 + BM25 合并 | 保留 |

## 11. 二期扩展建议

基于以上对照，二期优先级最高的三个扩展：

1. **评估体系（Yuxi + my-agent 都有）**
   - 理由：能证明系统有效性，简历上写"建立检索/生成质量评估体系，hit@k 提升 X%"
   - 实现：抄 my-agent 的 evals + Yuxi 的 benchmark 生成

2. **数据训练运营（SQLBot 核心）**
   - 理由："越问越准"是 SQLBot 的核心卖点，体现持续运营思维
   - 实现：SQL 示例库 + 用户反馈机制 + 自动训练

3. **多租户/权限体系（Yuxi 企业级）**
   - 理由：企业级背书，和美团实习的"多 IDC 告警"呼应
   - 实现：工作空间隔离 + 行列权限 + 操作审计
