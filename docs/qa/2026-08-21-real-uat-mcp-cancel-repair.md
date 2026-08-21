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

初始未安装依赖时，根目录 `npx tsc --noEmit` 被 `pi-runtime` 缺少 `pi-mcp-adapter`、
`@earendil-works/pi-coding-agent` 和 `typebox` 阻断；Pi Gateway 自身 typecheck 已通过。候选 CI
阶段仅安装 `pi-runtime/package-lock.json` 已锁定依赖，没有修改该兼容路径的源代码或生产桥接。

候选 CI 前置重新安装了 `pi-runtime/package-lock.json` 的锁定依赖（398 packages）；随后 root lint 和
Pi Runtime typecheck 均通过。更新后的现行 `revision` POC 契约断言也已通过；没有修改生产桥接或历史
Snapshot。

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

### 5.4 Candidate 与本地 CI

从包含 fixture/CI 修复的最新 HEAD 重建 `codex/real-uat-mcp-cancel-integration-r3`，并以 fast-forward
方式合入本地 `main`，当前合入 commit 为 `f3d3881`。候选 CI 现在安装锁定的 Pi Runtime 依赖，并把
一次性离线拓扑 UAT 从普通 CI 排除，避免重复执行本轮已经完成的完整离线 UAT；该 UAT 仍保留为单独的
发布门。

本地候选 CI（不含离线 UAT文件）结果：

```text
Backend production CI subset: 2180 passed, 29 skipped in 187.97s
Frontend full Vitest: 44 files, 322 passed
Pi Gateway full Vitest: 27 files, 188 passed
Root lint: passed
Pi Runtime typecheck: passed
Ruff / frontend build / Gateway typecheck+build / git diff --check: passed
```

本地完整 Browser E2E 在 `0bb65bc` 上执行 51 项，3 项失败且均为同一旧用例
`pauses an active run with the input pause button` 在三个视口的同一选择器错误：测试查找旧文案
“暂停”，页面实际已按取消契约显示“取消任务”。未出现第二类 E2E 失败。提交 `7b83d82`
仅将该用例改为取消语义；隔离执行该场景的三个视口结果为 `3 passed in 6.9s`。

推送 `0bb65bc` 后的远程 GitHub Actions run `32470724885` 已结束，但不是全绿：Node workspaces
Pi Runtime、Migration safety 成功；Backend、Frontend、Browser E2E、Pi Gateway 失败。失败 job 的
公开状态可见，但 GitHub API 下载日志返回 `403 Must have admin rights to Repository`，因此本轮不
猜测远端失败原因，也不把它记为通过。预发布部署与真实 Web UAT 继续等待远程 CI 全绿。

随后推送 `d53b345` 的 candidate-r5 run `32471771494` 也已结束为 failure。公开 annotations 给出：
Frontend 的两个 Blob 下载测试在 Node 22/jsdom 环境报 `object.stream is not a function`；Pi Gateway
的 `worker-entry.test.ts:246` 在 5 秒默认测试时限内超时；Browser E2E 与 Backend 仅有步骤级
`Process completed with exit code 1`，没有公开具体断言。对应的最小测试兼容修复已提交为 `ddc6a82`：
下载测试改用具备 `blob()` 的最小 Response double，worker 生命周期测试时限改为 15 秒；生产代码与
取消/直通逻辑未改。修复后的本地定向结果为 Artifact 下载 `12 passed`、worker 场景 `1 passed`。
由于 Backend/Browser 的具体日志仍不可见，不能把它们归因或宣称已修复；需由下一候选 CI 给出结果。

candidate-r6（`984dc90`）对应 Actions run `32472516897` 已结束为 failure。该 candidate 尚未包含
`7d487f6`，因此公开 annotation 仍是上一轮已知的 Frontend workspace 依赖缺失和 Gateway early
heartbeat 测试时序失败；Migration safety、Pi Runtime 成功，Backend 与 Browser 仍只有步骤级 exit code
1。随后在当前修复工作树执行完整 Browser E2E：`51 passed in 1.2m`；Gateway heartbeat 定向用例与
根目录 `npm run lint` 也通过。`7d487f6` 补齐 Frontend job 的 Pi Runtime/Gateway 锁定依赖，并让
该 Gateway 测试等待两次 heartbeat failure 的稳定调度窗口；它将由 candidate-r7 验证。

candidate-r7（`35ccc37`）对应 Actions run `32473243258` 已结束为 failure。公开状态显示 Migration
safety、Frontend、Pi Runtime、Pi Gateway 全部成功；Browser E2E 失败，根因为 Playwright 默认使用
`backend/.venv/bin/python`，而 CI Browser job 使用系统 `python` 安装依赖且没有创建该 venv，与此前
隔离工作树启动错误一致。提交 `e61b473` 在 Browser job 显式设置 `BACKEND_PYTHON=python`；本地完整
Browser E2E 已 `51 passed in 1.2m`。Backend 仍只有步骤级 `Pytest exit code 1`，没有公开测试名或
日志，未做猜测性修复。

candidate-r8（`cd2be3e`）对应 Actions run `32473941437` 已取得除 Backend 外的全绿：Frontend、
Migration safety、Browser E2E、Pi Runtime、Pi Gateway 均成功；Backend job
`96746314055` 的 Pytest 日志经已有登录态读取到完整失败摘要：

```text
1 failed, 2179 passed, 29 skipped, 1 warning in 259.16s
FAILED tests/integration/pi_uat/test_harness_lifecycle.py::test_topology_reaps_in_process_server_when_start_fails
subprocess.CalledProcessError: Command ['npm', 'run', 'build'] returned non-zero exit status 2
```

失败发生在离线拓扑测试的 `_ensure_gateway_built()`，Backend job 只安装了
`pi-runtime`，未安装 `pi-gateway` 依赖；因此该测试首次执行 Gateway build 时退出。该测试单独
复现及整个 `test_harness_lifecycle.py` 模块在本地分别为 `1 passed` 与 `10 passed`，说明不是
取消运行时断言失败。提交当前候选修复仅在 Backend job 增加锁定的 `pi-gateway/npm ci`，生产代码、
测试 expected、UAT 数据和运行时契约均未改；下一候选为 r9，尚未取得远程全绿。

## 6. 安全与兼容结论

- 标准 MCP Result 直通不是放宽安全：模型仍只能经审核工具、当前租户/Session/Run 归属和固定计费
  入口调用；凭证、Bearer、API Key、DSN 仍禁止进入模型和日志。
- 取消不是视觉假动作：持久 `cancel_requested`、preflight、heartbeat、worker abort、terminal fence、
  Recovery 和 SSE 共同决定真实终态；在飞调用不因取消被错误释放或重放。
- 历史 `required_artifact` 字段、旧 Snapshot、旧 Run、旧表和 legacy 读取不删除、不回写；新 Pi Run
  只执行通用“当前 Run 至少一个合法顶层主报告”的完成不变量，不消费固定 Artifact 类型。

## 7. 当前发布状态与后续停止门

独立只读审查已完成，结论为 Critical 0 / Important 0；本轮线性提交包括
`fd61e9e`（runtime）、`dbd2a96`（tests/UAT）、`de7c45b`（docs）、`2644065`（fixture）、
`f3d3881`（CI）、`0bb65bc`（candidate CI 边界）、`7b83d82`（E2E 取消语义）、`ddc6a82`
（Node CI 测试兼容）、`984dc90`（r6 失败证据）、`7d487f6`（workspace 依赖/heartbeat 测试）和
`e61b473`（Browser runner Python）。
候选已合入本地 `main`，但远程 GitHub CI 当前失败、预发布和一次真实 Web
UAT 尚未完成，因此此前的
`NOT_READY_FOR_PREDEPLOYMENT` 已推进为 `REAL_UAT_PRECHECK_FAILED /
NOT_READY_FOR_FINAL_FUNCTIONAL_UAT`。candidate-r9 已全绿并完成预发布部署，
但本轮唯一真实 Web UAT 在 Run 创建前失败；本轮未执行生产灰度。

后续唯一真实 Web UAT 使用专用 UAT 租户和一次 `瑞幸咖啡` 请求，限制为 Run=1、Attempt=1、模型≤10、
DataTap≤5、积分≤50、retries=0、fresh Run=0；必须同时证明此前 `unsupported_content` 场景的标准
Result 到达模型、点击“取消任务”后无后续外发、唯一 `cancelled`、Attempt/ToolCall/permit/lease/
worker/端口收口。通过后状态只能推进到
`REAL_UAT_MCP_PASS_THROUGH_AND_CANCEL_PASS / READY_FOR_FINAL_FUNCTIONAL_UAT`，本轮不做生产灰度。

## 8. 历史 Candidate-r9 与唯一真实 Web UAT（2026-08-21）

### 8.1 历史 Candidate-r9 与预发布部署

- `3c01d13` 已合入并推送 `main`；GitHub Actions run `32476331375` 全绿：Backend
  `96753338631`、Frontend `96753338475`、Migration `96753338762`、Browser E2E
  `96753338598`、Pi Runtime `96753338567`、Pi Gateway `96753338660` 均成功。
- 远端预发布同步使用同一 `3c01d13`，Alembic head 为 `0049_skill_rollout_history`；后端健康检查为
  `{"status":"ok","service":"kol-insight-api"}`，Pi Gateway `/healthz` 与 `/readyz` 分别为
  `{"status":"ok"}` 与 `{"status":"ready"}`。后端和 Gateway systemd 均为 active。
- 专用 UAT 租户为 `uat-pi-r9-20260821`（瑞幸咖啡），配置为 `runtime_backend=pi`、
  `max_decisions=10`、DataTap 每次 10 积分；钱包余额 1000、预留 0。未改生产库，未改历史 Run、
  Snapshot、Version 或迁移。

### 8.2 历史唯一瑞幸咖啡 Web UAT 结果

- 仅使用专用 UAT 用户提交一次请求：
  `请分析瑞幸咖啡近30天的小红书声量、互动与情感趋势，并给出可执行的KOL选择建议；如数据受限请明确说明，不要把缺失值当作0。`
  未点击重试、继续、取消，也未创建第二个请求。
- 页面错误为 `runtime_adapter_catalog_too_large`。调用路径是新 Run 创建时的运行时配置快照预检：
  `RuntimeConfigService` 读取当前租户 enabled+approved MCP 目录，并在超过 Pi adapter 目录上限 32
  时拒绝创建快照。该路径早于模型调用、Gateway claim 和 DataTap 外发。
- 远端只读数据库证据：该专用会话、消息、Agent Run、Attempt、Tool Call 均为 0；enabled+approved
  目录共 58 个，分组为 `insight-cube-mcp=24`、`social-grow-mcp=16`、
  `social-grow-content-mcp=10`、`bilibili-mcp=8`。因此本次没有模型请求、DataTap dispatch、
  permit/积分扣费或取消竞态。
- UAT 在目标 MCP 透传与取消断言之前停止，不能宣称 `REAL_UAT_MCP_PASS_THROUGH_AND_CANCEL_PASS`。
  当前状态为 `REAL_UAT_PRECHECK_FAILED / NOT_READY_FOR_FINAL_FUNCTIONAL_UAT`；生产灰度、合入生产、
  5% → 25% → 100% 均禁止。

### 8.3 历史边界与后续授权

- 未通过关闭任意工具、把 allowlist 改成任意字符串、放宽目录上限、测试特判或重跑 Web UAT 来掩盖
  失败；原 12 项红灯和完整离线 UAT 证据保持不变。
- 租户初始化时新钱包调整路由暴露既有 `McpPreflightContext` 长度校验错误；事务已回滚，随后使用
  既有兼容 Admin 调整 API 完成同一租户钱包初始化，仍经过钱包账本、幂等和审计边界。该 setup 问题
  未在本修复中改动。
- 若要处理 58>32 的租户目录配置并重新验证，必须取得新的明确 Web UAT 授权；在此之前不得修改目录、
  创建 Run 或进入生产发布。

## 9. Pi Adapter Catalog 容量修复与候选部署（2026-08-21）

### 9.1 根因与架构决定

- 历史 `32` 是 `RuntimeConfigService`、Pi Gateway contract/parser 的 control-plane catalog 防御性上限；
  它不是 Pi SDK、模型可见工具数或业务服务 allowlist。58 个 `enabled+approved` 条目在 Run 创建前的
  Snapshot 生成阶段触发 `runtime_adapter_catalog_too_large`，因此此前没有创建 Session/Run，也没有模型、
  DataTap 或计费副作用。
- 最小修复将有界容量提升为 **128 entries + canonical JSON 128 KiB**。新 Run Snapshot 保留全部 58 项，
  不截断、不分页、不按 Profile/用户文本筛选，也不选择单一业务工具；没有数据库迁移、工具状态变更或
  allowlist 放宽。
- `quarantined`、`unknown`、`query_user_info` 不进入目录；字段完整性、schema digest、重复身份、敏感字段、
  DTO/parser 对称校验全部保留。Pi 仍使用单一 MCP proxy，`directTools=false`、`scriptMode=false`、
  output guard 与固定计费/归属边界不变；58 项由四个 MCP server 分发。
- 该修复不改变正式 Artifact Schema、Publication、Version、lineage、tenant/session/run 归属或通用完成
  不变量；历史 `required_artifact` 字段、旧 Snapshot/Run/Version 仅按原语义兼容读取，不回写。

### 9.2 TDD、定向验证与候选 CI

- 红灯提交：`319f6d0 test(pi): reproduce reviewed adapter catalog capacity mismatch`，锁定 32/33/58/128
  边界、129/128 KiB 拒绝、过滤、排序、重复身份、Snapshot 不可变、DTO/parser 对称、四 server/resource
  loader、`directTools=false`/`scriptMode=false`/output guard 与首尾计费归属行为。
- 修复提交：`0615533 fix(pi): raise bounded adapter catalog capacity`。受影响 Backend 测试 `47 passed`，
  Pi Gateway 定向 Vitest `56 passed`，Ruff、Pi Gateway typecheck、build 均通过；`git diff --check`、敏感信息/DSN
  扫描与无迁移扫描通过。
- 唯一候选 CI 为 GitHub Actions `32480577421`（HEAD `0615533c5f65bfd55c57fbbd181fbfa622c13282`），
  Backend、Frontend、Migration safety、Browser E2E、Pi Runtime、Pi Gateway 全部成功。独立只读审查结果为
  Critical=0 / Important=0。

### 9.3 预发布部署与只读 Snapshot 证据（Web UAT 前）

- 已将精确候选 HEAD `0615533c5f65bfd55c57fbbd181fbfa622c13282` 部署到 UAT；后端备份为
  `/home/kol_insight/backups/backend-before-0615533.tar.gz`。后端与 Pi Gateway systemd 均 active，
  `/healthz`、`/readyz` 正常，Alembic head 仍为 `0049_skill_rollout_history`。
- 对专用租户 `uat-pi-r9-20260821` 执行了一次只读、回滚事务的 `RuntimeConfigService` 检查：新 Pi
  `session_analyst_v1` Snapshot 中完整存在 58 项，分布为 `insight-cube-mcp=24`、`social-grow-mcp=16`、
  `social-grow-content-mcp=10`、`bilibili-mcp=8`；catalog digest 为
  `1467342826d397c8dfb3653b476e3612ded999b5ada957a401b2a7c56fbd541f`。未创建 Session/Run，未改写历史
  Snapshot/Version/迁移数据。
- 在 Web UAT 前曾发现浏览器会话是 `个人租户-手机用户_7961`，不是专用 UAT 租户，因此没有向错误租户提交请求；
  随后已切换并核对专用账号。真实 Web UAT 的最终结果见 §9.5；生产未部署、未合入生产、未执行灰度。

### 9.4 下一道停止门（执行前计划，已由 §9.5 结果取代）

切换到专用账号并完成即时认证确认后，只执行一条固定瑞幸咖啡请求、一个新 Session/Run（Run=1、Attempt=1、
模型≤10、DataTap≤5、积分≤50、retries=0、fresh Run=0），验证标准 MCP Result 到达模型后点击“取消任务”，
并核对无后续外发、唯一 `cancelled`、Attempt=1、ToolCall/permit/lease/worker/端口清理。自然完成时不补建
第二个 Run，只记录取消未测。通过后才可记录 `REAL_UAT_MCP_PASS_THROUGH_AND_CANCEL_PASS /
READY_FOR_FINAL_FUNCTIONAL_UAT_REVIEW`；在此之前不得合入 main、生产部署或 5%→25%→100% 灰度。

### 9.5 唯一真实 Web UAT 实际结果与停止结论

- 已切换并核对专用账号 `UAT 瑞幸咖啡 Tester`，新建唯一干净 Session
  `81bfb306-aa94-44d0-913e-c6c66997f356`，只发送一次固定瑞幸咖啡请求，对应唯一 Run
  `f5b7f6d9-0a45-4f3e-8373-bf183a26e494`；未点击重试、继续或第二次发送。
- 精确候选 `0615533` 首次部署后，Gateway claim 连续返回 409；只读复现为
  `PiGatewayClaimResponse` canonical 校验接收服务端 Pydantic DTO 时未先转 JSON mapping，错误为
  `pi_gateway_claim_catalog_invalid`。这不是 58 条目容量失败，也未产生 Attempt/ToolCall/积分副作用。
- 以 TDD 补充 `test_claim_accepts_service_owned_catalog_models`，最小修复提交为
  `5682b6a test(pi): cover claim DTO catalog normalization` 与
  `fc4e70c fix(pi): accept service-owned adapter catalog DTOs`；受影响 Backend 定向回归为 `48 passed`，
  Ruff 与 diff 检查通过。该 post-CI 热修复未重跑候选全套 CI，遵守本轮“candidate CI 只执行一次”的授权边界。
- 热修复同步并重启 UAT 后，同一个 Run 被 Attempt 1 领取并产生 `run.started`、模型 thinking/message 事件及
  3 个内部工具 `tool.started/tool.succeeded` 事件；没有外部 `AgentToolCall`，没有 DataTap dispatch，也
  没有标准 MCP Result 到达模型。随后 Run 以 `pi_model_provider_error` 失败，未进入取消验证，因此取消状态
  记录为“未测”，不伪造为通过。
- 最终只读收口：Run=`failed`、Attempt 1=`failed`、`decision_count=0`、`AgentToolCall=0`、Run 账务记录=0、
  wallet reserved=0、Run lease/active_run 均为空；Gateway 无残留 worker，仅保留主进程和 8100/9471 健康监听。
- 当前状态为 `REAL_UAT_CATALOG_CLAIM_FIXED_PROVIDER_FAILED / NOT_READY_FOR_FINAL_FUNCTIONAL_UAT`。
  该结果不能推进到 `REAL_UAT_MCP_PASS_THROUGH_AND_CANCEL_PASS`；不合入 main、不部署生产、不执行
  5%→25%→100% 灰度，也不再重试本次 Web UAT。该历史记录当时未包含 provider 诊断授权；当前续作见 §10。

## 10. Provider 失败安全可观测性（2026-08-21，当前修复）

### 10.1 旧 Run 的根因边界

- 唯一真实 Run `f5b7f6d9-0a45-4f3e-8373-bf183a26e494` 已终态失败，禁止恢复、修改或复用；该 Run 的
  DataTap、AgentToolCall 和扣费均为 0。
- 旧路径只把 `message.stopReason="error"` 记为布尔值，`PiModelProviderError` 没有分类，Child IPC
  只发送 `errorCode`，且 Child stdout/stderr 被设为 `ignore`。因此原始 provider `errorMessage` 已不可
  恢复，不能从旧 Run 猜测鉴权、限流、上下文或协议根因。

### 10.2 新契约与安全边界

新增严格 `provider_failure_v1` metadata-only DTO：`version`、枚举 `failure_class`（含
`authentication`、`authorization`、`rate_limited`、`model_not_found`、`invalid_request`、
`context_length`、`timeout`、`network`、`upstream_5xx`、`aborted`、`unknown`）、可选 100–599
`http_status`、可选安全字符且不超过 128 的 `provider_request_id`、64 个十六进制字符的 SHA-256
`error_fingerprint`、可选毫秒 UTC `observed_at`。未知情况统一为 `unknown`。

- 只从 SDK `AssistantMessage.errorMessage` 在内存中提取有限分类、状态和 request id；原文只用于哈希，
  不进入 Error message、IPC、HTTP、数据库、事件或日志。
- 严格 DTO、Child IPC 帧、Gateway terminal 和 FastAPI terminal 均执行 exact-key、长度、枚举、状态码、
  request id 与 fingerprint 校验；元数据只能和 `pi_model_provider_error` 绑定。
- terminal 业务错误码保持 `pi_model_provider_error`；provider 失败不自动重试、不创建基础设施 Attempt 2。
  SDK `aborted` 与用户取消仍走 `cancelled`；取消栅栏抢先时不会持久化 provider metadata。
- Recovery 继续按原有 durable terminal 语义处理 terminal ACK 丢失；已落库的 provider failed 不会重新
  排队。没有对 provider 请求协议做猜测性修改。

### 10.3 TDD 与当前停止边界

- 已锁定 400/401/403/404/422/429、上下文窗口、timeout、network、500/502/503/529 与 unknown 分类，
  以及 secret/Bearer/prompt/response body 不出现在任何投影中的断言。
- 已锁定 IPC 篡改、额外字段、非法状态码、非法 request id、错误码绑定、Run ID 不一致 fail-closed，
  provider failed 不产生 Attempt 2，以及 cancel/aborted 保持 cancelled 的断言。
- 受影响定向验证已通过：Pi Gateway 9 个测试文件/100 项、Backend 34 项；Gateway typecheck、build、
  Backend Ruff、`git diff --check` 与生产代码 secret/DSN/Bearer 扫描通过。独立只读复核为
  Critical 0 / Important 0 / Minor 1；Minor 是 ACK-loss 测试未模拟真实传输层断响应，不改变终态语义。
- provider 探针 A/B、候选 CI、预发布部署和新的 Web UAT 已按授权执行；实际结果与停止结论记录于 §10.4。

### 10.4 实际诊断探针与唯一 Web UAT 结果（2026-08-21）

- 独立审查后只推送一次候选 `31796539b3297941fe1d4be48ffae5437d773b37`（分支
  `codex/runtime-adapter-catalog-capacity-repair`），GitHub Actions `32485676754` 的 Migration safety、Frontend、
  Browser E2E、Pi Runtime、Backend、Pi Gateway 六个 Job 全部成功。预发布只部署该精确 HEAD；后端备份为
  `/home/kol_insight/backups/backend-before-3179653.tar.gz`，后端 `8100/healthz`、Pi Gateway `9471/healthz` 与
  `/readyz` 正常，systemd 服务均 active。
- 探针 A 使用 `tencent-plan / glm-5.2` 和预发布实际 Base URL，`retries=0`，1 次最小文本请求成功，
  `stop_reason=stop`。探针 B 使用同一 provider/model，2 次请求完成 assistant tool_call → 进程内 no-op
  tool result → 下一轮响应，分别为 `toolUse`、`stop`，成功；no-op 未调用 MCP、数据库或生产工具。探针合计模型
  请求 3 次、DataTap=0、钱包=0，未发现鉴权、限流、上下文、invalid_request 或上游 5xx 错误。
- 探针后对专用租户 `uat-pi-r9-20260821` 做只读核验：Snapshot 仍为 58 项，digest
  `1467342826d397c8dfb3653b476e3612ded999b5ada957a401b2a7c56fbd541f`，canonical bytes=16241；分布为
  `insight-cube-mcp=24`、`social-grow-mcp=16`、`social-grow-content-mcp=10`、`bilibili-mcp=8`。
- 按唯一 Web UAT 授权使用账号 `UAT 瑞幸咖啡 Tester`，新建 Session
  `80d5537b-6bfa-423b-aeb0-17ea07f41099`，只提交一次固定瑞幸咖啡请求。系统自动产生的 utility 标题 Run
  `1f7f65b7-da74-4708-8113-e311ebe47337` 与业务 Run 分开计，不是业务补测；业务 Run 为
  `8e711362-638a-461f-9cb4-2896e81d1ccd`。
- 业务 Run 在标准 MCP Result 到达模型前触发 Gateway `event_buffer_overflow`；Attempt 1 失败后 Recovery
  自动创建 Attempt 2。发现已超过本次 `Attempt=1` 预算且尚未获得 MCP Result 后点击一次“取消任务”，最终
  Run=`cancelled`、Attempt 2=`cancelled`，无 Attempt 3；取消后的 lease/worker 收口，未发生后续 DataTap 外发。
  期间还记录了 `control_plane_unreachable`，该错误属于终态事件传输/收口问题，不是 provider 分类证据。
- 业务 Run 只产生 2 条模型 usage 记录；`AgentToolCall=0`、DataTap dispatch=0、钱包账务=0、最终 reserved=0；
  没有标准 MCP Result 原样到达模型，也没有可用于证明 Direct MCP pass-through 的 durable Evidence。
- 因 Attempt 预算被 Recovery 路径突破且 MCP Result 门槛未满足，本轮不能宣称取消闭环通过，状态收口为
  `REAL_UAT_EVENT_BUFFER_OVERFLOW_CANCELLED / NOT_READY_FOR_FINAL_FUNCTIONAL_UAT`。不创建第二个 Web UAT，
  不合入 `main`，不部署生产，不执行 `5% → 25% → 100%` 灰度；`event_buffer_overflow` 是后续修复的真实运行时阻断。

## 11. Pi Gateway 事件投递 backpressure 修复（2026-08-21，仅离线/定向验证）

### 11.1 旧行为复现与初始分类

- 历史复现用例为 `PiGateway > baseline: reproduces event overflow when transient delivery failure waits for heartbeat recovery`。
  它使用默认生产 heartbeat interval，在第一次事件投递发生网络错误后同步注入 257 个事件；修复前观测到生产者
  一次突发 257、首次 ACK=0、队列高水位=256、事件重试=0、heartbeat=0、terminal=0，旧路径随后 abort 并以
  `event_buffer_overflow` 收口。该用例在修复后的套件中保留为历史证据并标记 skip，不删除、不改写为通过。
- 初始分类确定为 **事件生产/内存缓冲/控制面 ACK 与恢复路径**，不是 provider、MCP、钱包、积分或 terminal
  业务判断：失败发生在标准 MCP Result 到达模型之前，且旧 UAT 的 DataTap dispatch、AgentToolCall 与扣费均为 0。

### 11.2 最小修复与安全边界

- 新增独立串行 `EventDeliveryPump`：事件生产与 heartbeat 解耦；单批最多 32 条、canonical JSON 最多 128 KiB，
  同一 Run/Attempt 使用连续 source sequence；批次在 ACK 前保留，timeout/network/5xx 只对同一批做有界短退避重试，
  最多 5 次重试，并受 lease、cancel、shutdown fence 约束。4xx、业务拒绝、协议/序列错误不盲重试。
- 后端 batch DTO、HMAC/nonce、lease、归属和事务接收各只执行一次；完整批次先校验再原子写入，commit 后按可见 sequence
  发布。ACK 丢失时重放完全相同的 source batch，依靠 source identity 幂等，不重复写 AgentEvent、RuntimeUsageRecord、
  assistant message 或 SSE。旧 `/events` 单事件端点仍保留兼容路径。
- terminalization 先等待已提交事件 drain；永久控制面不可用时 fail-closed、终止 worker 并把 Run 留给既有 Recovery，
  不伪造 terminal、不关闭 Recovery、不直接禁止 Attempt 2。取消 backlog 则优先在取消 fence 下有界收口，只有事件 ACK 后才发送
  `run.cancelled`。
- 诊断仅允许 `event/event_batch/heartbeat/terminal`、失败/重试/ACK/overflow、失败分类、合法 HTTP 状态、队列深度/高水位、
  批大小、最后 ACK source sequence、连续失败数和 latency bucket；不含 payload、prompt、MCP result/arguments、模型输出、
  用户数据、secret、Bearer、HMAC 或 DSN。健康指标新增 `event_delivery_failures_total`、
  `event_delivery_retries_total`、`event_buffer_overflows_total`、`event_queue_high_water`、
  `event_last_acked_source_sequence`。

### 11.3 修复后定向证据

| 指标 | 修复前历史复现 | 修复后离线/定向结果 |
|---|---:|---:|
| 生产者 | 257 条同步突发 | 257 条同步/分段注入均进入 pump，未触发 overflow |
| ACK 吞吐 | 首次网络失败后为 0 | 65 条顺序泵测试最终 ACK=65；257 条 Gateway 测试 drain 后 terminal=1 |
| 批次 | 无 batch，单事件失败即阻塞 | 每批 ≤32、同批重试；canonical body ≤128 KiB |
| 事件高水位 | 256 后 overflow | 有界为 active batch+pending queue，不扩大 256 上限 |
| 重试 | 0 | 一次网络失败后同批重试并 ACK；永久网络故障为 6 次调用（初次+5 次重试）后 fail-closed |
| 终态顺序 | terminal=0 | 正常 drain 后 terminal=1；取消测试确认 sequence=4 ACK 在 `cancelled` 前 |

以上是确定性测试计数/桶值，不是线上吞吐或 SLA 声明；真实 UAT 不在本轮执行。

### 11.4 TDD、回归与停止边界

- 线性提交为 `f78c5ea`（历史 RED 复现）、`81d4bf9`（batch contract RED/边界）、`c300ca2`（batch 接收与幂等）、
  `60c47e8`（独立 pump、诊断、取消/terminal drain）和 `edacdfb`（协议常量及 failure 诊断计数收紧）。
- 已通过 Gateway 定向 Vitest：7 个文件、65 passed、1 skipped（历史复现；另含本地子进程级 257 事件回归）；Gateway typecheck、build 通过；Backend
  Pi Gateway 定向 pytest 28 passed；受影响 Ruff、`git diff --check` 通过。验证覆盖批次顺序、ACK-loss 同批重放、gap
  全批拒绝且无部分写入、duplicate receipt 稳定、4xx/协议不重试、network/5xx 有界重试、永久故障留给 Recovery、
  terminal drain、取消 backlog、旧单事件端点、事件/usage 顺序和既有 Recovery/terminal 回归。
- 本轮未运行完整 backend pytest、完整离线 UAT、Browser E2E、历史 corpus replay、真实模型/provider、DataTap、钱包、
  Web UAT、候选 CI、push、预发布部署、main 合入、生产部署或灰度；未修改 provider 请求协议、DataTap/catalog/积分、
  Artifact/required-artifact、数据库迁移、历史 Run/Snapshot/Version。
- 当前最高状态仍待独立只读审查确认后才可推进为 `READY_FOR_SINGLE_REAL_WEB_UAT_REAUTHORIZATION`；本节本身不授权
  再次 Web UAT。
