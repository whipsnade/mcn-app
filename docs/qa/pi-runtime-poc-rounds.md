# Pi RPC POC 轮次记录

真实对比仅通过 `backend/scripts/run_pi_runtime_poc.sh` 启动。每个 round 追加记录命令、
模型/ Pi 版本、六例结果、供应商异常、人工可读性评分与 Gate A 结论；不记录密钥、DSN、
完整 Prompt、原始 DataTap 结果或内部 Run token。

---

## Gate A 预检（2026-08-07）— FAIL，未创建真实 round

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
