# 阶段二：Goal 与 Artifact 基础设施实施计划

设计依据：`docs/superpowers/specs/2026-07-23-multi-intent-task-artifacts-design.md` §19 阶段二。
范围确认：**只做阶段二**，用户行为保持不变；品牌/活动 GoalPolicy 与三 Tab 属阶段三，不在本次范围。

## 关键事实（已核实）

- 迁移 head：`0021_favorite_platform_uid`；0020 的 revision id 是 `0020_kol_selection_session_rpts`（与文件名不同），0022 的 down_revision 写 `0021_favorite_platform_uid`。
- 阶段一已落地：`backend/app/goals/`（schemas/validation/context/planner/evaluation），影子开关 `GOAL_PLANNER_SHADOW_ENABLED`，输出只写 `model_prompt_logs`。
- 执行链路：`TaskExecutor._run_agent_loop`（executor.py:235）→ `ExecuteMcpCall` → `McpGatewayService.execute_batch` → settled 后 `DatabaseSelectionIngest.ingest` → 收尾 `write_conclusion_message` + `_TaskArtifacts.auto_kol_analysis`（发 `report.updated`）。
- `session_kol_selections`：(session_id, platform, kol_uid) 唯一；`analysis_reports`：(session_id, version) 唯一、task_id 可空；版本用 FOR UPDATE 锁定读 + SAVEPOINT 重试。
- 前端 `src/state/taskEvents.ts` 有 `default:` 分支，未知事件类型静默忽略——新增 `goal.*`/`artifact.updated` 事件不会破坏现有前端。
- `quick/service.py` 是独立轻量计费旁路（`quick_mcp_calls`），不挂 goal_id，本次不动。

## 总体策略

- 单 Goal 包装：每条新任务在创建时落一条 `task_goals`（`goal_type="kol_selection"`、sequence=1、params 取会话 brand/category 快照）。阶段二不接 GoalPlanner 驱动执行，Planner 仍是影子。
- 双写过渡：`session_kol_selections` 继续写入（阶段五才停），同时写入新 `kol_selection_items`；兼容端点改读「最新 selection set」证明新链路，DTO 形状不变。
- 报告：`analysis_reports` 加 `report_type`/`scope_json`，存量回填 `kol_analysis`，唯一约束改 (session_id, report_type, version)，现有 version 编号不重排。
- 幂等：Artifact 一律按 `artifact_key` upsert（`goal:{goal_id}:{type}` / `legacy:{domain_id}:{type}`）。

---

## Task 1：迁移 0022 —— 新表 schema

新迁移 `0022_goal_artifact_infra.py`（down_revision `0021_favorite_platform_uid`），只建表改约束，不做数据回填：

- `task_goals`：id / task_id FK CASCADE / sequence / goal_type String(32) / status String(32) 默认 `pending` / depends_on_goal_id 可空自引用 / params_json / trajectory_json 可空 / result_summary_json 可空 / warning_code / error_code / started_at / completed_at / created_at / updated_at；唯一 (task_id, sequence)。
- `task_artifacts`：id / session_id FK CASCADE / task_id 可空 / goal_id 可空 FK task_goals / artifact_key String(191) 唯一 / artifact_type String(48) / title String(200) / version Integer / status String(16) / report_id 可空 FK analysis_reports / selection_set_id 可空 FK kol_selection_sets / scope_json 可空 / error_code / created_at / updated_at；索引 (session_id, artifact_type)。
- `kol_selection_sets`：id / session_id FK CASCADE / task_id 可空 / goal_id 可空 / version Integer / title String(200) / scope_json 可空 / status String(16) / created_at / updated_at；唯一 (session_id, version)。
- `kol_selection_items`：镜像 `session_kol_selections` 的字段（user_id/platform/kol_uid/nickname/followers/city/profile_url/fields_json/score_json/source_tool/first_task_id/last_task_id/时间戳），归属列改为 selection_set_id FK CASCADE；唯一 (selection_set_id, platform, kol_uid)，索引 (user_id)。
- `artifact_read_states`：id / user_id / session_id FK CASCADE / module_key String(32) / last_seen_artifact_id 可空 / seen_at；唯一 (user_id, session_id, module_key)。
- `user_brand_profiles`：id / user_id FK CASCADE / brand_name String(100) / is_default Boolean 可空（非默认存 NULL）/ metadata_json 可空 / created_at / updated_at；唯一 (user_id, brand_name) + 唯一 (user_id, is_default)（MySQL 唯一索引允许多 NULL，保证最多一个默认品牌）。
- `analysis_reports`：新增 `report_type` String(32) 非空 server_default `kol_analysis`、`scope_json` 可空；DROP `uq_analysis_reports_session_version`，ADD 唯一 (session_id, report_type, version)。
- `mcp_calls`：新增 `goal_id` String(36) 可空。

模型层同步（与迁移同 PR）：`goals/models.py`（TaskGoal）、新模块 `app/artifacts/models.py`（TaskArtifact、ArtifactReadState）、`selection/models.py`（KolSelectionSet、KolSelectionItem）、`identity/models.py`（UserBrandProfile）、`reporting/models.py`（AnalysisReport 加列）、`mcp_gateway/models.py`（McpCall.goal_id）。

验证：测试库 `alembic upgrade head` + downgrade 回滚一次再升级。

## Task 2：迁移 0023 —— 旧数据回填

`0023_goal_artifact_backfill.py`（DML only）：

- `analysis_reports.report_type` 存量已是 server_default `kol_analysis`，显式 UPDATE 兜底 NULL。
- 每个有 `session_kol_selections` 行的会话：创建一份 `kol_selection_sets`（version=1、title=「历史默认名单」、status=`completed`、task_id/goal_id NULL、scope_json NULL），把该会话全部行拷贝进 `kol_selection_items`（保留 source_tool/first_task_id/last_task_id/快照）。
- 存量 `analysis_reports` 每行登记 legacy Artifact：`artifact_key=legacy:{report_id}:kol_report`，task_id 取报告原值，status 与报告一致。
- 存量历史默认名单登记 legacy Artifact：`artifact_key=legacy:{set_id}:kol_selection_set`。
- 回填全部幂等（按 artifact_key / set 存在性跳过），可重复执行。

测试：构造含旧数据的会话跑迁移函数（抽成 `app/artifacts/backfill.py` 的可测函数，迁移脚本调用它），断言 set/items/artifact 数量与幂等。

## Task 3：selection service set 化 + 双写

`selection/service.py` 扩展（旧方法保留）：

- `ensure_selection_set(user_id, session_id, *, task_id, goal_id, title, scope) -> KolSelectionSet`：不存在则创建（version = 锁定读 max+1 + 唯一约束兜底，沿用 build_session_report 的并发模式）。
- `ingest_tool_evidence_to_set(selection_set_id, ...)`：复用现有归一化/二次归并/评分逻辑写 `kol_selection_items`。
- `latest_selection_set(session_id)`、`list_selection_items(set_id, ...)`、`count_latest_items(session_id)`、`get_all_for_export_set(set_id)`。
- `DatabaseSelectionIngest`（tasks/dependencies.py）改为：写当前 goal 的 set（新）+ 写旧表（双写），任一失败只 warning。

测试：ingest 双写一致、派生 uid 二次归并在 items 表同样生效、latest set 查询。

## Task 4：reporting report_type 适配

- `AnalysisReportService.build_session_report` 增加 `report_type="kol_analysis"`、`scope_json=None` 参数；版本计算改为按 (session_id, report_type) 锁定读；`latest_session_report` 加 report_type 过滤。
- kol-analysis 调用点传 scope（brand/category 快照）。
- 测试：同会话 kol_analysis 版本独立递增、并发冲突重试、旧读取端点契约不变。

## Task 5：artifacts 注册服务

`app/artifacts/service.py`：

- `register_artifact(*, session_id, artifact_key, artifact_type, title, version, status, task_id=None, goal_id=None, report_id=None, selection_set_id=None, scope=None)`：按 artifact_key 幂等 upsert；会话归属校验。
- `module_key_of(artifact_type)` 映射：kol_report→kol_analysis、kol_selection_set→kol_selection、brand_report→brand、campaign_report→campaign。
- 已读状态最小服务 `mark_seen(user_id, session_id, module_key, artifact_id)`（表已建；无新端点，阶段三接前端）。

测试：幂等重放、二选一外键约束、归属隔离。

## Task 6：goal_id 贯穿 MCP 与事件

- `ExecuteMcpCall` 加 `goal_id: str | None`；`McpCallService.prepare` 接收并落 `mcp_calls.goal_id`；`_matches_request` 幂等比对纳入 goal_id。
- `build_tool_event_payload` 加 `goal_id` 字段（None 时省略键）。
- executor `_run_agent_loop`：从当前 goal 取 id 传入 command 与事件。
- 测试：prepare 落列、幂等重放不误判、事件 payload 带 goal_id。

## Task 7：executor 包装 kol_selection Goal

- `TaskService.create` 同事务创建 TaskGoal（kol_selection、sequence=1、status=pending、params 取会话 brand/category 快照）；重试任务同样建自己的 goal。
- executor `_run_agent_loop` 开始：goal 标记 running、发 `goal.started`；旧任务无 goal 走 legacy 分支（行为与现状完全一致，不发 goal 事件、不写新表）。
- 收尾（`mark_completed/_with_warnings/insufficient_balance/failed` 对应映射 goal 终态）：
  - selection set 完成（status=completed）并登记 `kol_selection_set` Artifact；
  - `auto_kol_analysis` 产出的报告登记 `kol_report` Artifact；
  - 发 `goal.completed`/`goal.failed` + `artifact.updated`（与 `report.updated` 双发）；
  - goal.trajectory_json 镜像 task.plan_json。
- 余额不足：goal 标 `insufficient_balance`，已产生 Artifact 保留。

测试：全流程集成（任务完成 → goal 终态 + set + 2 个 Artifact + 事件序列）、legacy 任务无 goal 不受影响、恢复场景不重复建 Artifact。

## Task 8：默认品牌存储接线

- `identity` 增加 UserBrandProfile 服务（设置/查询默认品牌，事务内设默认时清其他行）。
- 端点 `GET/PUT /users/me/brand-profiles`（列表 + 设默认，schema 校验 brand_name 1-100）。
- `GoalPlannerContextBuilder` 的 `account_default_brand` 改从 `user_brand_profiles` 读（替换 None 占位）。
- 测试：最多一个默认品牌、planner 上下文带出默认品牌、端点归属隔离。

## Task 9：兼容端点切读新表

- `GET /sessions/{id}/kol-selection` → 读最新 selection set 的 items（DTO 形状不变；无 set 时 total=0）。
- `GET /sessions/{id}/kol-selection/export` → 导出最新 set；空则 409 `NO_KOL_SELECTION`（不变）。
- `POST /sessions/{id}/kol-analysis` → 基于最新 set（手动路径，`manual:` artifact_key）。
- 会话 DTO 的 `kol_selection_count` → 最新 set 的 count。
- 测试：端到端走一遍任务后旧端点返回一致数据；只有旧表存量数据的会话（回填后）端点正常。

## Task 10：验证与文档

- `cd backend && .venv/bin/ruff check app tests && .venv/bin/pytest -q`
- 前端无改动，仍跑 `npm run test && npm run lint && npm run build` 兜底。
- 迁移上开发库与测试库。
- 更新 `AGENTS.md`（数据模型、事件、双写状态）；写 `changelog/2026-07-24.md`。

## 不做（明确排除）

- 不接 GoalPlanner 驱动执行（仍影子模式）；不实现品牌/活动 GoalPolicy；不做右侧三 Tab 与已读提示 UI；不停写 `session_kol_selections`；不动 quick 计费旁路；不做 selection set 的新读取 API（`/selection-sets/*` 阶段三随前端一起上）。

## 风险点

- 回填数据量：按会话逐批处理，单事务过大时分批 commit。
- `uq (user_id, is_default)` 依赖 MySQL 唯一索引 NULL 语义，迁移后在真实 MySQL 验证（非 SQLite 行为推断）。
- executor 改动面最大，legacy 分支必须保持零行为差异（靠现有 `tests/tasks/test_agent_loop.py` 回归兜底）。
