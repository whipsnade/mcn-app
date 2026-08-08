# Pi RPC POC 轮次记录

真实对比仅通过 `backend/scripts/run_pi_runtime_poc.sh` 启动。每个 round 追加记录命令、
模型/ Pi 版本、六例结果、供应商异常、人工可读性评分与 Gate A 结论；不记录密钥、DSN、
完整 Prompt、原始 DataTap 结果或内部 Run token。

---

## Gate A 预检（2026-08-07）— FAIL，未创建真实 round

> 后续设计方复核已将本记录重分类为 `BLOCKED / NOT RUN`；保留本标题和原文仅用于审计，
> 当前有效结论见文末“设计方复核”。

### 执行范围

- 仅执行配置/数据库门禁、DataTap 接入结构检查、单次只读 MCP `initialize` 探针和静态调用链审查。
- 未运行 `run_pi_runtime_poc.sh --case all --runtime both`，未创建 `outputs/pi-runtime-poc/<round>/`，
  未调用模型、未执行六个案例、未进行人工可读性评分。

### 已通过前置条件

- 接入链接确认包含四个独立 DataTap MCP 服务；多服务 endpoint mapping 已由
  `cd4d2d3` 支持，并只在启动进程环境中传递。
- 一个服务的 Streamable HTTP MCP `initialize` 返回 HTTP 200；未记录 URL、token 或响应正文。
- POC 代码与脚本对 `APP_ENV=test`、`MYSQL_DATABASE=kol_insight_pi_poc` 和
  `PI_RUNTIME_POC_ENABLED=true` 保持 fail-closed 门禁。

### 硬门槛失败与证据

**积分隔离：FAIL。** 对比 Harness 的 `CurrentRuntimeCaseExecutor` 必须按计划调用既有
`AgentRunExecutor.process_run`。该执行器装配 `AgentMcpTool`；任何真实 DataTap 工具调用在
`backend/app/agent_runtime/tools/mcp.py` 的 `DurableToolCallCoordinator.prepare` 中调用
`AgentMcpAccounting.reserve`，其 `reserve`、`settle`、`release` 均调用 `WalletService`。这会查询并
写入钱包/预留/结算，直接违反 POC “完全不处理积分”的不可违反边界。

不能通过给 POC 用户预置积分、替换 Current Runtime、mock MCP 或在代码中跳过会计来得到一次看似
成功的对比：前两者改变对比对象，后两者违反真实服务/不做 mock 的要求。因此在任何真实 MCP 或
模型调用前终止。本 Gate A 结论为 **FAIL**。

### 后续

- 已停止在方案 A；未进入方案 B 或方案 C。
- 若要重新进行 Gate A，必须先由设计方明确解决“Current Runtime 对比必须复用计费路径”与“POC
  禁止任何积分处理”的冲突，并形成新的确认计划；不能自行添加无积分 Current Runtime 分支。
- 仅当后续 Gate A 通过，才基于实测 Pi API 新建方案 B 详细计划；Pi 链路稳定一个发布周期后再
  评估方案 C。

---

## 设计方复核（2026-08-07）— 重分类为 BLOCKED / NOT RUN

原预检事实保留，但“Gate A FAIL”结论经设计方复核后不作为 Pi 效果失败：该轮没有调用模型、
没有运行六案例、没有创建真实 round，无法评价 Pi 或 Current 的报告效果。

根因是旧计划把用户确认的“POC 不考虑积分”错误扩大为“Current 基线也禁止读取和写入隔离
钱包”。正确边界是：积分不参与 POC 评价、不接触真实用户钱包；Pi 路径无钱包；Current
路径必须保持生产原生 `AgentMcpAccounting → WalletService`，但只使用
`kol_insight_pi_poc` 中每案例独立的一次性测试钱包。

代码复核同时发现 Current fixture 尚缺 Wallet、默认渠道权限，`profile_version` 错写为
`"1"`，Run model 写成占位值。新增计划 Task 8A 要求按 TDD 修复这些测试前置数据，不修改
Current 计费代码。Task 8A 通过后允许重新执行一次真实 Task 9；此前状态保持
**Gate A BLOCKED / NOT RUN**。

---

## Task 8A（2026-08-07）— Current 隔离基线已解除预检阻断

### 修复与隔离核对

- `PocCaseFactory` 为每个 Current 案例仅在 `kol_insight_pi_poc` 中创建一次性 Wallet：
  `balance=10000`、`reserved=0`；Pi 案例不创建任何 Wallet。
- 两侧用户均预置 `IdentityService.default_channels` 的渠道权限，Run 固定使用配置的真实模型名与
  `profile_version="v1"`。
- Current 仍走原生 `AgentMcpAccounting → WalletService`；没有增加 POC 旁路。积分字段只保留为
  Task 9 诊断数据，`assess_gate_a` 不以积分判定效果。
- 测试与迁移仅访问精确库 `kol_insight_pi_poc`；未连接 `kol_insight` 或 `kol_insight_test`。

### 验证结果

- `pytest -q tests/pi_runtime_poc/test_comparison.py tests/agent_runtime/tools/test_mcp.py` →
  **55 passed**。
- `pytest -q tests/pi_runtime_poc` → **64 passed**。
- `ruff check app/pi_runtime_poc tests/pi_runtime_poc scripts/run_pi_runtime_poc.py` →
  **All checks passed**。

### 后续

- Task 8A 已满足重启条件；尚未启动真实模型或 DataTap 六场景。
- 下一步只能运行一次修订后的 Task 9；完成后按实际证据写 Gate A PASS 或 FAIL，并停止在方案 A。

---

## Task 9 重启尝试（2026-08-07）— BLOCKED / NOT RUN

### 启动前核验

- `APP_ENV=test`、`MYSQL_DATABASE=kol_insight_pi_poc`、`PI_RUNTIME_POC_ENABLED=true`，迁移为
  `0036_export_claim_token (head)`；模型凭证与 DataTap token 均只确认存在。
- DataTap 连接配置有效，包含 `bilibili-mcp`、`insight-cube-mcp`、
  `social-grow-content-mcp`、`social-grow-mcp` 四个服务。token 与 endpoint 仅在本次子进程环境中
  存在，未写入文件、输出或本记录。
- 8000 端口空闲、无 POC 进程；Task 8A 提交 `3cc227f` 已确认。

### 阻断与证据

- 唯一一次 `bash scripts/run_pi_runtime_poc.sh --case all --runtime both` 启动尝试在创建任一案例前
  fail-closed，错误为 `pi_poc_datatap_endpoint_mapping_required`。
- 根因：启动脚本只将临时多服务映射导出为 `DATATAP_MCP_ENDPOINTS_JSON`，而 `Settings` 的
  `datatap_mcp_urls` 使用 Pydantic 字段环境名 `DATATAP_MCP_URLS`；因此映射没有进入 Settings。
  纯本地示例映射验证确认后者可被正确读取。
- 更正：输出根目录除既有 `.gitkeep` 外，还存在空目录 `20260807T120735Z/`。该目录由
  `begin_round()` 在四服务 Settings 校验前创建；目录中没有案例、模型、DataTap MCP 或 Pi 数据。
  `kol_insight_pi_poc` 中 `AGENT_RUNS=0`、`TOOL_CALLS=0`，未调用模型、DataTap MCP、Pi 进程或
  钱包，也未产生真实 six-case round。

### Gate A 结论

**Gate A BLOCKED / NOT RUN**。本次阻断发生在真实六场景之前，不能评价 Pi 效果，不能写 PASS 或
FAIL。本次没有启动案例，因而不消耗唯一真实 round 授权；完成 Task 8B 后的下一次运行才是第一轮
真实对比。不得进入方案 B 或方案 C。

---

## Task 8B（2026-08-07）— 配置桥接与 round 创建顺序修复

### 修复与隔离核对

- 外部 `DATATAP_MCP_ENDPOINTS_JSON` 只在启动进程中经 Shell helper 规范化为
  `DATATAP_MCP_URLS`；Python Settings 解析后，Pi factory 再从已校验四服务 mapping 重建
  `DATATAP_MCP_ENDPOINTS_JSON`。
- `begin_round()` 移至 DataTap catalog 刷新、Current executor 与 Pi executor 全部构建成功之后；
  预检失败不再创建空 round。历史 `20260807T120735Z/` 空目录保留，未删除或覆盖。
- 未改动 Current 计费、Wallet、DataTap 透明 Hook 或 Pi Extension 的工具边界。

### 验证结果

- Shell→Settings→Pi 端到端映射测试与“预检失败不调用 begin_round”测试通过。
- `pytest -q tests/pi_runtime_poc/test_comparison.py tests/pi_runtime_poc/test_task8b_bridge.py
  tests/agent_runtime/tools/test_mcp.py` → **58 passed**。
- `pytest -q tests/pi_runtime_poc` → **67 passed**；Task 范围 ruff 与 `bash -n` 通过。

### 后续

- Task 8B 完成后，下一次 `--case all --runtime both` 才是第一轮真实对比；此前没有真实案例执行。

---

## Task 9 启动尝试（2026-08-07，Task 8B 后）— BLOCKED / NOT RUN

### 已通过前置条件

- DataTap 四服务 mapping 已按 Shell → `DATATAP_MCP_URLS` → Settings → Pi factory 链路通过本地预检；
  迁移为 head、8000 端口空闲、无含数据的历史 round。
- 真实启动入口只使用临时进程内的连接 token 与 endpoint mapping，未写入文件或输出。

### 阻断与证据

- 启动在 Current 的第一个案例调用 `PocCaseFactory.create()` 时失败，错误为
  `agent_messages.session_id → agent_sessions` MySQL 外键约束失败。
- 根因是 factory 在同一个 pending flush 中写入 `AgentSession`、`AgentRun` 和 `AgentMessage`，但没有
  在 Message 前显式 flush 已被其标量外键引用的 Session/Run。事务已整体回滚。
- 本次新增空目录 `outputs/pi-runtime-poc/20260807T123431Z/`，其中没有文件；它由修复后的
  `begin_round()` 在 executor 构建成功后创建。精确 POC 库计数为 `AGENT_RUNS=0`、`TOOL_CALLS=0`、
  `AGENT_SESSIONS=0`、`AGENT_MESSAGES=0`、`WALLETS=0`。
- 未调用模型、DataTap MCP、Pi 子进程或钱包会计；没有形成真实 six-case round。

### Gate A 结论

**Gate A BLOCKED / NOT RUN**，不是 Pi 效果 FAIL。为避免重复真实 UAT，本会话不再运行；若要恢复，
需先确认新的最小 Task 修复 factory 的 MySQL 持久化顺序并补真实 MySQL 回归测试，再重新授权首轮
真实对比。方案 B/C 均未进入。

---

## Task 8C（2026-08-07）— MySQL fixture 持久化顺序修复

### 修复与隔离核对

- 新增显式 opt-in 的 `kol_insight_pi_poc` MySQL 回归测试；默认测试不运行它，绝不连接
  `kol_insight` 或 `kol_insight_test`。
- `PocCaseFactory` 现在严格按 `AgentSession → flush → AgentRun → flush → AgentMessage` 持久化，
  最后回填 `input_message_id`。未修改表结构、计费、Wallet、DataTap Hook 或 Pi。
- 初始红灯复现 Message FK；首次最小修复暴露 Run FK；逐层 flush 后在真实 MySQL 通过，表明根因是
  无 ORM relationship 的标量 FK 同批 flush 顺序，而非供应商或 Pi 行为。

### 验证结果

- `RUN_PI_POC_MYSQL_TESTS=1 pytest -q tests/pi_runtime_poc/test_task8c_mysql.py` → **1 passed**。
- 清理后 `AGENT_RUNS=0`、`TOOL_CALLS=0`、`AGENT_SESSIONS=0`、`AGENT_MESSAGES=0`、`WALLETS=0`。
- 未调用模型、DataTap MCP、Pi 或钱包会计；没有创建新 round。

### 后续

- Task 8C 提交后，重新执行 Task 9 前必须重做脱敏预检；下一次才允许启动首轮真实 six-case 对比。

---

## Task 8D 单工具 DataTap 冒烟尝试（2026-08-07）— BLOCKED / NOT RUN

### 启动前核验

- 精确数据库为 `kol_insight_pi_poc`，迁移为 `0036_export_claim_token (head)`；8000 端口空闲。
- 本次仅计划执行 `social-grow-content-mcp` 的 `tools/list` 和一个已审核、空参数的只读字典工具；
  不启动模型、不运行六场景，也不创建 Task 9 round。

### 阻断与保全

- Node 启动器在 DataTap 客户端创建后、任何 `tools/list` 或 `tools/call` 前因 `tsx -e` 的
  top-level await 编译限制终止（安全错误码：`tsx_top_level_await_unsupported`）。
- 单个临时 Pi Run 已显式收口为 `failed`；事实为 `AgentStep=0`、`AgentToolCall=0`、
  `EvidenceItem=0`，最后事件为 `run.failed`。未发生模型或 DataTap 调用。
- 安全诊断文件通过密钥模式检查；未记录 token、endpoint、DSN、Prompt 或供应商原始结果。

### Gate 与后续

**BLOCKED / NOT RUN**。这是本地冒烟启动器错误，不能评价 Pi 或 DataTap，也不构成真实单工具冒烟。
按“不因失败重试”的约束，本会话已停止；修正启动器并通过本地测试后，须重新取得一次单工具真实
DataTap 冒烟授权，才可继续。Task 9 与方案 B/C 均未进入。

## B0 Task 7 离线 Marketing Capability Pack 回放（2026-08-08）— PASS（本地 synthetic only）

### 范围与保全

- 回放只读取 `backend/fixtures/pi_runtime_poc/marketing_b0/` 中的脱敏 cases、results 和
  fake Pi events；`app/pi_runtime_poc/replay.py` 只返回确定性的 execution 值对象。
- 没有创建数据库、模型、DataTap、钱包或积分客户端；没有启动真实 Pi、没有运行或修改
  Task 9，也没有覆盖历史 round。此节不是 Gate A 真实业务结论。

### 六案例证据

- 品牌、活动和 KOL 三个报告案例分别带有期望 artifact type、Version、Evidence、scope
  和结构化 lineage/叙事/limitations 标记；`evaluate_case` 的十项 hard checks 全部为真。
- 钻取只绑定 `version-brand-b0`，不产生 Artifact 或 DataTap 调用；篡改绑定 Version 会
  被本地回放和 Gate 拒绝。
- 澄清和非营销拒答均为零 Artifact、零 DataTap，分别返回 clarification/refused 行为。
- 事件顺序、案例顺序、报告 Version 去重和 fixture digest 均确定性；缺失/乱序事件与
  重复报告 fail-closed。execution 与 hard-check summary 均可重复生成且不含人工评分；
  fixture 不含密钥、Bearer、endpoint 或用户凭证。

### 验证结果

- Task 7 定向回放：**6 passed**。
- 后端 `tests/pi_runtime_poc tests/agent_artifacts`：**709 passed、9 skipped**；范围
  Ruff 与 `git diff --check`：通过。
- Pi Runtime `npm test`：**47 passed（9 files）**；`npm run typecheck`：通过。

### 结论

本地 B0 回放 Gate 证据通过；它不授权真实 UAT 或 Task 9。Task 4R、Task 5、Task 6、Task 7
完成后停止，不进入 B1–B7 或方案 C。

---

## Task 8D 单工具冒烟启动器本地修复（2026-08-07）— READY_FOR_REAL_SMOKE_AUTHORIZATION

### 修复与边界

- 新增受版本控制的 `pi-runtime/scripts/datatap-single-tool-smoke.ts`，以显式 `main()` 启动，
  不使用 `tsx -e` 或 top-level await；固定命令为 `npm run smoke:datatap`。
- 入口必须显式设置 `RUN_PI_POC_DATATAP_SMOKE=1`，并在连接前校验 Run ID、Run token、内部审计
  地址、固定服务 mapping 与 DataTap 凭证；缺失任一项立即 fail-closed。
- 只允许 `social-grow-content-mcp` 的 `hotwords_xiaohongshu_dictionary`，最多一次 `tools/list` 与
  一次 `tools/call`；不调用模型、钱包、积分或六场景执行器。
- 异常时入口向受控 POC 内部 API 请求收口仅限 `single-datatap-smoke` 的临时 Run，避免残留
  queued/running 状态；诊断仅包含 code、stage、service_slug、tool_name、exception_type。

### 本地验证

- 进程级入口测试实际运行 `npm run smoke:datatap`，仅连接本机 fake MCP/fake audit server；验证成功
  顺序 `config → connect → tools_list → schema_validate → audit_start → mcp_call → audit_settle`，且
  `tools/list=1`、`tools/call=1`。
- 缺少 opt-in、Run ID、审计地址、mapping 或凭证均在连接前非零退出；上游工具错误会非零退出、调用
  audit fail 与 smoke Run 终态收口，不遗留 fake running 状态。
- 本地测试未调用真实模型或 DataTap，未创建真实 round，未进入 Task 9。

### 状态

**启动器本地修复完成，READY_FOR_REAL_SMOKE_AUTHORIZATION。** 这不是真实冒烟通过结论；下一步仍须
单独授权一次且仅一次真实 DataTap 单工具冒烟，之后再按数据库事实判断是否可进入后续记录。

---

## Task 8D 单工具 DataTap 真实冒烟（2026-08-07）— FAIL（审计启动链路）

### 执行事实

- 使用精确 `kol_insight_pi_poc` 数据库创建一个临时 Pi Run；仅选择
  `social-grow-content-mcp` 的固定只读工具，未启动模型、钱包、积分路径、Task 9 或方案 B/C。
- 安全阶段记录到 `config → connect → tools_list → schema_validate`，说明一次真实 `tools/list`
  已完成。执行未到 `audit_start`，因此没有发送 `tools/call`。
- 最终数据库事实：Run 已收口为 `failed`（最后事件 `run.failed`），`AgentStep=4`、
  `AgentToolCall=0`、`EvidenceItem=0`、模型调用记录 `0`、钱包行 `0`、积分预留/结算均为 `0`。
- 受控诊断文件经密钥模式扫描为干净，8000 临时回调端口已释放；未记录 token、endpoint、DSN、Prompt
  或供应商原始结果。

### 结论

**Task 8D 真实冒烟 FAIL，且未重试。** 失败位于 POC 内部审计启动边界，当前安全诊断未持久化异常类型/
错误码，因而不能据此评价 Pi 或 DataTap 的业务效果。Task 9 未运行，Gate A 没有新的 PASS/FAIL 结论。

---

## Task 8D 最小 POC 服务模型注册修复（2026-08-07）— 本地门禁通过

### 根因与最小修复

- 前一轮真实单工具冒烟的 `tools/list` 已成功，但 `tool-calls/start` 在 `audit_start` 前失败。
  根因是独立的 `app.pi_runtime_poc.server` 仅导入局部 ORM 模型；SQLAlchemy 在解析
  `agent_runs.user_id → users` 外键时抛出 `NoReferencedTableError`。
- `server.py` 在创建 FastAPI app 前显式导入中央 `app.db.models`，不导入 `app.main`，因此仍不会
  启动 Current Runtime 后台领取循环。
- 新增全新 Python 进程回归，证明最小服务自身注册 `users`、Agent Run/Step/ToolCall/Event 和
  Evidence 表；新增显式 opt-in 的 POC MySQL HTTP 回归，覆盖 diagnostics、tool-calls/start 与
  尚未创建 ToolCall 时的 smoke-failed 收口。

### 安全与验证

- `start_tool` 的数据库异常现在先回滚，再以 `stage`、`exception_type`、可选 MySQL errno 与约束名
  的安全投影记录，并返回稳定代码 `pi_poc_audit_start_failed`；不记录 SQL、请求正文、token、endpoint
  或 DSN。Pi HTTP client 也不再将非 2xx 响应正文拼入 Error message。
- 后端 POC：82 passed、5 skipped；显式 `RUN_PI_POC_MYSQL_TESTS=1`：6 passed；Pi Runtime：50 passed
  且 typecheck 通过；Ruff、`git diff --check` 与密钥模式扫描通过。
- 上述测试未调用 DataTap、模型、钱包或积分。新的真实单工具冒烟尚未在本节记录，执行后另行追加事实。

---

## Task 8D 单工具 DataTap 真实冒烟（模型注册修复后，2026-08-07）— PASS

### 执行事实

- 仅在 `kol_insight_pi_poc` 创建一个临时 Pi Run；只使用
  `social-grow-content-mcp` 的固定已审核只读工具。阶段顺序完整为
  `config → connect → tools_list → schema_validate → audit_start → mcp_call → audit_settle`。
- 实际次数：`tools/list=1`、`tools/call=1`、`AgentToolCall=1`、`EvidenceItem=1`；ToolCall 已 settled。
- Pi 路径没有模型调用、钱包行或积分流水；ToolCall 的 points reserved/settled 均为 `0`。临时 Run
  已收口为 `completed_with_warnings`，8000 端口已释放。
- 受控诊断文件通过密钥模式扫描；未记录 token、endpoint、DSN、Prompt 或供应商原始结果。

### 结论

**Task 8D 单工具真实冒烟通过。** 这是 Task 9 前的 POC 基础设施门禁，不是六场景效果 Gate A 结论；
Task 9、方案 B/C 均未运行。完整六场景仍须单独授权。

---

## Task 9 真实对比（2026-08-08，round `20260807T162812Z`）— Gate A FAIL

### 启动前修复与门禁

- 上一轮 `20260807T155018Z` 的 Current Run 已全部收口，旧进程和 8000 端口均已释放。
- 真实 MySQL 回归复现了 Pi Runner 长会话在 `REPEATABLE READ` 下漏读 Extension 已提交审计状态：
  首先会撞 `agent_run_attempts.uq_agent_run_attempts_run_attempt`，已有 Attempt 时则会撞
  `agent_steps.uq_agent_steps_run_sequence`。
- `PiRunAuditWriter` 对最新 Attempt 与最大 Step sequence 改为持锁当前读；红灯为唯一键
  `IntegrityError`，绿灯为定向测试 1 passed、Task 8D MySQL 并发 4 passed、POC 后端 89 passed、
  Pi Runtime 50 passed并通过 typecheck，Ruff 与 `git diff --check` 通过。
- 真实启动前确认数据库精确为 `kol_insight_pi_poc`、迁移为 `0036_export_claim_token (head)`、
  可执行态 Run 为 0、模型为 `deepseek-v4-flash`、四个 DataTap 服务映射齐全；凭证只确认存在，
  未输出值。

### 唯一真实轮次事实

- Current 品牌分析：`completed`，AgentToolCall 13（settled 12、unknown 1），Evidence 12，产生正式产物。
- Pi 品牌分析：首次真实 DataTap 调用 settled，AgentToolCall 1、Evidence 1；随后因
  `agent_events.uq_agent_events_run_sequence` 冲突失败，未产生正式产物。
- Pi 活动分析：首次真实 DataTap 调用 settled，AgentToolCall 1、Evidence 1；随后发生同一 Event
  sequence 冲突并失败。
- Current 活动分析：`completed_with_warnings`，AgentToolCall 16（settled 13、failed 1、unknown 2）、
  Evidence 13，产生正式产物。
- Current 达人圈选：`completed`，AgentToolCall 15、Evidence 13，产生正式产物。
- Pi 达人圈选：1 条调用 settled 并形成 1 条 Evidence，另 5 条调用在 Pi 进程失败时仍为 running；
  收口时仅将这 5 条零积分调用迁移为 unknown，不重放、不删除、不补造 Evidence。
- 因 Pi 品牌分析没有成功 Artifact，后续 `artifact-drilldown-v1` 在依赖预检时报
  `poc_dependency_run_unavailable`；澄清与非营销案例也未创建。输出目录保留上述 6 个结果 JSON，
  没有生成 `summary.json`，没有覆盖历史 round。

### 根因与 Gate 结论

- 新根因仍是同类 MySQL 快照读：`AgentEventStream.append_locked()` 已在持有 Run 行锁时分配 Event
  sequence，但最大序号查询没有使用当前读。Pi Runner 长会话漏掉 Extension HTTP 审计请求刚提交的
  `tool.started/tool.succeeded` Event，随后插入旧序号并触发唯一键冲突。
- 这次每个失败 Pi Run 都先完成了真实 DataTap 调用并落下 ToolCall/Evidence，证明 DataTap 和模型
  确实被调用；失败不能用于比较 Pi 与 Current 的真实分析能力。
- 所有实际 Run 已终态，全库可执行态 Run 为 0，悬挂 Pi ToolCall 为 0，8000 已释放；诊断和 round
  输出密钥模式扫描 PASS。Pi 路径积分始终为 0。
- **Gate A FAIL（POC 审计/验收夹具失败）**。没有完成六场景、没有 Pi 正式报告或 Excel，因此不做
  盲评和 Excel 对比，不得宣称效果 PASS，也不得重复本轮掩盖失败。

### 后续边界

- 若重新开放 Gate，先增加 Task 8E：为 `AgentEventStream.append_locked()` 增加旧快照真实 MySQL
  回归并改为当前读；同时让依赖案例在单侧上游失败时记录该侧失败结果，而不是中止整轮。
- Task 8E 本地通过后仍须重新取得一次完整真实 round 授权。本次停止在方案 A，不进入方案 B/C。

---

## Task 4A / 8D：pi-mcp-adapter 迁移与真实单工具冒烟（2026-08-08）— PASS

### 依赖与审计边界

- Pi Runtime 精确锁定为 `@earendil-works/pi-coding-agent@0.79.10`、根级
  `@earendil-works/pi-ai@0.74.2`、`@earendil-works/pi-tui@0.74.2` 与
  `pi-mcp-adapter@2.20.1`。降级原因是 `pi-ai@0.84.1` 已移除 adapter 所需的
  `complete` 运行时导出。Pi Core 自身的 `0.79.10` 子依赖仍保留在其声明树内，非 lockfile 漂移。
- 已删除自研 `datatap-mcp.ts` 与直接 MCP SDK 使用；项目级 `.mcp.json` 仅引用四个明确环境变量，
  adapter 以 `mcp` 代理模式运行，禁用 direct tools 与 mcpScript。
- 审计改为纯观测：Pi RPC 事件保留 Agent 工具尝试；仅 adapter 返回真实 `mcpResult` 后创建
  `AgentToolCall`；只有 `details.mode="call"`、无 `details.error` 且非 `isError` 才 settle 并生成
  Evidence。ToolCall 的 Step 同时保存带前缀 `requested_tool_name`、adapter 原始 `tool_name` 与
  `service_name`；裸名/未解析调用不生成 ToolCall 或 Evidence。

### 本地验证

- adapter 导入成功；同模型 RPC 状态探针确认 `deepseek-v4-flash`、相同 provider 与 `high` thinking，
  不发送模型请求。Pi Runtime 全量 **46 passed**，typecheck 通过。
- POC 后端相关回归 **21 passed, 8 skipped**；显式精确 POC MySQL 并发与最小服务回归 **9 passed**；
  Ruff、Bash 语法、`git diff --check` 与密钥模式扫描通过。

### 唯一真实单工具事实

- 临时 Run：`22e5ccb4-0175-4499-8e75-6bded6864acc`，数据库严格为 `kol_insight_pi_poc`。
- 仅使用 `social-grow-content`：adapter 以 `mcp({ connect: "social-grow-content" })` 完成一次发现，
  从返回的完整带前缀工具名执行一次已审核只读工具调用；不调用模型、钱包、积分或其他 MCP 工具，未自动重试。
- 阶段为 `config → connect → tools_list → schema_validate → mcp_call → audit_start → audit_settle`。
  因审计为观测旁路，`mcp_call` 先于审计持久化是预期行为。
- Run 为 `completed`，`AgentToolCall=1`、`EvidenceItem=1`、悬挂 ToolCall=0；Wallet=0、
  WalletTransaction=0、points reserved/settled=0。8000 已释放，安全诊断密钥扫描无命中。

### 结论

**单工具 adapter 链路 PASS，READY_FOR_TASK_9_AUTHORIZATION。** 该结论只证明 Pi 到 DataTap 的最小
受控链路及 Evidence 旁路；不是六场景 Gate A，也没有启动 Task 9、方案 B 或方案 C。

---

## Pi-only Task 9（2026-08-08，round `20260808T030356Z`）— INFRA_FAILED

### 执行事实

- 真实预检通过：数据库严格为 `kol_insight_pi_poc`、四个 DataTap 映射完整、模型为
  `deepseek-v4-flash` 且 thinking 为 `high`、8000 空闲、可执行态 Run 与悬挂 ToolCall 均为 0。
- 品牌研究创建了唯一 Pi Run `9d6d91f1-c923-496e-9159-cbc0b45f5f4e`。Run 在真实执行中出现
  `PiRpcProtocolError` 并终态失败；该案例的已落库事实为 Attempt 1、Pi decision 1、Step 23,197、
  DataTap ToolCall 25、Evidence 19、Artifact 1。
- 随后本地编排进程在 Run 已终态、无 MySQL 锁等待、无 Pi 子进程的状态下停止推进，无法进入后续
  独立案例。为释放受控服务，仅终止该精确挂起编排进程；不删除任何数据、不重放模型或 DataTap 调用。
- 同一 append-only round 已写入完整 `execution.json`：品牌为 `failed/PiRpcProtocolError`；产物钻取为
  `skipped_dependency/poc_dependency_artifact_unavailable`；活动评估、达人筛选、范围澄清和非营销拒答为
  `not_run/poc_round_aborted_before_case`。未创建 Current Run。

### 产物、隔离与安全核验

- 品牌留下一个 `brand_report_v3` Artifact；其 Excel 可由 openpyxl 打开，含 8 个 Sheet，BI payload
  来自同一不可变 Version。由于案例失败，不将此单个产物视为报告质量或 Gate 通过证据。
- Pi 用户 Wallet=0、WalletTransaction=0、points reserved/settled=0、悬挂 ToolCall=0。
- `human-review.json` 未创建；基础设施不完整时无需人工评分。零外部调用 finalizer 已生成
  `summary.json`，结论为 **INFRA_FAILED**。
- 8000 已释放；round 与安全诊断文件的密钥模式扫描均无命中。未使用视觉审核或 LibreOffice。

### 最小后续修复范围

- 为 Pi RPC 协议错误路径补充 client 生命周期关闭与编排返回的确定性测试：失败案例必须写入自身结果，
  后续无依赖案例仍可运行，最终必写六案例 execution.json；不得重放本 round。
- 本轮已发生真实外部调用，停止于方案 A，不进入方案 B/C。修复和本地验证后，如需新的完整 Gate，
  必须重新取得用户对新 append-only round 的明确授权。

---

## Pi 0.79 RPC 终态与有界审计修复（2026-08-08）— 本地通过，未执行真实调用

### 根因与修复

- Pi 0.79 的最终事件为 `agent_end`；`prompt.response` 只表示请求被接受。Runner 改为只在
  `agent_end.willRetry == false` 时收口，`willRetry == true` 继续等待后续重试事件；不再使用
  不存在的 `agent_settled`。
- RPC 单条 JSONL 上限从 1 MiB 调整为有界的 16 MiB。`agent_end` 在进入 Queue 前投影为
  `type/willRetry/messageCount`，完整 messages 不进入 Queue、AgentStep 或产品事件。
- `message_update` 同样只保留事件类型与实际 delta，丢弃 Pi 反复携带的累积 message/partial 快照；
  Runner 再做一次同样投影，防止测试或其他调用方绕过 RPC client 污染审计。
- `PiRpcProtocolError` 现有稳定安全 code，例如 `pi_rpc_record_too_large` 与
  `pi_rpc_invalid_record`；Runner 将该 code 写入失败终态，不记录原始 JSON、供应商内容或凭证。
- `close()` 对 process wait、stdout/stderr reader 与 stdin `wait_closed()` 分别限时；超时只会
  terminate/kill 当前精确 Pi 子进程，随后取消挂起 reader，不存在无界等待。

### 验证

- 红灯确认旧实现缺少安全 code、拒绝 2 MiB 的 Pi 终态、保留 message snapshot、等待
  `agent_settled`，且无关闭超时常量。
- 绿灯：POC pytest **113 passed**；Pi Runtime Vitest **46 passed**；typecheck、范围 Ruff、
  `agent_settled` 静态扫描和 `git diff --check` 通过。
- 本节所有测试使用 SQLite/Fake Pi 子进程或精确 POC 测试库；**没有启动模型、DataTap、单工具冒烟或
  Task 9 round**。

### 后续边界

- 这只修复本地协议与收口行为，不改变模型/provider/thinking、钱包、积分、DataTap 调用规则或方案 B/C。
- 如需再次运行真实单工具冒烟或六场景 Gate，必须取得新的明确授权，并使用新的 append-only round。

---

## Pi-only Task 9（2026-08-08，round `20260808T060814Z`）— EVALUATED_FAIL

### 预检与唯一真实执行

- fail-closed 预检确认：数据库严格为 `kol_insight_pi_poc`，迁移位于 head；Pi 依赖精确为
  `pi-coding-agent@0.79.10`、`pi-ai@0.74.2`、`pi-tui@0.74.2`、
  `pi-mcp-adapter@2.20.1`；模型为 `deepseek-v4-flash`，thinking 为 `high`，四个 DataTap
  服务映射完整；8000 空闲，active Run、running Attempt、悬挂 ToolCall 均为 0。
- 只执行一次 `bash scripts/run_pi_runtime_poc.sh --case all --runtime pi`。没有创建 Current Run、
  Current Wallet 或 `current.json`，也没有自动重跑 round 或真实 MCP 调用。
- 六案例结果均已写入同一 append-only `execution.json`：

| 案例 | Run ID / 数据库终态 | 模型调用 | DataTap ToolCall（settled/failed） | Evidence | Artifact Version |
|---|---|---:|---:|---:|---:|
| 品牌研究 | `e2eb5e46-5273-42e7-92a2-86925750376b` / completed_with_warnings | 1 | 20（17/3） | 17 | 1 |
| 活动评估 | `fcb15c8f-cfd4-4e0b-a55a-b6c972db4f7c` / completed_with_warnings | 1 | 20（15/5） | 15 | 1 |
| 达人筛选 | `a2b826bd-0f29-427d-917d-aa05b2e64fd5` / completed_with_warnings | 1 | 23（21/2） | 21 | 2 |
| 产物钻取 | `f7959f20-b582-4a88-a3e8-810a5906a731` / completed | 1 | 0 | 0 | 0 |
| 范围澄清 | `5e6f723e-c311-48b7-badc-27948b428bf7` / clarification_requested | 1 | 0 | 0 | 0 |
| 非营销拒答 | `66fc4143-c508-4a2f-a13b-4b5c0eff194e` / completed | 1 | 0 | 0 | 0 |

总计模型调用 6、DataTap ToolCall 63（settled 53、failed 10）、Evidence 53、不可变 Artifact
Version 4。全部 Attempt 已结束；全库 active Run、running Attempt、悬挂 ToolCall 均为 0。

### 产物结构与人工式证据复核

- openpyxl 内存检查：品牌 Excel 8 Sheet、0 个数值单元格、0 个图表；活动 Excel 9 Sheet、12 个
  数值单元格、3 个图表；两份达人 Excel 各 4 Sheet。所有文件可打开，未使用 LibreOffice、浏览器、
  截图或视觉审核。
- 品牌正式 payload 的 overview、sentiment、trend、topics、top_posts 等核心章节均 unavailable，
  `evidence_refs=0`、字段 lineage 为空；叙事却给出大量精确指标，所有 supporting_paths 为空。
- 活动 payload 有 13 个 evidence_refs，但 timeline、sentiment、top_posts、KOL、ROI 等多数章节
  不可用，字段 lineage 为空；完整叙事中的精确数字仍未绑定 supporting_paths。
- 达人案例发布了两份 `kol_selection_v3`，各只有一条空昵称、`platform=unknown`、得分 0、无报价的
  item；scope 丢失品牌、平台、预算和年龄窗口，但叙事声称具体达人和投放组合。
- 产物钻取保持 DataTap=0，但继续引用上述无法从发布版结构化 payload 核验的品牌数字。范围澄清终态
  正确。非营销案例虽保持 DataTap=0，却直接解释量子纠缠，没有按 fixture 拒答，是额外绝对行为失败。
- `human-review.json` 使用独立证据复核身份记录 12 项严格评分：品牌 `1/2/3/1`、活动
  `2/2/3/2`、达人 `1/1/1/2`（事实性/洞察/可执行性/限制披露）；所有报告均存在低于 3 的维度。

### RPC、隔离与最终 Gate

- 新终态协议在真实六 Run 中生效：五个正常完成 Run 的 `agent_end` 只保留
  `type/willRetry/messageCount`，不含完整 messages；澄清 Run 由受控澄清工具提前收口。
- 本轮保存 55,973 条最小 RPC 投影，总计约 4.92 MB；54,764 条 `message_update` 约 4.85 MB、
  单条最大 98 bytes。相比旧轮约 339 MB 已消除完整快照污染，但逐 token 行数仍是后续可优化项。
- 本轮用户 Wallet=0、WalletTransaction=0；预检前后全库钱包 17 行、流水 123 行，完全不变；
  所有 ToolCall 的 points reserved/settled 均为 0。8000 已释放。
- round、`human-review.json`、`summary.json` 和本轮空诊断日志的真实密钥、endpoint、DSN 及模式扫描
  均为 0 命中。
- finalizer 首次进程在进入 `finalize_round()` 前因 import 链意外实例化 Settings 而退出，未创建
  `summary.json`；确认无输出后，以非真实占位配置完成同一轮本地 finalizer。最终不可变
  `summary.json` 结论为 **EVALUATED_FAIL**。

### 最小后续修复范围

1. Builder/Publication 必须拒绝“叙事精确数字无 supporting_paths、无同版本 Evidence lineage”的发布；
   把已采集 Evidence 确定性映射进品牌/活动结构化章节，不能让 unavailable 数据被叙事绕过。
2. 达人 Builder 必须保留 case scope，拒绝空 nickname、unknown platform、全缺失评分 item，并保证正式
   名单与摘要中的达人和预算组合一致。
3. 将非营销拒答、钻取引用可追溯性纳入 execution 的绝对 hard_check；finalizer 应移除 Settings/DB
   import 副作用，真正做到无环境依赖的纯本地汇总。

本轮已发生真实外部调用，不得重跑或覆盖。停止于方案 A，不进入方案 B/C。
