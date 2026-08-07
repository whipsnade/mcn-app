# Pi-only Gate A 修订设计

> 状态：已确认，待实施计划
>
> 日期：2026-08-08
>
> 适用工作树：`codex/pi-runtime-poc`
>
> 本文覆盖 `2026-08-07-pi-agent-runtime-integration-design.md` 中方案 A 的 Current/Pi
> 对比口径，以及 `2026-08-07-pi-rpc-poc.md` 中 Task 9 的双 Runtime 执行与相对指标。
> 原文其余架构边界继续有效。

## 1. 决策与目标

方案 A 不再证明“Pi 优于 Current Runtime”，只证明 Pi 能否在现有 FastAPI 可信控制平面、
DataTap MCP、Evidence、Builder、Artifact、Excel 和 BI 契约下独立完成营销业务闭环。

Task 9 只执行 Pi，不创建 Current Run、一次性 Current Wallet 或 Current 结果文件，不再计算
覆盖率不低于 Current、指标优于 Current 等相对指标。生产 Current Runtime、现有用户路径和
方案 B 的灰度回滚能力不在本修订的删除范围内；本次只移除方案 A 验收 Harness 中的 Current
执行和比较逻辑。

## 2. Task 8E：真实 Gate 前的基础设施收口

Task 8E 必须在新的真实 Task 9 前完成，且其本地验证不得调用模型或 DataTap。

### 2.1 统一 Run 内序号分配

对所有基于 MySQL `REPEATABLE READ` 长会话的 per-run 序号分配做一次完整审计：

- `AgentRunAttempt.attempt`
- `AgentStep.sequence`
- `AgentEvent.sequence`
- 普通事件、Thinking 聚合事件、Tool start/settle/fail 事件和终态事件

统一约束为：先对 `AgentRun` 执行 `SELECT ... FOR UPDATE`，再以锁定当前读读取最新序号，写入
子表，并在同一事务提交。禁止用普通一致性读的 `max(sequence)`；不得通过捕获唯一键异常后重试
掩盖旧快照问题。

必须新增真实 `kol_insight_pi_poc` MySQL 回归，精确覆盖：Runner Session A 先建立旧快照，
Extension HTTP Session B 提交 Step、`tool.started` 和 `tool.succeeded`，Session A 随后继续写
RPC Step/Event。Attempt、Step、Event 都必须严格递增且不冲突。

### 2.2 六场景相互隔离

Harness 以 `(case_id, runtime=pi)` 为独立结果单元。任何案例失败都不得阻止无依赖案例继续执行。

产物钻取仅依赖 Pi 品牌报告：若品牌 Artifact 不可用，钻取写入
`status=skipped_dependency` 和稳定原因 `poc_dependency_artifact_unavailable`，但范围澄清与
非营销拒答仍必须执行。无论成功、失败或跳过，round 都必须在 `finally` 中生成完整
`summary.json`，并列出全部六个案例的状态；不得因异常只留下半轮目录。

### 2.3 Pi-only Harness

Task 9 入口只构建 `PiRuntimeCaseExecutor`。不调用 `create_agent_runtime()`，不刷新仅供 Current
基线使用的执行器，不创建 Current Wallet，不生成 `current.json`。CLI 的 Task 9 正式用法固定为：

```bash
cd backend && bash scripts/run_pi_runtime_poc.sh --case all --runtime pi
```

若保留 `--runtime` 参数用于兼容，它在 Task 9 中只接受 `pi`；`current` 和 `both` 必须在任何
Run、输出目录或外部调用创建前 fail-closed。历史 Current/Pi round 保留，不覆盖、不改写。

## 3. Pi-only 六个验收场景

1. 品牌调研：发布 `brand_report_v3`，并能从同一 Artifact Version 导出 Excel、构建 BI 数据与
   营销建议。
2. 活动评估：发布 `campaign_report_v2`，并完成同版 Excel、BI 与营销建议。
3. 达人圈选：发布 `kol_selection_v3`，并完成同版 Excel、BI 与营销建议。
4. 产物钻取：绑定本轮品牌报告的精确 Artifact Version，基于既有 Artifact/Evidence 回答；该固定
   fixture 不得重新调用 DataTap，也不得发布一份重复的完整品牌报告。
5. 范围澄清：输出 `clarification_requested`，不发布 Artifact，不调用 DataTap。
6. 非营销拒答：明确系统仅提供社媒营销能力，Run 正常完成，不发布 Artifact，不调用 DataTap。

三个报告场景允许供应商缺失导致 `partial`，但必须保留 Evidence lineage、availability、limitations
和实际尝试记录，不得编造或把缺失值转为零。

## 4. Gate A 绝对验收标准

### 4.1 硬门槛

- `summary.json` 含六个且仅六个 Pi 案例结果。
- 三个主报告均成功发布所需 Artifact Version；对应 Excel 可由 `openpyxl` 打开，必需 Sheet、关键
  数据类型、图表对象和 Artifact id/version 元数据正确；BI 与 Excel 消费同一 Version。
- 所有已发布数值可追溯到 available Evidence；`partial` 明确披露缺失和限制。
- 钻取引用精确的品牌 Artifact Version，DataTap ToolCall 为 0，且不产生重复完整报告。
- 澄清与非营销案例均为 0 DataTap ToolCall、0 Artifact；行为符合 §3。
- MCP 错误原样回喂 Pi；Harness 不改参、不换工具、不自动重放 unknown 调用。
- round 结束后可执行态 Run 为 0、running/reserved ToolCall 为 0；unknown 调用只记录、不重放。
- Pi 路径 Wallet 行、WalletTransaction、积分预留和结算均为 0。
- 输出、诊断、事件和 Git diff 的密钥扫描通过。
- 报告人工评审按事实性、洞察、可执行性、限制披露四项分别评分，每项均不低于 3/5，并记录理由。

### 4.2 Gate 状态

- `BLOCKED`：在任何真实模型或 DataTap 调用前被配置、迁移或启动条件阻断。
- `INFRA_FAILED`：已经开始真实执行，但 Pi RPC、审计、状态机、Harness 或输出收口失败，无法完成
  六场景效果评价。
- `EVALUATED_FAIL`：六场景均有终态结果和完整 summary，但一项或多项业务/质量硬门槛不通过。
- `PASS`：六场景完整，且 §4.1 全部通过。

`INFRA_FAILED` 不得表述为 Pi 分析效果失败；`EVALUATED_FAIL` 才表示 Pi 已被完整评价但结果不达标。

## 5. 执行与失败边界

- Task 8E 的真实 MySQL 拓扑回归通过后，直接执行一次 Pi-only 六场景 Task 9，不再额外重复执行
  品牌 canary。六场景必须使用新的 append-only 目录，不能覆盖历史结果。
- Harness 层不自动重跑失败 Run。模型可以在同一 Run 内依据 MCP 原始错误自主调整，这是 Agent
  行为，不属于 Harness 重试。
- 依赖案例失败只记录 `skipped_dependency`；其他案例继续。进程级异常也必须尽力收口已有 Run、
  将悬挂调用迁移为 unknown，并生成完整 summary。
- Task 9 不运行与本次后端/Node POC 无关的前端测试。Task 8E 提交前运行相关 POC pytest、显式
  MySQL 回归、Pi Runtime Vitest/typecheck、Ruff 和 `git diff --check`；全量回归留在合并前执行。

## 6. 输出与后续

每个案例只生成 `pi.json`；round 的 `summary.json` 记录案例状态、Artifact Version、Evidence、
ToolCall、数据覆盖、质量评分和 Gate 状态。QA 与 changelog 只记录脱敏事实。

只有 Pi-only Gate A `PASS` 才进入方案 B 详细计划。方案 B 仍保留租户级灰度与 Current Runtime
回滚能力；待 Pi 链路稳定一个发布周期后，再提醒用户评估已单独记录的方案 C Marketing MCP
Gateway，未经确认不得实施。
