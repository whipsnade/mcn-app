# GoalPlanner 对话式意图（respond）设计

日期：2026-07-27
状态：已确认（用户评审通过）
前置文档：`2026-07-23-multi-intent-task-artifacts-design.md`（Goal/Artifact 体系）、`changelog/2026-07-24.md`（阶段三 enforce）

## 背景与目标

画像 ready 后，会话内所有消息都走 `POST /sessions/{id}/tasks` 的 GoalPlanner enforce 路径。当前 planner 只理解两类输出：`clarify`（参数澄清）与 `execute`（brand/campaign/kol 三种分析目标）。实际使用中，用户消息里有大量**不需要执行新分析**的内容：

1. 针对会话已有内容提问——失败原因追问（"为什么失败了"）、结果解释（"为什么圈选这个达人"）、已有结论的总结/对比/复述；
2. 询问使用方法与案例；
3. 与 KOL/品牌/活动/营销分析无关的问题。

这些消息现在会被硬塞进 execute/clarify 语义：要么误建任务扣积分，要么得到答非所问的澄清。目标是在 GoalPlanner 中新增**对话式意图**，让 planner 识别后由后端**同步直接回复**：不建任务、不调 MCP、零积分。

### 核心设计决策（已与用户确认）

- 三个场景都是对话式回复，**不进入 Goal/Task 执行体系**（不新增 goal_type、不动 executor/artifact/selection 链路）。
- 失败原因与结果解释合并为一个意图 `context_qa`：边界不是"是否有失败任务"，而是"问题能否用会话已有内容回答"。
- `context_qa` 的回答由一次零积分模型调用生成，直接输出模型回复；`usage_help` 用仓库内维护的静态文案；`out_of_scope` 用固定拒答文案。
- 仅 enforce 路径生效；brainstorm 澄清阶段不扩展。

## 现状

- `GoalPlannerOutput`（`backend/app/goals/schemas.py`）：`action: Literal["clarify","execute"]`，model_validator 强制 clarify 有 question 无 goals、execute 反之。
- enforce 路由（`backend/app/tasks/router.py`）：planner 输出 clarify → 落 user+assistant 消息、不建任务、返回 `TaskOutcomeClarify`；execute → 落 1-3 个 goal 建任务；planner 异常回退 kol_selection 单 goal。
- 上下文构建（`backend/app/goals/context.py`）：最近 20 条消息压缩 + `session_context`（active_brand/campaign_name/category/platforms/target_audience/brainstorm_profile）+ `account_default_brand` + `artifact_summaries`（各模块最新 completed artifact 的**元数据**投影，无正文）。
- 任务失败信息已落库：`analysis_tasks.error_code/error_message`（白名单安全文案）、goal 行 `error_code`、assistant 错误消息（`metadata.message_type="error"`）。
- 圈选名单已有 6 维加权评分与 rating（`selection/scoring.py`），存在 `kol_selection_items`。

## 设计

### 1. planner 契约扩展（`goals/schemas.py`）

```python
action: Literal["clarify", "execute", "respond"]
respond_type: Literal["context_qa", "usage_help", "out_of_scope"] | None
```

model_validator 规则：

- `respond`：必须有 `respond_type`；`goals` 与 `question` 必须为空。
- `clarify` / `execute`：`respond_type` 必须为空（其余规则不变）。

### 2. planner prompt 意图规则（`model/prompts.py` 的 `GOAL_PLANNER_SYSTEM_TEXT`）

新增段落描述三类 respond 意图与判定优先级：

- **优先级**：可执行分析需求 > 上下文答疑 > 使用帮助 > 无关拒答。一条消息同时含新分析需求与提问时，按 execute 处理。
- **`context_qa`**：用户针对会话已有内容提问，且答案不需要采集新数据。覆盖失败/部分失败原因追问、结果解释（圈选依据、报告结论依据）、已有内容的总结/对比/复述。
- **`usage_help`**：用户询问产品怎么使用、能做什么、要示例案例。
- **`out_of_scope`**：问题与 KOL/品牌/活动/营销分析/本会话历史无关。
- **边界规则**：纯解释/归因/总结已有内容 → `context_qa`；要求新数据或新结论（继续钻取，如"再查一下这个达人的粉丝画像"、"把名单扩大到 50 个"）→ `execute`（缺参数时 `clarify`）；问题指向会话中不存在的内容且不需要新数据（如问一个没做过的分析的结论）→ `clarify` 引导；**拿不准一律 `clarify`，不误拒、不误答**。
- 顺带删除"当前只执行影子规划"的遗留文案（enforce 下已与事实不符）。

### 3. 上下文证据包

两处补充：

- **planner 输入**（`goals/context.py` 的 `session_context`）：新增 `recent_task_outcomes`——本会话最近 3 个任务的 `{status, error_code, error_message, completed_at}`，供 planner 判断失败追问是否有事实依据。
- **respond 处理时**（路由层组装的答疑证据包，作为 `context_qa` 模型调用的 user 输入）：
  - 最近消息压缩（复用 `compress_messages`）；
  - `recent_task_outcomes`（同上）；
  - 最新圈选名单投影：最新 selection set 的 top 20，字段 platform/nickname/rating/6 维评分要点；
  - 最新报告正文投影：kol_analysis/brand_analysis/campaign_analysis 各取最新版，正文各截断约 4000 字符；
  - 证据包总量上限约 12000 字符，超出按「任务结果 > 名单 > 报告」优先级裁剪。

### 4. enforce 路由同步处理（`tasks/router.py`）

`action="respond"` 时复用 clarify 的同步路径：落 user+assistant 消息、bind_turn、思考块持久化、commit、**不建任务、零积分**。响应 union `TaskCreateResult` 新增：

```python
TaskOutcomeRespond { outcome: "respond", respond_type: str, message: MessageRead }
```

路由 `status_code=202`，respond 三分支（含降级文案）均返回 202。幂等键行为与 clarify 现状一致：命中幂等键时跳过 planner，但不落幂等记录，重复提交会重复落消息——本设计不新增幂等记录。

三个分支：

- **`context_qa`**：组装第 3 节证据包 → 一次零积分模型调用（新 prompt `context_qa_v1`，`purpose="context_qa"`，文本输出走 `stream_text` 出口以复用 prompt 日志）。prompt 约束：只能基于证据包回答；证据不足时明说"当前会话中没有相关信息"；不得编造达人、数据或结论。模型调用失败 → 降级为固定文案（"暂时无法回答，请稍后重试。"），仍返回 202。assistant 消息 `metadata.respond = {"type": "context_qa"}`。
- **`usage_help`**：仓库内维护的静态中文使用指南 + 案例（新模块 `app/workspace/usage_guide.py` 常量），直接落消息，零模型调用。
- **`out_of_scope`**：固定拒答文案（说明本系统是营销分析助手，仅支持 KOL/品牌/活动相关分析与历史会话内容问答），零模型调用。

`context_qa` 的模型调用不创建用户可见 thinking operation（purpose 白名单外），与 goal_summary/followup 等后台调用一致。

### 5. validation 与 shadow

- `goals/validation.py` 的 `validate_goal_plan` **必须新增 `action == "respond"` 的 early-return 分支**（与 clarify 同样跳过 goal 序列/依赖/证据/品牌解析校验）。现状只早退 clarify，其余 action 一律落入 execute 校验并执行 `_validate_brand_resolution`——respond 输出在会话已有 active_brand 时会误触 `brand_source_context_mismatch`，经语义重试后抛错回退误建 kol_selection 任务，respond 将不可用。互斥规则（respond 无 goals/question）由 schema validator 保证，无需重复校验。
- shadow planner：输出只写 `model_prompt_logs`，新 action 被 schema 接受即可，无需其他改动；观测上可统计 respond 各类型的分布。

### 6. 前端

- `src/api/contracts.ts`：`TaskCreateResult` union 新增 `TaskOutcomeRespond`（形状与 clarify 一致，多 `respond_type`）。
- `src/hooks/useWorkspace.ts` 与 `src/api/tasks.ts`：`respond` 复用 clarify 的消息落库处理（把返回的 message 落为 assistant 消息），无新 UI 组件。

## 降级与失败语义

| 环节 | 失败表现 | 处理 |
| --- | --- | --- |
| planner 调用/校验失败 | 抛异常 | 沿用现状：回退 kol_selection 单 goal，不阻塞用户 |
| `context_qa` 模型调用失败 | ModelAdapterError | 降级固定文案落消息，返回 202 |
| 思考块持久化失败 | 异常 | 沿用现状：只记 warning |
| 证据包组装失败（DB 查询异常） | 异常 | 只记 warning，证据包降级为仅最近消息 |

## 不做的事（YAGNI）

- 不新增 goal_type、不建任务、不动 executor/artifact/selection/quick 链路。
- brainstorm 澄清阶段不扩展（后续需要再单独立项）。
- 不做 respond 的 SSE 流式输出（同步响应，与 clarify 一致）。
- 不做多轮追问的特化上下文（复用最近消息压缩即可）。

## 测试策略

- schema/validation：respond 三类型合法性、与 goals/question 互斥、clarify/execute 带 respond_type 被拒绝。
- enforce 路由：
  - `context_qa`：落 assistant 消息（模型回复原文）、不建任务、零积分、证据包包含名单/报告/任务结果；
  - `context_qa` 模型失败 → 降级文案仍 200；
  - `usage_help` / `out_of_scope`：落静态文案消息、零模型调用（断言适配器未被调用）；
  - 幂等键命中跳过 planner 的现状不回归。
- planner 语义（prompt 层用假模型输出驱动）：无失败任务时模型输出 context_qa 也能正常回答（证据包无任务结果，模型应按 prompt 说明信息不足）。
- 前端：`respond` outcome 消息正确落会话；union 类型收窄。

## 遗留事项

- `usage_help` 静态文案内容需单独撰写评审（功能清单、积分规则、圈选/报告/快捷功能案例）。
- respond 意图的线上误判率需用 `model_prompt_logs`（purpose=goal_planner）观测迭代 prompt。
