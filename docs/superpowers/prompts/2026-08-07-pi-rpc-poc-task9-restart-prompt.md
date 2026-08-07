# Codex 提示词：解除积分预检阻断并重启 Task 9

```text
继续现有 Pi RPC POC 开发，不创建新方案，不进入方案 B/C。

工作树：/Users/hanxiang/Works/Projects/codex/mcn-app/.worktrees/codex-pi-runtime-poc
分支：codex/pi-runtime-poc
当前审计提交：
- cd4d2d3 fix: support multi-service datatap poc config
- 15b2503 docs: record pi rpc poc gate result

先完整读取：
1. AGENTS.md 与最新 3 篇 changelog
2. docs/superpowers/specs/2026-08-07-pi-agent-runtime-integration-design.md
3. docs/superpowers/plans/2026-08-07-pi-rpc-poc.md，重点是新增 Task 8A 和修订后 Task 9
4. docs/qa/pi-runtime-poc-rounds.md

设计方已经确认：15b2503 记录的不是 Pi 效果 Gate FAIL，而是旧计划自相矛盾造成的
Gate A BLOCKED / NOT RUN。它没有调用模型、没有执行六案例、没有创建真实 round。

正确积分边界：

- 积分不参与 POC 效果评价，且绝不接触真实用户钱包。
- Pi 路径保持完全无钱包，Pi MCP ToolCall 的 points_reserved/points_settled 恒为 0。
- Current Runtime 必须保持生产原样，包括 AgentMcpAccounting → WalletService 的
  reserve/settle/release；不得修改、mock、绕过或新增“无积分 Current”分支。
- Current 仅使用 kol_insight_pi_poc 中每 runtime/case 独立的一次性测试钱包，初始余额
  固定 10,000。钱包流水只证明 Current 基线真实运行，不参与 Gate 指标。

先使用 superpowers:systematic-debugging 复核根因，再使用 superpowers:test-driven-development
严格执行计划中的 Task 8A。必须先红后绿，并完成以下结果：

1. PocCaseFactory 构造器接收真实 model_name 和 current_wallet_balance=10_000。
2. 两种 runtime 的一次性用户都创建 IdentityService.default_channels 对应的启用权限。
3. 只有 runtime=current 创建 Wallet(balance=10_000,reserved=0,version=0)；Pi 用户没有 Wallet。
4. 两侧 AgentRun.profile_version 都是 "v1"，model 都是 settings.tencent_plan_model。
5. prompt_snapshot_json 只记录 billing_mode：Current=native_isolated_wallet，Pi=disabled。
6. _collect_case_result 可记录 Current/Pi 的 points_settled/points_reserved 诊断值，但
   assess_gate_a 不得使用积分决定 PASS/FAIL 或 improved_metric_count。
7. 不修改 AgentMcpAccounting、WalletService、AgentMcpTool 和 DataTap 直连透明 Hook。
8. 在 docs/qa/pi-runtime-poc-rounds.md 追加设计方复核，不覆盖原记录：把旧结论重分类为
   BLOCKED / NOT RUN，并说明重新开启条件。

Task 8A 聚焦验证命令：

cd backend
.venv/bin/pytest tests/pi_runtime_poc/test_comparison.py tests/agent_runtime/tools/test_mcp.py -q
.venv/bin/pytest tests/pi_runtime_poc -q
.venv/bin/ruff check app/pi_runtime_poc tests/pi_runtime_poc scripts/run_pi_runtime_poc.py

确认全部通过、git diff 无任何密钥后，提交：

git commit -m "fix: unblock isolated current runtime poc baseline"

然后重启 Task 9：

1. 再次确认 APP_ENV=test、MYSQL_DATABASE=kol_insight_pi_poc、PI_RUNTIME_POC_ENABLED=true；
   只显示 key/endpoint mapping 是否存在，不显示值。
2. 确认 8000 没有来源不明的服务、没有运行中的 POC、输出 round id 尚不存在。
3. 运行且只运行一轮：
   cd backend && bash scripts/run_pi_runtime_poc.sh --case all --runtime both
4. 不因供应商错误重复运行来掩盖波动；保留本轮真实结果。
5. 按计划完成 openpyxl 非视觉 Excel 检查和盲评可读性；禁止 LibreOffice、截图或视觉审核。
6. 只有六场景真实执行并创建 round 后才能写 Gate A PASS/FAIL。若仍在真实调用前阻断，写
   BLOCKED / NOT RUN；不要再次误记为效果 FAIL。
7. 完成全量验证、QA/changelog 和独立 commit 后停止，不进入方案 B/C。

先回复你对“Pi 无钱包、Current 原生计费但只用隔离测试钱包”的理解，然后从 Task 8A 的
失败测试开始，不要直接跳到真实 Task 9。
```
