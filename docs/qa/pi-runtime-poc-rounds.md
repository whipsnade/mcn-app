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
