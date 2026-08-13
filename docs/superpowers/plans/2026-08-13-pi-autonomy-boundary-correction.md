# Pi 自主决策边界纠偏实施计划

基线：`a53533bbb60c37fc0f74afec51e068262d02de1f`  
分支：`codex/pi-autonomy-boundary-correction`  
范围：离线 TDD；不调用真实模型/DataTap/钱包，不创建 UAT Run。

## 执行约束

- 每个功能先增加能证明旧行为错误的红灯测试，再实现最小修复，最后运行定向测试。
- 不修改历史 Runtime Config、旧 Run、旧 UAT、历史 Evidence；不得 reset/rebase/amend/rewrite 已有提交。
- 新增数据库结构时使用新的 Alembic migration；优先复用现有 `loop_guard_json`，不为业务熔断扩展字段。
- 每个阶段形成独立提交，提交前运行该阶段的定向验证。

## 阶段 0：设计基线（当前）

- [x] 阅读 AGENTS.md、最新三篇 changelog、三份原始设计和 agent prompt。
- [x] 用 CodeGraph 跟踪 Runtime Config → claim → Pi session → MCP → Evidence → Builder → Publication → completion/recovery。
- [x] 新增 `2026-08-13-pi-autonomy-boundary-correction-design.md`。
- [x] 新增本实施计划。

## 阶段 1：Runtime Snapshot 与 capability allowlist

目标：停止 Profile→required artifact 推导，同时保留历史 Snapshot 只读兼容。

文件候选：

- `backend/app/runtime_config/schemas.py`
- `backend/app/runtime_config/service.py`
- `backend/app/agent_runtime/profiles.py`
- `backend/app/marketing_capability_pack/*`
- `backend/tests/runtime_config/*`
- `backend/tests/pi_gateway/*`

TDD 场景：新 Run Snapshot 只含 pack version/digest/allowed artifact contracts；旧 Snapshot 可读但不回写；Profile mapping 不再决定 contract；Pi 选定越权 contract fail closed；Pi 可选择多个已审核 contract。

## 阶段 2：统一 completion validator 与 Recovery

目标：正常 terminal、ACK、Recovery、force-complete 共享平台完成契约。

文件候选：

- `backend/app/pi_gateway/completion.py`
- `backend/app/pi_gateway/router.py`
- `backend/app/pi_gateway/service.py`
- `backend/app/pi_gateway/recovery.py`
- `backend/app/agent_runtime/events.py`
- `backend/app/agent_runtime/repositories.py`
- `backend/tests/pi_gateway/test_completion*`
- `backend/tests/agent_runtime/test_recovery*`

TDD 场景：无 artifact 文本完成；用户目标报告缺失时 Runtime 可完成；active Draft 阻断；abandoned Draft 带 limitation 放行；合法当前 Run Version 放行；历史 Version 不满足；unknown 只 warning、不 replay/Recovery；running/unresolved 仍阻断；业务拒绝不变成 worker crash。

## 阶段 3：透明 MCP bridge 与结算边界

目标：模型结果面保持 adapter 原始语义，sidecar 只服务 accounting/Evidence；严格区分 available/empty/unavailable/unknown。

文件候选：

- `pi-gateway/src/mcp-accounting-extension.ts`
- `pi-gateway/src/mcp-result*`（如需拆分）
- `pi-gateway/src/*adapter*`（必要时增加适配边界）
- `backend/app/pi_gateway/result.py`
- `backend/app/pi_gateway/service.py`
- `backend/app/agent_runtime/evidence.py`
- `backend/tests/pi_gateway/test_mcp_result*`
- `pi-gateway/src/*.test.ts`

TDD 场景：脱敏真实 adapter structured result→Evidence→settled；合法单 text JSON→available；普通 text/UI/image/path 不进 Evidence；adapter synthetic empty→empty；confirmed success payload too large/persistence failure→unavailable+settled；确认失败→release；unknown→reserved 保留；request/logical ids 持久化；trusted offload 安全校验；Scenario 1 裸 MCP 名称映射不回归。

## 阶段 4：LoopGuard 去业务化

目标：跨 Attempt 持久化观测，但不由相同 Builder/search 错误替 Pi 决定终态。

文件候选：

- `backend/app/pi_gateway/loop_guard.py`
- `backend/app/agent_runtime/engine.py`
- `backend/app/agent_runtime/tools/builders.py`
- `backend/app/agent_runtime/tools/history.py`
- `pi-gateway/src/internal-tools.ts`
- `backend/tests/agent_runtime/test_loop_guard*`
- `backend/tests/pi_gateway/test_terminal*`

TDD 场景：相同 Builder/Search 可以继续交回 Pi；错误指纹/Evidence 集合版本跨 Attempt 持久化可见；不同 logical_call_id 不被当作重复业务调用；高层 max_decisions 仍是唯一通用决策 fuse；无 Evidence 不生成空 artifact。

## 阶段 5：delta 与既有协议回归

目标：默认批次配置下覆盖所有退出边界，不靠扩大 buffer。

文件候选：

- `pi-gateway/src/event-projector.ts`
- `pi-gateway/src/worker-entry.ts`
- `pi-gateway/src/*.test.ts`

TDD 场景：正常完成、tool/usage/turn、cancel、abort、provider error、decision limit、child exit、terminal 前均 flush；批次 bytes/wait 有界；`message.completed` 在唯一 terminal 前；sequence 单调；关键 tool/usage/artifact 不丢；无 overflow；Scenario 1、usage 去重、terminal ACK、decision limit 不回归。

## 阶段 6：离线端到端回归、文档与审查

- [ ] 运行 backend 相关 pytest、全量 pytest、Ruff。
- [ ] 运行 Pi Gateway 全量 Vitest、typecheck、build。
- [ ] 运行完整离线 Pi UAT 一轮；确认没有真实 provider/DataTap/wallet/UAT。
- [ ] 运行 migration check、`git diff --check`、`git show --check`、secret scan、进程/端口残留检查。
- [ ] 更新当日 changelog：记录原始偏差、纠偏决策、红绿灯、未解决风险和“用户目标评价与 Runtime completion 分离”。
- [ ] 独立代码审查，目标 Critical 0 / Important 0。

## 预期提交序列

1. `docs: define Pi autonomy boundary correction`
2. `test(runtime): cover capability-only artifact snapshot`
3. `fix(runtime): remove profile required artifact derivation`
4. `test(pi-gateway): cover generic completion and recovery gate`
5. `fix(pi-gateway): unify platform completion validation`
6. `test(pi-gateway): cover transparent MCP result accounting`
7. `fix(pi-gateway): preserve raw MCP semantics beside accounting sidecar`
8. `fix(runtime): persist loop observations without business circuit termination`
9. `test(pi-uat): cover autonomy and terminal regression matrix`
10. `docs: record autonomy boundary correction evidence`

架构纠偏完成后，仍不得执行真实 Scenario 2；对外状态按会话既定门禁报告为：

`READY_FOR_FUNCTIONAL_SCENARIO_2_RERUN_REVIEW`
