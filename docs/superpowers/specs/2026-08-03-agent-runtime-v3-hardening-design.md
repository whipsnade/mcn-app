# Agent Runtime v3 修复加固设计

状态：已确认，待实施计划

日期：2026-08-03

适用分支：`codex/agent-runtime`

关联设计：`2026-08-02-model-led-agent-runtime-design.md`

## 一、背景与结论

`codex/agent-runtime` 已完成统一 Agent 运行时的主要数据模型、Run 状态机、
Reviewer、Artifact 版本、lineage、BI 与前端 API 切换，但再次对照代码、原设计、
真实 UAT 记录和两份代码审核报告后，确认当前分支仍命中多项发布阻断条件，不能合并
或执行一次性切换。

本设计不推翻“模型主导 + 可信执行内核”的方向，也不重新实现整个分支。修复采用
两道发布闸门：先保证执行安全、计费一致和恢复正确，再提高正式 Artifact 的稳定
交付能力并完成真实 UAT。两道闸门都通过前，分支不得合并到主分支。

## 二、审核结论校正

### 2.1 MCP durable-before-send 实际未成立

`AgentEventStream.append("tool.started")` 会提交 Step，但积分预留和
`agent_tool_calls.running` 发生在该提交之后。`_db_transaction()` 在已有外层事务中
只提交 savepoint，不提交外层事务，因此外发后、结果返回前进程崩溃时，调用行和积分
预留仍可能整体回滚。恢复后模型可能用新 Step 再次发起同一调用。

修复必须同时覆盖：独立持久事务、悬挂调用扫描、Run transcript 重建和不确定结果禁止
重放。仅补一处 `commit()` 不能闭环。

### 2.2 真实 UAT 不是全量通过

现有 UAT 仅验证了澄清、部分故障分类、余额不足和一个受控 Reviewer 场景。品牌、活动、
Top20 圈选场景均因 DataTap 长调用挂起，父 Artifact 钻取未执行，KOL 详情只验证了缓存
命中。因此发布材料统一使用以下表述：

> 已执行部分真实模型 + 真实 DataTap UAT；部分运行时机制通过，但核心业务闭环仍被
> 长调用和正式 Artifact 生成可靠性阻断。

### 2.3 Artifact 已读状态采用新表

`artifact_read_states.session_id` 仍外键到旧 `sessions`，诊断成立；但直接把旧表外键
改向 `agent_sessions` 会破坏旧应用版本回滚。新增 `agent_artifact_read_states`，旧表和旧
外键保持不变。新 Agent API 只读写新表。

### 2.4 Artifact 强类型校验提升为发布阻断项

Reviewer 当前只把 JSON Schema 提供给模型，发布事务没有调用 schema 对应的 Pydantic
类型校验，冻结后的 lineage 闭包也没有写入 Version。该问题直接影响 BI、Excel 和审计，
从 Major 提升为 Blocker。

### 2.5 data_status 反向聚合约束缺失

当前契约验证了 `complete` 的正向条件，却没有保证 `restricted` 必须对应至少一个受限的
必需章节，也没有保证所有必需章节都存在于 `availability`。发布前强类型校验落地时必须
一并修正，否则仍可发布状态与章节不一致的 payload。

## 三、设计边界

### 3.1 模型负责的决策

模型继续决定：

- 是否澄清以及询问什么；
- 分析目标和钻取方向；
- 选择哪些已审核 MCP/历史/计算/Builder 工具；
- 查询参数、查询顺序和失败后的替代路径；
- 使用哪些 Evidence、生成哪些 Artifact；
- 叙事、结论和建议；
- 何时提交 Reviewer 或结束当前 Run。

代码不得重新引入 Brainstorm、GoalPlanner、固定 Goal 类型流水线、固定 MCP 调用顺序或
按关键词硬编码业务意图。

### 3.2 可信执行内核负责的约束

代码负责：

- 用户、Session、渠道和工具审核权限；
- Run、Attempt、Step、租约、取消和恢复；
- MCP 参数 Schema、幂等、计费、熔断和结果分类；
- Evidence 不可变存储和字段级 lineage；
- 确定性计算、评分和结构归一；
- Artifact payload 强类型校验、Reviewer 和原子发布；
- BI、Excel、缓存和 SSE 表现层。

### 3.3 确定性 Builder 的边界

正式 Artifact 必须通过受信任 Builder 工具生成。模型向 Builder 提供用户确认的 scope、
Evidence ID、需要的叙事内容和显式配置；Builder 负责读取这些 Evidence、执行确定性聚合、
生成字段级 lineage、构造并校验强类型 payload。

Builder 不得主动选择 MCP 工具、发起外部查询、改变用户目标或决定业务执行顺序。模型仍然
控制“查什么、用什么证据、构建什么产物、何时构建”。开放式钻取可生成
`insight_board_v1`，但也必须经过统一 payload/lineage 发布边界。

## 四、总体修复架构

修复保留现有 Session、Run、Step、Evidence、Draft、Reviewer 和 Version 模型，新增或重构
以下边界组件：

1. `AgentToolRegistryFactory`：生产工具装配和用户能力过滤的唯一入口。
2. `DurableToolCallCoordinator`：外发前持久化、外发后结算和恢复核对的事务边界。
3. `RunTranscriptLoader`：从完整持久 Step 和 ToolCall 恢复模型上下文。
4. `RunLeaseHeartbeat`：长模型、MCP、Reviewer 调用期间的独立租约续期。
5. `ArtifactPayloadValidator`：Draft 和 Version 的强类型、模块映射和状态聚合边界。
6. `ArtifactLineageFreezer`：发布时重算并固化传递闭包。
7. `ArtifactBuilderTool` 集合：Evidence 到正式 Artifact 的确定性转换。
8. `AgentEventThinkingSink`：把主 Agent 的真实 thinking 写入新 Run SSE。

## 五、Gate A：可信运行时加固

### 5.1 生产工具装配与权限

新增单一工厂构建每个 Engine 的 Tool Registry，生产必须注册：

- history：`read_artifact`、`search_evidence`、`read_tool_result`；
- calculation：`calculate_expression`、`aggregate_metrics`、
  `calculate_period_comparison`、`normalize_sentiment`、`rank_kols`；
- artifact：`create_draft`、`update_draft`；
- 目录中当前仍 approved、enabled 且签名未变化的 MCP 工具。

Engine 创建时按 `user_id` 查询 `user_channel_permissions` 并注入；默认空权限只能隐藏受限
工具，不能作为生产用户的永久配置。工具执行前仍需实时复核目录行状态，避免 Engine 创建后
工具被隔离却继续调用。

`kol_detail_v1` 使用明确的达人详情/热帖 MCP allowlist，而不是放开整个 `MCP_TOOLS` 分类；
同时保留 Artifact Builder/发布能力。该 Profile 不允许 `ask_user`。

### 5.2 Agent 已读状态迁移

新增 Alembic 迁移 `0028_agent_artifact_read_states`：

- 新建 `agent_artifact_read_states`；
- `session_id` 外键到 `agent_sessions.id`；
- 唯一键为 `(user_id, session_id, module)`；
- 字段包含 `last_seen_sequence`、`updated_at`；
- 不迁移旧水位，不修改、不删除 `artifact_read_states`；
- downgrade 只删除新表。

新 Agent Artifact 服务和路由切换到新 ORM；旧表仅供旧应用版本回滚使用。

### 5.3 MCP 外发、计费与故障分类

`DurableToolCallCoordinator.prepare()` 使用独立数据库 Session 和单一事务：

1. 校验 Run/Step 归属；
2. 计算并锁定 `logical_call_id`；
3. 插入或复用 `agent_tool_calls`；
4. 预留固定 10 积分；
5. 写 `running` 和 `started_at`；
6. 提交后才允许调用 transport。

外部调用完成后分别以独立事务执行：

- 成功：输出 Schema 校验、写 Evidence、settle 10 分；
- `failed_confirmed`：release；
- `definitely_not_sent`：release；
- `result_unknown`：保留预留，写 unknown，进入核对；
- 进程取消且请求可能已发送：按 `result_unknown` 收口。

Agent 路径关闭 DataTap 旧服务级熔断 `circuit_scope="service"`，使用一个进程共享的
`FineGrainedCircuitBreaker`，key 固定为 service + internal_tool_name + normalized arguments。
504、5xx、协议中断和 PossiblySentTimeout 都属于可能已发送，Agent 路径禁止自动重试；只有
明确的连接前失败可由模型决定是否重新尝试。

管理员核对器在应用启动时写入 `app.state.agent_tool_reconciler`。恢复或人工取回的 payload
必须重新执行输出 Schema 校验后才能写 Evidence。

### 5.4 恢复与 transcript

恢复扫描包含：

- `unknown`；
- 超过受控时间仍处于 `running/reserved` 的调用；
- 租约过期的 `running` Run；
- 租约过期的 `reviewing` Run。

超时 `running/reserved` 先迁移为 unknown 并核对，绝不直接释放或重新外发。

`RunTranscriptLoader` 从触发消息和完整 Step 重建本 Run 上下文。每个已完成工具 Step 必须回放
其持久结果；settled 结果使用原 Evidence，failed/unknown 使用原结构化结果。恢复从最后一个
完整 Step 的下一 sequence 开始，沿用原 Step 对应的 `logical_call_id`，不依赖模型记忆防重。

### 5.5 取消、租约和 reviewing 恢复

取消规则：

- queued/paused/clarification：API 可立即迁移 cancelled 并写终态事件；
- running/reviewing：API 只写 `cancel_requested`，Engine 在外发前、模型返回后、Reviewer
  返回后和每轮顶部检查并收口；
- 已在执行的 MCP 若无法确认是否发送，取消后按 result_unknown；
- 每个 cancelled Run 恰好一个 `run.cancelled` 终态事件。

`RunLeaseHeartbeat` 使用独立 DB Session，每 `lease_seconds / 3` 续租一次，覆盖模型、MCP 和
Reviewer 长调用。心跳确认租约已被其他 worker 接管时，旧 worker不得再发布 Artifact 或写
Run 终态。

reviewing 恢复读取既有 Review Batch、Item、Attempt 和当前 Draft Revision：已 approve 的
Item 不重审，pending/revise 继续，完成后原子发布。Reviewer 调用和发布操作都必须幂等。

`resume` 必须锁 Session 并检查是否已有其他 queued/running/reviewing 的
`session_analyst_v1` Run；存在则返回 409。同一 Session 任意时刻最多一个活动主分析 Run。

### 5.6 Artifact 强类型与 lineage 发布边界

新增 `ArtifactPayloadValidator`，schema_version 必须映射到唯一 Pydantic 类型，并校验：

- schema_version、module、artifact_type 的固定组合；
- Artifact key 所需 business fields；
- `extra="forbid"`、URL scheme、Top20、评分权重等类型约束；
- required availability key 完整；
- `complete` 当且仅当全部必需章节 complete；
- `restricted` 当且仅当至少一个必需章节 partial/unavailable，并具有覆盖限制；
- 缺失数据保持 null，不得自动变为 0。

create/update Draft 时执行一次校验并保存标准化 `model_dump(mode="json")`；publish 事务内锁定
Revision 后再次校验，防止旧 Draft 或旁路写入绕过。

发布事务调用 `validate_and_freeze_lineage()`，把返回的 Evidence 传递闭包写入 Version 的新字段
`lineage_snapshot_json`。原始 `evidence_refs_json` 继续保留，分别承担“模型直接引用”和“发布时
冻结审计快照”职责。该字段通过同一 0028 迁移增加，旧 Version 为 NULL。

Excel 导出对历史 NULL 或非法 payload 返回稳定 409，不允许 Pydantic ValidationError 泄漏为
500。

### 5.7 Draft 与 Review Batch 生命周期

第一次 `submit_review` 创建 Batch 后冻结 Draft ID 集合和 completion_text。后续提交必须与
原集合一致；新增、遗漏或替换 Draft 返回结构化 `review_batch_draft_set_mismatch`，由模型决定
修订现有 Draft 或结束 Run。

以下所有非发布出口都释放当前 Run 持有的 Draft owner，但保留不可变 Revision：

- ask_user；
- complete；
- paused；
- cancelled；
- failed；
- Reviewer reject/abort。

新 Run 可认领空闲 working head 并基于历史 Revision 继续；不得因旧 owner 已终态而永久
`artifact_busy`。

### 5.8 Profile 动作与事件

Engine 在 dispatch 前校验 `action.action in profile.allowed_actions`。不允许的动作作为结构化
validation error 回喂；达到统一无效动作上限才失败。

模型适配器只负责单次 JSON 修复；修复仍失败时把可恢复的非法输出结果交给 Engine 计数，不直接
把 Run 失败。供应商错误、鉴权错误和不可恢复协议错误仍按系统错误失败。

新增 `AgentEventThinkingSink`，只给用户可见的主 Run/KOL Detail Run 注入；Reviewer、Utility 不
发送 thinking。只有供应商真实返回 think 时才产生 thinking 事件，完成后前端折叠展示。

事件顺序固定为：thinking/tool/review/artifact → assistant message → `message.completed` →
`run.completed|failed|cancelled`。Run 终态事件必须是该 Run 最后一条用户可见 SSE 事件。

## 六、Gate B：Artifact 交付与产品闭环

### 6.1 Builder 工具

新增并注册：

- `build_brand_report_draft`；
- `build_campaign_report_draft`；
- `build_kol_selection_draft`；
- `build_kol_analysis_draft`；
- `build_kol_detail_draft`。

每个 Builder 输入必须包含 scope、Evidence ID 列表、模型提供的叙事字段和必要配置；输出为
`artifact_id/draft_id/revision_id/schema_version`，不把完整 payload 再回灌模型上下文。

品牌与活动 Builder 复用已确认的指标/比较期口径；KOL Selection Builder 复用
`kol_score_v2` 唯一评分源、缺失为 0、互动量默认排序和 Top20；KOL Analysis/Detail Builder
复用父 Artifact Version 与缓存契约。Builder 生成的全部业务数值都有 Evidence 或确定性计算
工具 lineage。

保留底层 `create_draft/update_draft` 供 `insight_board_v1` 和受控修订，但正式五类业务 Artifact
的 prompt 必须优先使用 Builder；发布边界仍对所有 Draft 一视同仁校验。

### 6.2 Prompt 与工具描述

Profile prompt 注入：

- 当前用户消息与有限会话 Memory；
- 可见工具及完整输入 Schema；
- Artifact 类型的紧凑用途说明；
- Builder 的输入/输出契约；
- Evidence ID 和字段读取方法；
- 1 至 2 个去敏成功示例。

Prompt 不规定固定业务阶段或固定工具顺序。模型看到 schema 但不手写整份大型正式 payload；
Builder 承担结构组装，从而减少反复 revision。

### 6.3 Payload null 治理

废除易漏字段的 `SECTION_NUMERIC_PATHS` 手工枚举，改为 schema 元数据驱动的递归检查：每个业务
章节声明其根路径，校验器遍历该章节下所有 Optional 数值叶子。数组中的 daily trend、topics、
regions、top posts、KOL items 等必须同样覆盖。

`SentimentBucket.count/share` 改为可空；情感 unavailable 时允许 null + limitation，不得伪造
0。真实零值只能在 Evidence 明确返回 0 时写入，并需要 lineage。

### 6.4 Utility 与前端契约

Utility 接线时机：

- Session 首条用户消息提交后 best-effort 生成标题；
- 用户 Run 终态后 best-effort 生成 run_summary 和 suggestions；
- Utility 失败不改变父 Run 状态。

前端只消费 `/api/v1/agent/runs/{id}/events`，删除旧 Session thinking hook、API 和路由注册。
会话详情中的 runs 按 `created_at`、Attempt/创建顺序和 id 稳定排序；前端不再以随机 UUID 顺序
推断最新 Run。

KOL 详情请求中的 selection artifact/version 必须校验归属并进入 prompt、Artifact parent 和
lineage 上下文。前端仍进行 http/https URL 白名单检查，后端强类型 URL 校验作为最终边界。

旧前端 API、hook、组件只有在 `rg` 和 TypeScript import graph 证明无生产引用后删除；旧数据库
表不在本次修复中物理删除。

## 七、错误处理

对用户暴露稳定错误码：

- `active_run_in_progress`：同 Session 已有活动主 Run；
- `run_cancel_requested`：Run 已请求取消，不可恢复；
- `artifact_payload_invalid`：Draft/publish 强类型失败；
- `artifact_lineage_invalid`：lineage 失败；
- `review_batch_draft_set_mismatch`：复用 Batch 时 Draft 集合变化；
- `artifact_busy`：另一个活动 Run 确实持有 working head；
- `ARTIFACT_EXPORT_UNSUPPORTED`：类型/版本不支持导出；
- `result_unknown`：外部调用结果未确认，保持预留等待核对。

跨用户、跨 Session 的 Session、Run、Evidence、Artifact、KOL selection reference 一律返回 404，
不泄漏资源存在性。内部异常日志不得包含密钥、Authorization、完整原始 MCP payload 或完整模型
thinking。

## 八、测试设计

### 8.1 单元测试

- Registry 生产装配、Profile、渠道权限和动态隔离；
- MCP prepare/finalize 四类结果与账本幂等；
- 504/5xx/possibly-sent 不重试；
- transcript 重建和原 Step logical_call_id 复用；
- payload/module/type/status/null/URL/评分校验；
- frozen lineage 闭包写入 Version；
- Batch 集合冻结和所有 Draft 释放出口；
- Profile allowed_actions 和 Engine 非法输出三次容错；
- thinking 只从真实 delta 产生，终态事件最后发送。

### 8.2 迁移与集成测试

- 空库从 0027 升级 0028、downgrade 0028；
- 旧 `artifact_read_states` 数据和外键保持不变；
- 新 Agent Session 已读写入成功；
- 在“外发前”“外发后返回前”“settle 前”“reviewing 中”“publish 前”注入崩溃；
- 恢复后不重复外发、不重复扣分、不错误释放 unknown；
- 取消与 decide/dispatch/MCP/Reviewer 各竞态；
- 两 worker 租约接管与旧 worker 禁止发布；
- Session 并发 message/resume 的单活动 Run 约束；
- 每个正式 Artifact 从 Builder 到 Reviewer、Version、BI、Excel 的闭环。

### 8.3 前端测试

- 只建立新 Run SSE，不请求旧 Session thinking；
- thinking 实时展开、完成折叠、刷新重放；
- Run 历史按服务端顺序锚定到对应用户消息；
- 未读水位按 user/session/module 隔离；
- KOL selection reference 透传；
- URL scheme 白名单；
- 三个 BI Tab、达人两个子 Tab 和历史版本不回归。

### 8.4 真实 UAT

使用真实配置模型与真实 DataTap，至少覆盖：

1. 信息不足澄清；
2. 品牌分析和比较期；
3. 活动分析；
4. 多平台互动量 Top20 圈选及 `kol_score_v2`；
5. KOL 分析；
6. KOL 详情、主页和最新 5 条热帖；
7. 基于既有 Artifact 的继续钻取；
8. 504/长调用后继续其他分析；
9. 取消、进程重启和恢复；
10. 余额不足与 unknown 人工核对；
11. 跨用户访问拒绝；
12. 品牌与圈选 Excel 导出。

每个核心业务场景至少独立运行 3 次。UAT 断言业务目标、Artifact 契约、Evidence、lineage、
Run 状态和账本，不断言固定工具顺序。所有真实 MCP 调用记录积分前后、调用状态和
logical_call_id；未完成或 HANG 不得标 PASS。

## 九、实施计划拆分

本设计拆成两个顺序实施计划：

### Plan A：可信运行时加固

覆盖迁移、工具装配、MCP durable-before-send、熔断与核对、恢复 transcript、取消与租约、
reviewing 恢复、Artifact 强类型/lineage、Draft/Batch 生命周期、Profile 和 SSE。Plan A 完成后
系统应在 Fake/受控 Provider 下满足账本、恢复和发布不变量，但尚不宣称真实业务 UAT 通过。

### Plan B：Artifact 交付与切换

依赖 Plan A，覆盖 schema-driven null 治理、五类 Builder、Prompt、Utility、前端契约清理、
真实模型/DataTap UAT 和 cutover 文档。Plan B 和真实 UAT 全部通过后才允许合并。

## 十、发布闸门

Gate A 必须全部满足：

- 外发前调用行和积分预留已独立提交；
- unknown 不自动重放或释放；
- 恢复不重复外发/扣费；
- 租约、取消、reviewing 可安全恢复；
- 生产工具、权限、已读状态可用；
- 所有 Version 通过强类型并保存 frozen lineage；
- 单元、迁移、崩溃注入和集成测试通过。

Gate B 必须全部满足：

- 五类正式 Artifact 能由真实模型通过 Builder 稳定交付；
- BI 与声明支持的 Excel 消费同一强类型 Version；
- thinking、Run 历史、未读和 KOL 详情契约正确；
- 真实 UAT 全场景完成，核心场景至少连续 3 次通过；
- 每笔 MCP 计费和 unknown 预留可审计；
- cutover 清单不再包含未解决 blocker。

任一闸门失败，保持 `codex/agent-runtime` 未合并状态。首次切换仍不物理删除旧表；旧表清理
必须在稳定运行后另立设计、备份并再次批准。
