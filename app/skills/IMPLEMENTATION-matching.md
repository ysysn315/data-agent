# Skills 语义匹配速读（embedding 向量召回 + jieba 回退）

> 背景规格见 [docs/openspec/skills-optimization.md](../../docs/openspec/skills-optimization.md) §2.7、
> [docs/openspec/skills-system.md](../../docs/openspec/skills-system.md) §7。
> 定位：给 `SkillsMiddleware(auto_match=True)` 的"按用户输入挑技能"换一个更合理的召回器 ——
> 从 jieba 关键词重叠升级为 embedding 语义召回，未配置或调用失败时**逐字节回退**到原 jieba 打分。
> 只动一层，不改披露/激活/门控主链路。

核心文件：

- `matching.py` —— `SkillMatcher`（策略判定 / 向量召回 / jieba 回退 / 向量缓存 + 失效）
- `service.py` —— `match_skills_by_query` 解析候选集后把打分排序**委托** `SkillMatcher.match`；
  `_tokenize` 迁入 matching.py，此处留委托垫片（`app/text2sql/examples.py` 仍按老签名调用）
- `middleware.py::_resolve_root_slugs` —— `auto_match` 分支调 `match_skills_by_query`，契约不变

## ① 功能与行为：auto 策略判定表

入口 `match_skills_by_query(query, candidate_slugs=None, top_k=3)` 签名与 v1 完全一致。
内部按 `SkillMatcher.strategy` 选路，`auto` 再看 settings 的 embedding 配置：

| strategy | settings 条件 | 走哪条 | 说明 |
|---|---|---|---|
| `auto` | `settings is None` | jieba | 无配置来源，安全默认 |
| `auto` | `embedding_provider == "bge"` | embedding | 本地 BGE 无需 key |
| `auto` | `embedding_api_key` 非空 | embedding | 远程向量接口已配 key |
| `auto` | 其余（key 空且非 bge） | jieba | 离线/未配 → 关键词兜底 |
| `embedding` | 任意 | embedding | 强制向量（失败仍回退 jieba） |
| `jieba` | 任意 | jieba | 强制关键词，与 v1 打分一致 |

判定见 `SkillMatcher.use_embedding()`。默认构造（`SkillService` 惰性用全局 `get_settings()`）时，
仓库默认 `embedding_api_key=""` + `provider=openai` → 落在最后一行，**现有中文匹配测试原样跑 jieba 路径**。

jieba 路径的打分与 v1 逐字节相同：`slug` 命中 +10、`name` 词元重叠 ×2、`description` 词元重叠 ×1，
`score>0` 才入选，按分降序取 `top_k`（`SkillMatcher._jieba_match`）。
embedding 路径：把每个候选技能的 `name + description` 向量与查询向量算余弦，**纯 top-k 不设阈值**。

## ② 实现原理：缓存 / 失效 / 回退链

**向量缓存（惰性 + 按 slug 失效）**：`SkillMatcher._vector_cache: dict[slug, vector]`。
`_embedding_match` 遍历候选，未命中缓存的才 `embed(name+description)` 并写入 —— 冷启动一次性补齐，
之后同一批技能只需 embed"查询"这一条（查询是变化的，不缓存）。测试 `test_vector_cache_reuses_skill_embeddings`
断言第二次匹配的 embed 调用数只 +1（仅新查询）。

**失效钩子**：技能的 `name/description` 变了，缓存里的旧向量就是脏的。`SkillService` 在
`create_skill / import_skill_dir / update_skill / delete_skill` 成功后调 `_invalidate_match_cache(slug)`，
内部 `matcher.invalidate(slug)` 精确 pop 对应条目，下次匹配按新文本重算（`test_update_skill_invalidates_cache`
验证：更新 alpha 后只有 alpha 被重算，beta 仍命中缓存）。matcher 未构建时无缓存可失效，直接跳过，
不为一次删除白白拉起 matcher。

**回退链**：`match()` 若判定走 embedding，则 `try` 向量召回，任何一次 embed 抛错（网络/配额/维度）
→ `logger.warning` + 本次 `return self._jieba_match(...)`。关键点是**不缓存失败状态**：
`_vector_cache[slug] = await self._embed(text)` 先算后写，抛错则该 slug 不落缓存，
已成功算出的其它技能向量保留，下次请求对失败者重试（`test_embedding_exception_falls_back_to_jieba`
断言回退命中 + 缓存为空 + 有 warning）。

**embed 来源可注入**：`SkillMatcher(embed_fn=...)` 注入单文本 `async (str)->vector`；缺省首次使用时
惰性从 `app/rag/embeddings.py::EmbeddingService.embed_text` 取（复用现成向量化能力，**无新依赖**）。
离线测试注入确定性假函数（按关键词构造近似正交向量），完全不碰真实 embedding API。
余弦用纯 Python 实现（`matching.py::cosine_similarity`，零向量/维度不一致降级返回 0.0，不引 numpy）。

## ③ 参考对照

**Yuxi：没有语义匹配这一层。** 它靠渐进式披露让模型自己在技能清单里挑（清单只有 名称+描述，
模型判断后自行 `read_skill`）。既然模型能自选，我们为什么还要这层？—— 因为 `auto_match` 模式解决的是
**另一个问题**：技能规模上来后，把"全部技能的名称+描述"塞进 system prompt 的披露成本随技能数线性增长；
先用一次廉价召回把候选压到 `top_k`，披露列表就被封顶，token 更省。所以这是 **相对 Yuxi 的真实增量**
（skills-optimization.md §2.7、interview-guide.md 均记为主动创新），但定位是"披露前的可选预筛"，
不是替代模型的自选——默认关闭 `auto_match` 时行为与 Yuxi 一致。

**SQLBot：datasource / table 的 embedding 召回**，是本层的直接设计来源：

- 召回主体 `backend/apps/datasource/embedding/ds_embedding.py::get_ds_embedding`：
  `embed_documents(每个库/表的 schema 文本)` + `embed_query(question)` → 逐个 `cosine_similarity`
  → `sort(reverse=True)` → 取 `top-N`（`settings.DS_EMBEDDING_COUNT`）。表级同构于
  `table_embedding.py::get_table_embedding`。我们的 `_embedding_match` 就是这套"文档向量 + 查询向量 + 余弦排序"。
- 余弦 `backend/apps/datasource/embedding/utils.py::cosine_similarity`：纯 Python `math`，
  本项目 `matching.py::cosine_similarity` 与之等价（含零向量/维度校验）。
- 阈值 `backend/common/core/config.py::EMBEDDING_DEFAULT_SIMILARITY=0.4`：SQLBot 会砍掉低于阈值的候选；
  我们候选数少、上层 `top_k` 已封顶噪声，暂不设阈值（见 ④）。

## ④ 区别与取舍

- **内存缓存，不建向量库。** SQLBot 把每张表/库的向量**持久化进 DB 列**
  （`alembic/versions/047_table_embedding.py` 给 `core_table/core_datasource` 加 `embedding` Text 列），
  启动时后台线程 `fill_empty_table_and_ds_embeddings` 回填。那是为"多数据源 × 大量表 × 跨请求复用"设计的。
  本项目技能数 <100、进程内单例，`dict[slug]→vector` 惰性缓存足矣 —— 上 Milvus/建索引/写 DBschema
  是杀鸡用牛刀，还平白引入一致性与运维成本。真到技能上千再换 `app/rag` 的 `vector_store.py`。
- **只向量化 `name + description`，不含正文。** 渐进式披露的前提就是"正文按需读"；匹配阶段模型还没决定读谁，
  正文既拿不到也不该拿（拉全文向量化既慢又稀释语义信号）。名称+描述正是披露清单里给模型看的那一行，
  拿它做召回口径与披露口径天然对齐。
- **失败回退而非显式报错。** 匹配是"披露前的增强预筛"，不是关键路径：embedding 挂了，回退 jieba 仍能给出
  合理候选，最坏也就是召回质量降级；若改成抛错，会把一次可选优化变成阻断 `auto_match` 的硬故障。
  对齐本项目一贯口径 —— `execute_sql` 返回带候选的报错让模型自纠、MCP stdio 超时降级返回 `[]`，
  都是"增强路径失败即降级、不炸主流程"。
