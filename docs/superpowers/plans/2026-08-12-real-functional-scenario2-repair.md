# Scenario 2 生产链路修复实施计划

> **状态：** 实施已完成，离线验证与独立代码审查已完成（Critical 0 / Important 0 / Minor 0）；不得执行真实 Scenario 2。

> **执行方式：** 按本计划在当前隔离 worktree 内直接执行，每个阶段先添加针对失败事实的红灯测试，再实现最小修复并运行相关测试；不调用真实模型、DataTap、钱包或真实 UAT。

## 目标

修复 Pi Gateway 的 MCP 结果规范化、Evidence/账务结算、artifact completion gate、unknown reconciliation、跨 Attempt loop guard 和事件 delta batching，并用 fake topology 完成一轮离线 Scenario 2 回归。

## 约束

- 工作目录：`/Users/hanxiang/Works/Projects/codex/mcn-app/.worktrees/real-model-mcp-smoke-repair`
- 修复分支：`codex/real-functional-scenario2-repair`
- 基线：`7ee92a03d4b5207f215578c283dd1d0d7bd0bd78`
- 禁止读取/修改/清理旧 Run `3c3e6a2d-f020-453a-aa05-4e7bdb5e58b5` 和历史 UAT 数据；只读取脱敏报告作为事实。
- 不使用真实 provider 凭证；所有新增 adapter 形状测试均使用脱敏 fixture。

## 阶段 1：Run 创建时固化 profile-capability contract

### 1.1 先红后绿：快照契约

- 在 `backend/tests/agent_runtime/test_runtime_backend_snapshot.py` 增加测试：新 Run 的 snapshot 包含 `profile_name`、`required_artifact_contract`、`capability_pack_version`、`capability_pack_manifest_digest`，且字段与嵌套 capability 一致。
- 增加服务端映射测试：未审核 profile/contract、profile 不允许 artifact、contract 不在审核 pack 时 fail-closed；模型文本和 builder 调用不参与选择。
- 增加 existing Run 测试：修改当前 active runtime config 或 capability pack 不改变已保存 snapshot；历史 snapshot 不被回写。

### 1.2 实现

- `backend/app/runtime_config/schemas.py`：为 `RuntimeConfigSnapshot` 增加冻结审计字段和严格校验。
- `backend/app/runtime_config/service.py`：在 config JSON 中保存服务端 `profile_artifact_contracts`；`snapshot_for_new_run(tenant_id, profile_name)` 依据审核 pack/profile 解析 required contract；`snapshot_for_existing_run` 仅验证 Run snapshot。
- `backend/app/admin/schemas.py`、`backend/app/admin/gateway_service.py`：接入映射字段并在创建 runtime config 时做审核 contract 校验；管理响应不泄露 secrets。
- `backend/app/agent_runtime/router.py` 及所有 Run 创建 helper：传入服务端 profile 名，禁止从请求 body 读取任意 contract。
- `pi-gateway/src/protocol.ts`、`pi-gateway/src/main.ts`：扩展并严格映射 snapshot 字段，Gateway 不重新推导 contract。
- 新增 `backend/migrations/versions/0044_agent_run_loop_guard.py` 时一并只添加 loop guard 列（contract 字段留在 JSON snapshot，不修改历史行）。

### 1.3 验证

运行 snapshot/runtime-config/profile 定向 pytest、Ruff；`git diff --check`。

## 阶段 2：唯一 `mcp_result_v1` 与 Evidence/账务

### 2.1 先红：Gateway/adapter 归一化

- `pi-gateway/tests/mcp-accounting-extension.test.ts` 用脱敏真实 adapter 结果形状覆盖：原生非空 structuredContent、唯一合法 JSON text、空成功、多个 text、普通文本、UI resource/image、临时路径、omitted/oversized、明确 error、upstream request id。
- 断言只产生一个 canonical `mode=mcpResult` envelope；禁止从普通文本/resource/path 得到 structuredContent；known success unavailable 不调用 result_unknown。
- 更新旧 oversized 断言为 confirmed success + `result_unavailable` settle。

### 2.2 实现

- 新增/调整 `pi-gateway/src/mcp-result-envelope.ts`：定义严格判别联合、稳定 unavailable reason、JSON-only parser 和 request id 提取。
- `pi-gateway/src/mcp-accounting-extension.ts`：在 adapter/Gateway 边界归一化，不信任 summary/fullResultPath；payload 超大或本地持久化失败输出 unavailable。
- `pi-gateway/src/protocol.ts`：约束 finalize 详情只接受 canonical envelope。
- `backend/app/pi_gateway/result.py`（或等价专用模块）：后端再次校验 envelope、allowlist output schema 和 DataTap result 载荷解析；禁止文本/resource/image/path 绕过 Evidence。
- `backend/app/pi_gateway/service.py`：读取 canonical envelope，持久化 `upstream_request_id`，实现 available/empty/unavailable/confirmed failure/unknown 五条明确分支；Evidence 和 settlement 在同一事务边界，local persistence failure 不伪造成功。
- `backend/app/agent_runtime/models.py` 如需状态审计只复用现有 `error_type`，不改变 unknown 的保留语义。

### 2.3 先红：后端端到端账务

- `backend/tests/pi_gateway/test_mcp_chain.py` 和新定向测试覆盖：真实 adapter 形状 → 一条 Evidence → settled → points_reserved=0；empty/unavailable 无 Evidence 但 settled 费用已结算；result_unknown 保留 reserved 并阻止 completion。
- `backend/tests/admin/test_agent_tool_call_reconciliation.py` 增加 upstream_request_id 取回后二次 schema 校验、confirmed failure release、unknown 不动和两个事务/并发锁测试。

## 阶段 3：统一 completion validator

### 3.1 先红

- 新增 `backend/tests/pi_gateway/test_completion_validator.py`：assistant-only、running step、running/unknown call、未决 permit、缺 contract、缺 artifact、历史 artifact、错误 lineage 均拒绝并返回稳定 code；当前 Run 发布有效 Version 后通过。
- 扩展 `backend/tests/pi_gateway/test_terminal_gate.py`：正常 terminal、terminal ACK 丢失 Recovery、force-complete 均走同一 validator；业务拒绝不是 worker/infrastructure 错误，也不创建恢复 Attempt。
- `backend/tests/pi_gateway/test_recovery.py` 增加 unknown 阻止成功、失败终态无 running step/call。

### 3.2 实现

- 新增 `backend/app/pi_gateway/completion.py`：`CompletionValidator`、结果对象和稳定错误码；从 Run 自己的 immutable snapshot 读取 required contract；校验 tenant/user/session/run、Publication、Version、draft revision、lineage。
- `backend/app/pi_gateway/router.py` terminal 在 `settle_terminal` 前调用 validator；拒绝映射为业务 409。
- `backend/app/pi_gateway/service.py` Recovery 使用 validator；ACK lost 不再仅凭 assistant message force complete。
- `backend/app/agent_runtime/events.py` / `backend/app/agent_runtime/repository.py`：系统完成回调和 `force_complete` 只能接收已通过 validator 的结果，其他调用拒绝；失败/取消 cleanup 收口 open Step/Call。
- `pi-gateway/src/control-plane-client.ts` 增加稳定 business error；`pi-gateway/src/gateway.ts` 把 completion gate 拒绝收为 stable failed/completed_with_warnings，不标成 `pi_gateway_worker_failed`，不恢复重跑。

### 3.3 产物链路与离线回归

- 更新/新增 `backend/tests/integration/test_pi_gateway_offline_uat.py`：config/profile 固化 brand contract；缺 brand Version 拒绝完成；当前 Run Builder → Publication → Version → Excel/BI 同一 Version 才完成；历史 Version 不能满足。
- 使用现有 `backend/app/agent_artifacts` Publication/Version/lineage 服务，禁止在 completion gate 中推导 builder 调用。

## 阶段 4：跨 Attempt 持久化 loop guard

### 4.1 先红

- 新增 `backend/tests/pi_gateway/test_loop_guard.py`：同一稳定 builder error 第 3 次触发 `agent_loop_circuit_open`，写一次解释 assistant message；新 Attempt 继续被 guard 阻止；不同错误/成功 builder 重置。
- 覆盖 search_evidence 在 Evidence 集合版本不变时第 3 次触发；写入新 Evidence 后集合版本改变并重置。
- 断言 guard JSON 只由服务端更新、阈值为 3、fingerprint 去除 UUID/时间戳、终态 code 稳定。

### 4.2 实现

- `backend/app/agent_runtime/models.py` + `backend/migrations/versions/0044_agent_run_loop_guard.py`：新增 `loop_guard_json`。
- 新增 `backend/app/pi_gateway/loop_guard.py`：规范化错误指纹、Evidence 集合版本、状态更新、阈值和一次性解释消息。
- `backend/app/pi_gateway/internal_tools.py` / `backend/app/pi_gateway/router.py`：在内部工具成功返回前应用 guard，持久化后返回稳定业务 ToolResult；跨 Attempt 读取 Run 行，不重置。
- `pi-gateway/src/internal-tools.ts`、`pi-gateway/src/pi-session.ts`、`pi-gateway/src/worker-entry.ts`：收到稳定 circuit business result 后 abort 当前 SDK 会话并把 code 传到 Gateway terminal。
- `pi-gateway/src/gateway.ts`：business circuit failure 走稳定 failed 收口，不走 worker crash/recovery。

## 阶段 5：有界 delta batching

### 5.1 先红

- `pi-gateway/tests/event-projector.test.ts` 增加高量 thinking/message delta，断言批次字节、片段数、最大等待时间；边界 flush 顺序覆盖 tool/usage/turn、completion、cancel/abort、provider error、decision limit。
- `pi-gateway/tests/worker-entry.test.ts` 覆盖正常、异常、取消、provider/decision-limit、child exit 的 finally flush；`gateway.test.ts`/相关测试断言 terminal 前批次已发送。
- 离线 UAT 增加高事件量默认 buffer 用例：sequence 单调、message.completed 先于唯一 terminal、关键 tool/usage/artifact 不丢、无 overflow。

### 5.2 实现

- `pi-gateway/src/event-projector.ts`：thinking/message 分通道聚合，限制 4 KiB/32 fragments/50ms，提供幂等 `flush()` 和边界 flush。
- `pi-gateway/src/worker-entry.ts`：在 critical event 和 finally 中 flush，child 完成后才退出；flush 失败按 provider/worker error 稳定收口。
- `pi-gateway/src/gateway.ts`：terminal 前 drain projected events，保持默认有界 buffer，不改大到 20,000，不 sleep。

## 阶段 6：验证、审查、提交

截至当前已完成：

1. `pi-gateway` 全量 Vitest：25 files / 193 tests passed；typecheck、build passed；
2. 后端全量 pytest：2094 passed / 22 skipped；前轮 Evidence/Builder/Publication/Version/Gate 定向集：325 passed，最终结果/transport/内部工具回归：45 passed；
3. backend Ruff：`All checks passed!`；`git diff --check` passed；
4. 完整 fake topology 离线 Pi UAT：27 passed（不调用真实模型、DataTap、钱包，不创建真实 round）；
5. 独立代码审查：最终确认 Critical 0 / Important 0 / Minor 0，审查范围包含 contract 来源、strict union、账务原子性、unknown 不释放、completion 所有入口、跨 Attempt guard 与 delta flush 边界；
6. `changelog/2026-08-13.md` 记录真实失败事实、根因、红绿结果、验证命令和未解决风险，不改历史报告。

建议提交序列：

1. `fix(pi-gateway): normalize real mcp results for durable evidence`
2. `fix(pi-gateway): enforce artifact and unresolved-call terminal gates`
3. `fix(pi-gateway): bound failed artifact loops and event volume`
4. `test(pi-uat): cover real scenario 2 result and completion contracts`
5. `docs: record functional scenario 2 repair evidence`

完成后只报告：`READY_FOR_FUNCTIONAL_SCENARIO_2_RERUN_REVIEW`。不得自行重跑真实 Scenario 2，不得宣称 B7 PASS 或生产就绪。
