# 阶段三：品牌与活动分析实施计划

设计依据：`docs/superpowers/specs/2026-07-23-multi-intent-task-artifacts-design.md` §19 阶段三。
已确认决策：**先提交阶段二**；**GoalPlanner 从影子转为正式接管（单 Goal）**；**前后端一次做完**。

## 关键事实（已核实）

- 后端接缝：`POST /sessions/{id}/tasks` 在 `backend/app/tasks/router.py:128`；系统 prompt 硬编码在 `dependencies.py:649`（AGENT_LOOP_PROMPT.system）；export_contract 注入在 `dependencies.py:623`；kol 专属收尾在 `_TaskArtifacts.auto_kol_analysis`/`finalize_goal`（dependencies.py:147-453）。
- `REPORT_WRITER_PROMPT` 已无运行时调用方，不复活，新写品牌/活动专用 prompt（参照 `KOL_ANALYSIS_PROMPT` 的固定区块风格）。ReportBlock 7 种块类型不变。
- 前端：UniversalReport（655 行）内部双 Tab；SSE 事件唯一分发处在 `src/state/taskEvents.ts`（`artifact.updated` 目前走 default 忽略）；clarify 选项 chips 可复用 brainstorm chips 交互（点击填入输入框）。
- 画像 ready 判断在前端（`useWorkspace.ts:322`），后端 tasks 路由无此判断。

## 总体策略

- **单 Goal 接管**：每条 ready 后的消息先经 GoalPlanner 规划；`execute` 时只执行 sequence=1 的 Goal（>1 个时丢弃其余记 warning，复合编排属阶段四）；`clarify` 不建任务，落一条 assistant 澄清消息（options 存消息 metadata）。
- **灰度开关**：新增 `GOAL_PLANNER_ENFORCE_ENABLED`（默认 false，UAT/开发显式开）。关闭时行为与阶段二完全一致（任务固定 kol_selection）。Planner 调用/校验失败时回退 kol_selection 单 Goal 并记 warning（可用性优先，不阻塞用户）。
- **GoalPolicy 下沉**：kol 专属的 export_contract 注入/沉淀/自动分析从 executor 硬编码改为按 goal.goal_type 分派；品牌/活动 Policy 不注入契约、不沉淀、不生成 KOL 报告。
- 阶段二行为兜底：开关关闭 + 全部旧测试必须零改动通过。

---

## Step 0：提交阶段二

两个 commit：① 迁移 0022/0023 + 模型 + 服务层（selection/reporting/artifacts/goals/identity/mcp）；② executor 包装 + 端点切换 + 文档（AGENTS/changelog/计划文档）。

## Task 1：GoalPlanner 正式化（enforce 模式）

- `goals/context.py`：新增 `GoalPlannerContextBuilder.build_for_message(user_id, session_id, message)`（不依赖 task_id；recent_messages 取会话最近 N 条 + 当前消息；artifact_summaries 仍空，阶段四填）。
- `app/core/config.py`：`goal_planner_enforce_enabled`（env `GOAL_PLANNER_ENFORCE_ENABLED`，默认 false）；`.env.example` 记录。
- `tasks/router.py` 的 `create_task`：enforce 开启时先 `plan_context`：
  - `clarify` → 落 assistant 消息（content=question.text，metadata 存 options，走 workspace 消息服务的 metadata 白名单——需把 `clarify_options` 加白）→ 返回 `202 {outcome:"clarify", message}`；不建任务、不产生积分。
  - `execute` → 取 sequence=1 的 goal，透传 goal_type/params 给 `TaskService.create(..., goal=...)`；>1 个 goal 时丢弃其余记 warning 日志。
  - Planner 抛错/`goal_planning_failed` → 回退 kol_selection，warning。
- `TaskService.create`：固定 kol_selection 改按入参建 goal（默认 kol_selection，兼容旧调用方与 brainstorm 内联建任务）。
- 响应契约：`POST /sessions/{id}/tasks` 返回 union：`{outcome:"task", task:TaskRead}` | `{outcome:"clarify", message:MessageRead}`。`src/api/tasks.ts` 与 contracts 同步。
- 测试：clarify 不建任务落消息、execute 按类型建 goal、多 goal 只建第一个、失败回退、开关关闭时旧路径、幂等键与 enforce 共存（先查幂等再规划，避免重复扣 planner 调用）。

## Task 2：GoalPolicy 框架 + KolSelectionGoalPolicy 下沉

- 新建 `backend/app/goals/policies.py`：轻量接口（不追求设计 §7.1 全量方法，按当前执行器需要）：
  - `goal_type: str`
  - `inject_export_contract: bool`（仅 kol True）
  - `loop_prompt(context) -> str`（系统 prompt 选择）
  - `ingest_enabled: bool`（仅 kol True）
- `AgentLoopContext` 加 `goal_type: str = "kol_selection"` 与 `goal_params: dict`（extra=forbid 需显式字段；goal_params 供 prompt 感知品牌/活动/周期）。
- `dependencies.py`：
  - `build_agent_context` 加 goal 参数：kol 才注入 export_contract；param_profile 与 goal.params 合并（goal.params 的 brand/period/platforms 优先）。
  - `agent_decide` 按 `context.goal_type` 选系统 prompt（kol 用 AGENT_LOOP_PROMPT 不变）。
  - `_run_agent_loop` 的 `selection.ingest` 调用加 goal_type 判断（仅 kol）。
- 新增 `BRAND_ANALYSIS_LOOP_PROMPT`（brand_loop_v1）与 `CAMPAIGN_ANALYSIS_LOOP_PROMPT`（campaign_loop_v1）：复用 AGENT_LOOP 的通用约束段（积分/时间基准/参数格式/熔断），目标段改为：品牌=声量/情感/趋势/内容主题/平台分布/竞品；活动=曝光互动/平台贡献/达人贡献/节奏/正负反馈/复盘。finish conclusion 要求相应调整。测试沿用 test_prompts 契约模式。
- 测试：三类型 context 组装差异（contract 注入有无、prompt 选择、params 合并优先级）、ingest 仅 kol 触发、kol 旧行为全绿。

## Task 3：品牌/活动报告构建器

- 新建 `backend/app/reporting/builders.py`（参照 `selection/analysis.py:run_kol_analysis`）：
  - 证据聚合 `collect_goal_evidence(task) -> list[dict]`：从 plan_json 的 results（EvidenceNote）提取各工具 structured_content，经 `sanitize_evidence` 截断脱敏。
  - `run_brand_analysis(*, user_id, session_id, task_id, goal_id, params) -> AnalysisReport`：聚合 + `BRAND_ANALYSIS_PROMPT`（brand_analysis_v1，输出 ReportDocument，固定区块：声量概览 metric_grid/平台分布 pie/情感占比 pie 或 bar/趋势 line/热门主题 tag_list 或 table/结论 markdown）→ `build_session_report(report_type="brand_analysis", scope={brand, period, platforms})`。
  - `run_campaign_analysis` 同理（campaign_analysis_v1：活动概览/平台贡献/达人贡献榜 table/节奏 line/正负反馈/复盘建议；scope 含 brand+campaign）。
  - 空证据抛 `LookupError("no_evidence_collected")`（由 finalize 映射 completed_with_warnings，设计 §15.3）。
- `ModelPurpose` 加 `"brand_analysis"`、`"campaign_analysis"`；prompt 注册 + test_prompts 契约。
- 测试：聚合函数（截断/脱敏/空证据）、两构建器端到端（假模型）落库 report_type/scope 正确、版本独立编号。

## Task 4：finalize 分派 + 手动重试端点

- `_TaskArtifacts.finalize_goal`：kol 分支保持现状；brand/campaign 分支调 Task 3 构建器 → 登记 artifact（`goal:{goal_id}:brand_report` / `campaign_report`，module_key brand/campaign）→ 发 `artifact.updated` + `report.updated`（双发兼容）；报告生成失败 → goal `completed_with_warnings` + artifact `status="failed"`（error_code 记录），不删证据。
- 手动重试：`POST /sessions/{id}/analysis-retry` body `{report_type: "brand_analysis"|"campaign_analysis"}`：找会话最近一个该类型且证据非空的 goal，重跑报告构建器（不调 MCP、不扣分），登记 `manual:{report_id}:{type}` artifact；无证据 409 `NO_EVIDENCE`。挂 selection/router 或新 reporting 端点（放 reporting/router.py 更合适）。
- 测试：三类型 finalize 分派、报告失败降级 completed_with_warnings + failed artifact、重试端点（成功/409/跨用户 404/不重复扣费）。

## Task 5：读取 API

- `GET /sessions/{id}/reports?report_type=`：该类型版本列表（report_id/title/version/scope/created_at/status），按 version desc；归属鉴权。
- `GET /sessions/{id}/artifacts/summary`：四个 module_key（brand/campaign/kol_analysis/kol_selection）各返回最新 artifact（id/type/title/version/scope/created_at）+ `unread: bool`（对照 artifact_read_states.last_seen_artifact_id）。
- `PUT /sessions/{id}/artifact-read-state`：`{module_key, artifact_id}` → mark_seen（Task 5 服务已就位）。
- `GET /sessions/{id}/kol-selection?set_id=` 与 `GET /sessions/{id}/kol-selection/export?set_id=`：可选 set_id 切换历史名单（缺省=最新，兼容现状）；`GET /sessions/{id}/selection-sets`：名单版本列表（set_id/title/version/created_at/item_count）。
- 测试：各端点契约 + 归属隔离 + 空态。

## Task 6：前端三 Tab + 历史版本 + 未读

- contracts/api：`ApiAnalysisReport` 加 `report_type`/`scope`；新 `reports.ts`（版本列表）、`artifacts.ts`（summary + read-state）；`kolSelection.ts` 加 set_id 参数与 selection-sets 列表；`tasks.ts` createTask 返回 union。
- `state/taskEvents.ts`：消费 `artifact.updated` → `TaskRuntimeState.artifactUpdates: Record<module_key, {artifactId, version, title}>`（与 report.updated 按 report/artifact id 去重）；任务完成不切换视图（本就不切）。
- `useWorkspace`：会话激活/任务终态/artifact.updated 时拉 artifacts/summary；未读状态入 Session；`markArtifactSeen(module_key)` 回调；clarify 响应渲染为 assistant 消息 + options chips（复用 brainstorm chips 机制）。
- `UniversalReport` 重构（改动最大）：三个一级 Tab「品牌分析 | 活动分析 | 达人」（达人内保留 KOL 分析/圈选达人两子 Tab）；各 Tab 头部显示品牌/活动/周期/生成时间/版本下拉；未读圆点（点击 Tab 后调 read-state 清除）；空态提示（「完成一次品牌分析后在此展示」）；圈选达人 Tab 支持历史名单下拉 + 导出当前选中 set。
- 测试：taskEvents reducer（artifact.updated 去重/未读）、useWorkspace（summary 拉取/已读回调）、UniversalReport（三 Tab 渲染/版本切换/空态/圆点清除）、api client 契约。

## Task 7：验证与文档

- `ruff check app tests` + `pytest -q` 全量；前端 `npm run test/lint/build`。
- 开发库 `.env` 与 UAT 评估是否开 `GOAL_PLANNER_ENFORCE_ENABLED`（先开发库验证再决定）。
- 更新 AGENTS.md（enforce 开关、GoalPolicy、三 Tab、新端点）；changelog/2026-07-24.md 追加阶段三小节。

## 不做（明确排除）

- 多 Goal 顺序编排/摘要传递/软依赖（阶段四）；artifact_summaries 上下文（阶段四）；停写 session_kol_selections 与双发收敛（阶段五）；E2E 新用例（沿用现有 Playwright 兜底，新链路靠单测/集成覆盖）。

## 风险点

- Planner 接管改变所有会话入口行为——开关灰度 + 失败回退兜底；评估脚本 `evaluate_goal_planner_shadow.py` 可复用监控误分类。
- clarify 响应契约变更影响前端发送链路——前端同步改 createTask 处理 union，旧会话无影响。
- UniversalReport 重构面大——保持 props 边界，新增逻辑进子组件，现有双 Tab 测试改写到三 Tab 结构。
