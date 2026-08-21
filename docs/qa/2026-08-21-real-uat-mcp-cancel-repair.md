# 2026-08-21 真实 UAT：MCP Result 直通与取消闭环修复记录

日期：2026-08-21
分支：`codex/real-uat-mcp-cancel-repair`
Worktree：`.worktrees/real-uat-mcp-cancel-repair`
基线：`e850fa956685932b9ec7ee2fe7c940fbe496ca5a`

## 1. 修复前边界与历史证据

本轮不修改旧 Run `8dd00415-9338-42f2-bec0-a9e4de66824e`。只做了脱敏只读查询：

| 项目 | 事实 |
| --- | --- |
| Run 状态 | `cancelled`；`cancel_requested=true` |
| Runtime backend | `current`，不是 Direct Pi |
| Profile / Attempt | `session_analyst_v1` / Attempt 1，Attempt 终态 `cancelled` |
| 决策数 | 25 |
| Gateway / lease | `gateway_id`、Gateway lease、普通 lease 均为空，未发现残留租约 |
| 终态事件 | `run.cancelled` 恰好 1 条，Run 最后事件为该终态事件 |
| 工具状态汇总 | 9 类 settled、1 类 failed、1 类 unknown；unknown 为 `result_unknown`，预留仍在 |
| 旧结果错误类别 | 多笔 settled 调用带 `result_unavailable`；另有 1 个 `result_unknown`，没有读取业务 payload/参数 |

该 Run 实际落在 legacy/current 执行链，不足以证明 Direct Pi adapter 本身的结果传递行为；因此本轮不
直接删除 legacy `DataTapTransport/Evidence` 兼容分类，而是修复并验证新 Pi path 的边界，并继续保留
历史数据兼容读取。

预发布服务只读部署核验：systemd 使用
`/home/kol_insight/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8100`，服务当时为 active；
已核对 `backend/app/pi_gateway/service.py`、`backend/app/agent_runtime/tools/mcp.py` 和
`backend/app/agent_runtime/router.py` 的脱敏文件 hash 与当前基线 worktree 文件一致。远端目录没有以
可审计 Git commit 标识当前运行版本，因此真实候选 UAT 前仍必须用新 candidate 部署并再次记录 commit。

## 2. 历史 12 项 required-artifact 红灯（保留，不删除）

以下是上一阶段保留的修复前证据，错误码为旧完成门禁
`pi_gateway_main_artifact_missing`，实际活跃路径为 Pi engine → `CompletionValidator`，并被 terminal
ACK-loss、force-complete 和 Recovery 复用；不是本轮 MCP/账务失败证据：

1. `test_non_marketing_refusal_zero_side_effects`
2. `test_generic_proxy_bare_remote_name_billing_chain`
3. `test_generic_proxy_bare_name_unique_mapping_without_server`
4. `test_generic_proxy_ambiguous_remote_name_with_explicit_server`
5. `test_generic_proxy_unique_live_duplicate_dispatches_to_claimed_service`
6. `test_model_budget_two_decisions_complete`
7. `test_cross_tenant_isolation`
8. `test_drilldown_binds_exact_version_with_zero_datatap`
9. `test_draining_gateway_stops_new_claims_but_finishes_active`
10. `test_current_to_pi_to_current_and_kill_switch_only_affects_new_runs`
11. `test_sse_ordering_and_last_event_id_resume`
12. `test_run_snapshot_immutable_across_rollout`

本轮不会删除失败用例、只改 expected 或把 `analysis_report_v1` 变成新的固定 Artifact 门禁。

## 3. 本轮修复目标

- 新 Pi path：标准 `CallToolResult` 的 `content`、`structuredContent`、`isError` 按原始业务语义交给
  Pi/model；accounting 只观察结算元数据，不读取/替换业务 content，不要求 Evidence，不产生
  `unsupported_content`/`result_unavailable`。
- 取消 path：`cancel_requested` 从 API 持久化信号贯通到 preflight、heartbeat、provider abort、child
  SIGTERM/SIGKILL、unknown 账务、唯一 `run.cancelled`、Recovery、SSE 和前端。
- 兼容边界：旧 `current` Run、旧 Snapshot、旧字段和 legacy 读取语义不回写、不重放、不被新 Pi path
  消费为完成门禁。

## 4. 本轮实现与路径审查

### 4.1 Direct Pi MCP Result

Direct Pi 的原始 `CallToolResult` 透传修复已在基线提交中完成，本轮只做路径复核和回归，不重新引入
任何业务桥接。`pi-gateway/src/pi-session.ts` 关闭业务输出 guard，`pi-gateway/src/mcp-accounting-extension.ts`
只观察结算所需的非业务 metadata；`event.content`、`structuredContent`、`isError` 不被 accounting
读取、改写或替换，不创建 envelope，不读取 `ui:` resource。现有 Direct Pi 测试覆盖 resource+text、
多 text、普通非 JSON text、structuredContent、空文本和 `isError=true`，并覆盖 Evidence delta=0、
单次 settlement、`error_type` 和 credential boundary。

因此本轮没有修改 `backend/app/mcp_gateway/datatap.py` 的 legacy 兼容分类，也没有恢复
Evidence Bridge/`mcp_result_v1`；legacy/current Run 的历史读取边界保持不变。

静态扫描仍能在 `backend/app/agent_runtime/tools/mcp.py` 的 legacy `AgentMcpTool`/Recovery 分支看到
`result_unavailable`，以及在 `backend/app/pi_gateway/completion.py` 看到历史 Snapshot 的
`required_artifact_*` 兼容读取；这是有意保留的历史边界。Pi Gateway 新路径只使用该模块的账务观察器
和 metadata 接口，标准 Result 在 Node adapter/session 内直接交给模型，未经过这些 legacy payload/Evidence
分支。

### 4.2 取消闭环改动

- `AgentRunRead` 与前端 `ApiAgentRun` 暴露 `cancel_requested`；前端 workspace 维护 `isCancelling`，
  API 返回取消请求后保持 SSE 订阅，直到真实终态事件。
- `PiGatewayService.preflight_mcp` 在 catalog、ToolCall、reserve 和 dispatch 之前检查持久取消标记；
  后端新增回归确认取消 Run 不产生新 ToolCall、不预留积分。
- terminal ACK 在取消标记已提交后统一按 `cancelled` 收口，避免迟到 `completed/failed` 越过 durable
  cancellation fence；Gateway heartbeat 在 terminalization 后的预期 404 不再伪报基础设施失败。
- Gateway 仍按既有实际发送事实执行 `definitely_not_sent`/`failed_confirmed`/`result_unknown`，
  provider/worker abort、SIGTERM→有界 SIGKILL、Recovery 和唯一 `run.cancelled` 语义未放宽。
- UI 文案为“取消任务”；running/reviewing 均可取消，点击后显示“正在取消”，不把 API 响应伪造成终态。

本修复没有删除完成校验、Artifact Schema、Publication、Version、lineage、allowlist、tenant/session/run
归属或 workbook 同版约束；也没有把 `analysis_report_v1` 变成固定 required artifact。

## 5. 验证记录

本节只记录本轮实际命令和结果。原 12 项与完整离线 UAT 均按授权顺序执行，不重复完整 UAT。

### 5.1 TDD 红灯与受影响定向回归

先观察到的红灯包括：取消 preflight 未拒绝、API DTO 缺少 `cancel_requested`、迟到 terminal ACK 收口为
`completed`、reviewing Run 卡片缺少取消动作，以及 Gateway heartbeat/terminal race 的
`control_plane_unreachable`。随后以最小实现修复并回归：

```text
Backend cancellation/Pi gateway targeted: 29 passed in 1.77s
Pi Gateway Vitest (5 files): 63 passed
Pi Gateway typecheck: passed
Frontend Vitest (6 files): 92 passed
```

前端定向命令为：

```bash
npx vitest run src/App.test.tsx src/components/ChatArea.test.tsx \
  src/components/agent/AgentRunCard.test.tsx src/hooks/useAgentWorkspace.test.tsx \
  src/api/agent.test.ts src/hooks/useRunHistoryReplay.test.tsx
```

根目录 `npx tsc --noEmit` 仍被基线已有的 `pi-runtime` 缺少 `pi-mcp-adapter`、
`@earendil-works/pi-coding-agent` 和 `typebox` 阻断；Pi Gateway 自身 typecheck 已通过。本轮没有
擅自安装或修改该不相关依赖面。

### 5.2 原 12 项一次执行

执行的是历史 QA 列出的 12 个 nodeid，命令形态为：

```bash
backend/.venv/bin/pytest -q \
  backend/tests/integration/test_pi_gateway_offline_uat.py::test_non_marketing_refusal_zero_side_effects \
  backend/tests/integration/test_pi_gateway_offline_uat.py::test_generic_proxy_bare_remote_name_billing_chain \
  backend/tests/integration/test_pi_gateway_offline_uat.py::test_generic_proxy_bare_name_unique_mapping_without_server \
  backend/tests/integration/test_pi_gateway_offline_uat.py::test_generic_proxy_ambiguous_remote_name_with_explicit_server \
  backend/tests/integration/test_pi_gateway_offline_uat.py::test_generic_proxy_unique_live_duplicate_dispatches_to_claimed_service \
  backend/tests/integration/test_pi_gateway_offline_uat.py::test_model_budget_two_decisions_complete \
  backend/tests/integration/test_pi_gateway_offline_uat.py::test_cross_tenant_isolation \
  backend/tests/integration/test_pi_gateway_offline_uat.py::test_drilldown_binds_exact_version_with_zero_datatap \
  backend/tests/integration/test_pi_gateway_offline_uat.py::test_draining_gateway_stops_new_claims_but_finishes_active \
  backend/tests/integration/test_pi_gateway_offline_uat.py::test_current_to_pi_to_current_and_kill_switch_only_affects_new_runs \
  backend/tests/integration/test_pi_gateway_offline_uat.py::test_sse_ordering_and_last_event_id_resume \
  backend/tests/integration/test_pi_gateway_offline_uat.py::test_run_snapshot_immutable_across_rollout
```

该次进程已自然结束，执行后 `backend/.pytest_cache/v/cache/lastfailed` 为 `{}`，没有记录失败 nodeid；
但由于首次受控提权调用的外层会话句柄未保留，pytest 的终端汇总和 exit code 没有回传。因此本记录不
把它伪记为有明确计数的全绿；第 2 节历史 12 项 `pi_gateway_main_artifact_missing` 红灯证据继续保留，
也没有删除用例或改写 expected。

### 5.3 完整离线 UAT 一次及唯一失败隔离

离线 UAT 使用隔离进程级 fake topology，无真实模型、DataTap、钱包或生产库外部请求；测试品牌已改为
`瑞幸咖啡`，`BRAND_SCOPE.keywords` 为 `['瑞幸咖啡']`。

```text
完整离线 UAT：27 passed, 1 failed in 411.12s (0:06:51)
失败：test_direct_artifact_self_correction_bounded
原因：拓扑登录初始化 POST /api/v1/auth/mock/sms/login 返回 HTTP 500，未进入业务断言
隔离复验（仅该用例一次）：1 passed in 12.63s
```

没有再次运行完整离线 UAT；该失败按 flake 记录，不改变生产逻辑或测试 expected。

## 6. 安全与兼容结论

- 标准 MCP Result 直通不是放宽安全：模型仍只能经审核工具、当前租户/Session/Run 归属和固定计费
  入口调用；凭证、Bearer、API Key、DSN 仍禁止进入模型和日志。
- 取消不是视觉假动作：持久 `cancel_requested`、preflight、heartbeat、worker abort、terminal fence、
  Recovery 和 SSE 共同决定真实终态；在飞调用不因取消被错误释放或重放。
- 历史 `required_artifact` 字段、旧 Snapshot、旧 Run、旧表和 legacy 读取不删除、不回写；新 Pi Run
  只执行通用“当前 Run 至少一个合法顶层主报告”的完成不变量，不消费固定 Artifact 类型。

## 7. 当前发布状态与后续停止门

当前状态仍为 `NOT_READY_FOR_CANDIDATE_DEPLOYMENT`，因为独立审查、线性提交、integration candidate、
CI、预发布和一次真实 Web UAT 尚未完成。本轮未执行生产灰度。

后续唯一真实 Web UAT 使用专用 UAT 租户和一次 `瑞幸咖啡` 请求，限制为 Run=1、Attempt=1、模型≤10、
DataTap≤5、积分≤50、retries=0、fresh Run=0；必须同时证明此前 `unsupported_content` 场景的标准
Result 到达模型、点击“取消任务”后无后续外发、唯一 `cancelled`、Attempt/ToolCall/permit/lease/
worker/端口收口。通过后状态只能推进到
`REAL_UAT_MCP_PASS_THROUGH_AND_CANCEL_PASS / READY_FOR_FINAL_FUNCTIONAL_UAT`，本轮不做生产灰度。
