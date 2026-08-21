# Real UAT MCP 结果直通与取消闭环修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Direct Pi production path 中标准 MCP `CallToolResult` 被错误拒绝的问题，并让 running/reviewing Run 的取消从 API 持久信号贯通到 Gateway、worker、账务、Recovery、SSE 和前端；在一次受控真实瑞幸咖啡 UAT 中证明结果直通与取消均闭环。

**Architecture:** 保留 `runtime_backend=current` 的旧执行路径和旧数据兼容语义，不把旧 Run 重新执行或改写。新 Pi Run 只经 `PiGatewayService`、独立 Gateway worker 和 Pi session；MCP accounting 只观察经过 Pi 的标准结果所需的有界结算元数据，不读取、分类、替换业务 `content`，也不要求 Evidence。取消使用数据库 `cancel_requested` 作为持久事实：API 写入一次，Gateway heartbeat/外发 preflight/worker abort 共同构成安全栅栏，终态统一由 `AgentEventStream.settle_terminal` 生成唯一 `run.cancelled`；可能已外发但没有确定响应的调用保持 `result_unknown` 预留并交给 Recovery，禁止重放。

**Tech Stack:** Python 3.11/3.12、FastAPI、SQLAlchemy Async、Pydantic、pytest、Ruff；TypeScript、Pi SDK、Vitest、Node child-process IPC；React 19、Vitest、SSE reducer；MySQL 8；真实预发布 Web UAT 使用现有 DataTap 与模型服务。

**Spec:** `/Users/hanxiang/.codex/attachments/4fa2281f-9aa0-4468-b989-3689b479eacf/pasted-text.txt`

## Global Constraints

- 基线必须是 `e850fa956685932b9ec7ee2fe7c940fbe496ca5a`；当前分支为 `codex/real-uat-mcp-cancel-repair`，worktree 为 `.worktrees/real-uat-mcp-cancel-repair`。
- 只新增线性提交；禁止 reset、rebase、amend、stash、改写历史和修改旧 Run/Version/Snapshot/迁移数据。
- 旧 Run `8dd00415-9338-42f2-bec0-a9e4de66824e` 仅做脱敏只读诊断；不重试、不 reconcile、不释放历史预留。
- 不恢复 Evidence Bridge、`mcp_result_v1`、固定 required-artifact 门禁、Candidate/Corpus/Stage/observation Gate 或业务固定工具顺序。
- 标准 MCP 结果的 `content`、`structuredContent`、`isError` 保持原始业务语义；不因 resource+text、多 block、普通非 JSON 文本、缺 structuredContent 或标准工具错误而产生 `unsupported_content`/`result_unavailable`。凭证、Bearer、API Key、DSN 仍禁止进入模型或日志。
- 新增行为先写红灯测试并实际观察失败，再写最小实现；只运行一次规定的受影响定向验证，不重复完整 UAT。

---

## Task 1：完成基线、路径和历史失败证据登记

**Files:** `docs/qa/2026-08-21-real-uat-mcp-cancel-repair.md`、`docs/superpowers/plans/2026-08-21-real-uat-mcp-cancel-repair.md`

- [x] 记录 baseline、branch、worktree、main clean 状态以及 CodeGraph 健康状态。
- [x] 记录旧 Run 的脱敏结果：`runtime_backend=current`、profile、决策/Attempt 状态、Gateway/lease 已释放、事件终态唯一性、工具状态和 `result_unavailable`/`result_unknown` 错误类别；不保存业务 payload、参数或凭证。
- [x] 从现有 QA/离线 UAT 记录整理当前 12 个失败的测试名、错误码和实际调用路径，保留为本轮修复前红灯证据。
- [x] 通过 `RuntimeConfigService`、Gateway claim、worker wiring 和部署 hash 验证新 Pi path 与 legacy `DataTapTransport/Evidence` path 的边界；没有路径证据前不删除任何 legacy 分类器。

## Task 2：为标准 MCP Result 直通建立红灯测试

**Files:** `pi-gateway/tests/direct-mcp-result.test.ts`、`pi-gateway/tests/mcp-accounting-extension.test.ts`、必要时新增 `pi-gateway/tests/pi-session-mcp-result.test.ts`、`backend/tests/pi_gateway/test_direct_mcp_architecture.py`、`backend/tests/pi_gateway/test_mcp_finalize.py`

- [x] 先添加 resource+text、多 text、普通非 JSON text、structuredContent、`isError=true` 五类冻结形态，并通过模型 handoff seam 断言原始 `content`/`structuredContent`/`isError` 仍可见（本轮复核现有 Direct Pi baseline 测试，不新增桥接协议）。
- [x] 断言 accounting hook 不读取或替换 `event.content`，不创建业务 envelope，不主动读取或展开 `ui:` resource；结算元数据不含业务结果。
- [x] 断言成功调用恰好一次 settlement、`error_type IS NULL`、Evidence 增量可为 0、不会出现 `unsupported_content` 或 `result_unavailable`；标准工具错误按既有失败语义结算且仍把错误结果提供给模型。
- [x] 模型/日志 secret boundary 由现有 adapter/session 与静态扫描覆盖；未把业务结果做脱敏、摘要、裁剪或改写。
- [x] 定向 MCP 测试复核完成；本轮未删除历史红灯或修改其 expected。

## Task 3：实现 Direct Pi Result 透传与计费解耦

**Files:** `pi-gateway/src/mcp-accounting-extension.ts`、`pi-gateway/src/pi-session.ts`、`pi-gateway/src/resource-loader.ts`、必要时新增 `pi-gateway/src/mcp-result-bridge.ts`、`backend/app/pi_gateway/service.py`、`backend/app/mcp_gateway/datatap.py`、`backend/app/agent_runtime/tools/mcp.py`

- [x] 将标准结果的模型 handoff 责任固定在 Pi adapter/session 边界：原始 MCP result 由 Pi 保留，accounting 仅发送 permit 与严格的非业务结算元数据；不以 Evidence 写入成功作为模型可见结果的前置条件。
- [x] 确保 direct/proxy adapter 配置关闭输出 guard 的业务改写，并在 Pi 结果边界保留原始标准字段；不把 `structuredContent` 退化成“唯一 JSON text”，不把 resource+text 误判为空，不生成新的业务类型或固定结果 contract。
- [x] 将 `isError=true` 分成“模型看到的标准工具错误”和“账务失败分类”两条独立路径；只有无确定响应才用 `result_unknown`，确定收到标准错误则按 `failed_confirmed` 释放预留。
- [x] 确认当前 Pi production path 不消费 `unsupported_content`/`result_unavailable`；legacy `backend/app/mcp_gateway/datatap.py` 兼容读取保留，未被 Pi Run 调用。
- [x] Direct Pi Evidence delta=0、成功 settlement/error_type、单次结算和 credential boundary 的既有实现测试复核通过。

## Task 4：为取消 API、preflight 和终态一致性建立红灯测试

**Files:** `backend/tests/agent_runtime/test_cancel.py`、`backend/tests/pi_gateway/test_mcp_preflight.py`、`backend/tests/pi_gateway/test_recovery.py`、`backend/tests/agent_runtime/test_events.py`、`backend/tests/agent_runtime/test_api.py`（按现有测试布局落位）、`src/api/agent.test.ts`

- [x] 覆盖 running/reviewing 首次取消原子写入 `cancel_requested`、重复取消幂等、queued/paused/clarification 即时取消、终态重复取消不翻转。
- [x] 覆盖 `AgentRunRead`、session detail、前端 API 类型携带 `cancel_requested`。
- [x] 覆盖取消请求后 `preflight_mcp` 在任何 call/permit/reserve/dispatch 前稳定拒绝，且新预留与新 dispatch 均为 0；Builder/Publication 等新写操作同样不能越过取消栅栏。
- [x] 覆盖唯一 `run.cancelled`、Attempt=cancelled、无 running Step、无 planned/running permit、session slot/lease 释放，以及 terminal ACK loss 后 Recovery 不恢复模型执行。
- [x] 覆盖 in-flight 已发送调用进入 `result_unknown` 且保留预留，已确认成功只结算一次，取消与成功竞态不产生第二终态。
- [x] 先观察红灯：preflight 未拒绝、API DTO 缺字段、迟到 terminal ACK 收口为 completed、reviewing 卡片缺少取消动作；随后以最小修复转绿。

## Task 5：实现后端取消持久状态与外发门禁

**Files:** `backend/app/agent_runtime/router.py`、`backend/app/agent_runtime/events.py`、`backend/app/agent_runtime/repository.py`、`backend/app/pi_gateway/service.py`、`backend/app/pi_gateway/router.py`、`backend/app/agent_runtime/engine.py`、`backend/app/agent_runtime/executor.py`、`backend/app/agent_runtime/recovery.py`、对应 schema/DTO 文件

- [x] 在 Run DTO 和 session detail 输出 `cancel_requested`；取消 endpoint 只写一次持久取消信号，重复请求复用现有终态/信号，不重复追加非终态取消事件或终态事件。
- [x] 在 `preflight_mcp` 最早的 Run/lease 校验后、任何逻辑 call、Attempt/Step、钱包 reserve 前检查取消信号，并返回稳定、不可重试的 `cancel_requested` 业务错误。
- [x] 统一 API、engine、executor、Pi recovery 使用 `AgentEventStream.settle_terminal(...CANCELLED...)`；保留历史 Snapshot 读取且不回写。
- [x] 取消收口按发送事实关闭 call：planned/未发送为 `definitely_not_sent`，确认失败为 `failed_confirmed`，可能已发送为 `result_unknown`；不自动重放 unknown。

## Task 6：实现 Gateway/worker/provider abort 和 ACK-loss 取消闭环

**Files:** `pi-gateway/src/gateway.ts`、`pi-gateway/src/worker-entry.ts`、`pi-gateway/src/worker-bridge.ts`、`pi-gateway/src/ipc-rpc.ts`、`pi-gateway/src/control-plane-client.ts`、`pi-gateway/tests/cancel.test.ts`、`pi-gateway/tests/worker-entry.test.ts`

- [x] 以真实 provider stream 取消测试锁定 heartbeat 发现取消后的 abort 顺序、provider stream abort、child SIGTERM→有界 SIGKILL、Child close 后才回收 worker PID/lease/slot。
- [x] 取消后父/子 IPC bridge 立即进入关闭态，不能新增 `internal_tool`/`mcp_preflight`/finalize handler；在飞调用按实际发送状态向后端收口。
- [x] 防止 Gateway 因 heartbeat、worker done、terminal ACK 丢失产生重复 completed/failed/cancelled；迟到 heartbeat 404 被 terminalization fence 视为正常竞态，terminal 失败仍交给 Recovery。
- [x] one-worker topology 的现有测试覆盖活跃 worker PID、heartbeat timer、event listener、RPC pending 和 child 端口/连接清理。

## Task 7：接通前端取消状态、SSE 和正确 active Run

**Files:** `src/api/agent.ts`、`src/hooks/useAgentRun.ts`、`src/hooks/useAgentWorkspace.ts`、`src/state/agentEvents.ts`、`src/components/ChatArea.tsx`、对应 Vitest/E2E 定向测试

- [x] 让 cancel handler 只对当前 active Run 发一次请求；暴露并持久化 `isCancelling`，来自 `cancel_requested` 的 session reload 能恢复该状态。
- [x] 将按钮文案改为“取消任务”，点击后禁用并保持 SSE，直到真实 `run.cancelled`；不把 API 响应直接伪造成终态。
- [x] API 失败时恢复可点击状态并展示明确错误；终态事件后清理 cancelling、退出处理中、刷新 session/wallet。
- [x] 覆盖历史 Run 与当前 active Run 锚定、页面刷新、重复点击、SSE 断线重连和 cancel terminal event 顺序。

## Task 8：一次性受影响验证、真实 worker topology、审查与线性提交

**Files:** `docs/qa/2026-08-21-real-uat-mcp-cancel-repair.md`、`docs/runbooks/phase-2-runtime.md`、`docs/runbooks/agent-runtime-v3-cutover.md`、`docs/superpowers/plans/2026-08-21-pi-autonomous-marketing-skills-implementation.md`、`changelog/2026-08-21.md`

- [x] 按授权顺序完成 Backend/Pi Gateway/前端定向回归、worker/heartbeat race、Ruff、Gateway typecheck/build、前端 build、diff 和静态 secret/DSN 扫描；根目录 tsc 的既有 Pi Runtime 缺失依赖已记录为候选门禁阻断。
- [x] 已完成定向 Backend/Pi Gateway/前端验证、原 12 项一次执行和完整离线 UAT 一次；完整 UAT 唯一失败只隔离复验一次，未重跑全套，详细结果写入 QA。
- [x] 完成独立只读审查，Critical=0、Important=0；确认标准 MCP Result 直通、没有 Evidence Bridge、取消后无外发、unknown 不被释放/重放、无 worker/lease/port 残留、前端不是视觉假取消。
- [x] 已创建线性提交：`fd61e9e fix(runtime): make in-flight cancellation effective`、`dbd2a96 test(uat): cover direct mcp result and cancellation`、`de7c45b docs: record real uat evidence`；Direct MCP 透传沿用已审计基线，不创建空提交。
- [x] 已重建 `codex/real-uat-mcp-cancel-integration-r4` 并快进合入本地 `main`（`0bb65bc`）；本地生产 CI 范围已通过，普通 CI 已排除本轮一次性离线拓扑 UAT。
- [x] 本地完整 Browser E2E 的唯一失败类型已定位为旧“暂停”选择器，提交 `7b83d82` 改为“取消任务”；隔离三个视口验证 `3 passed in 6.9s`。
- [ ] `0bb65bc` 对应远程 Actions run `32470724885` 未全绿（Backend、Frontend、Browser E2E、Pi Gateway failed；公开 API 日志下载 403，无权限读取具体日志）；需在不猜测原因的前提下取得远程 CI 全绿，再部署预发布并执行一次专用 UAT 租户的“Direct MCP + 取消”组合 Web UAT：一次瑞幸咖啡请求，Attempt=1、模型≤10、DataTap≤5、积分≤50、retries=0、fresh Run=0。
- [ ] 组合 UAT 必须记录一个此前会产生 `unsupported_content` 的工具成功返回、模型收到标准结果、点击“取消任务”后的阶段时间戳、无后续外发、唯一 cancelled、Attempt/ToolCall/permit/lease/worker/端口收口。通过后只报告 `REAL_UAT_MCP_PASS_THROUGH_AND_CANCEL_PASS / READY_FOR_FINAL_FUNCTIONAL_UAT`；本轮不做生产灰度。
