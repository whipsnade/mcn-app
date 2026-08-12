# Scenario 2 生产链路修复设计

日期：2026-08-12

状态：已批准，待 TDD 实施

## 背景与范围

本设计修复真实功能 UAT 中 DataTap 结果没有进入 Evidence、未知调用保留积分却被成功收口、缺少 `brand_report_v3` 仍完成 Run、模型在无进展状态重复调用以及事件洪泛的问题。

本轮只允许使用脱敏 adapter fixture、fake model/DataTap transport、测试数据库和进程级离线 UAT。不得调用真实模型、DataTap、钱包，不创建真实 UAT round，也不修改旧 Run、旧 UAT 数据或历史报告。

## 1. Run 创建时固化能力与产物契约

### 1.1 服务端选择来源

`RuntimeConfigVersion.config_json` 新增服务端维护的 `profile_artifact_contracts` 映射。该映射只能由管理员通过已激活、不可变的 runtime config 版本提供；创建时必须同时满足：

1. profile 名称存在于服务端 `AgentProfile` 注册表；
2. artifact contract 存在于本次已审核 capability pack 的 contract 清单；
3. profile 允许 artifact 工具，且 contract 与审核 skill 的 contract 一致；
4. contract 不是从用户文本、模型动作、builder 调用、Draft 类型或已写入的 Artifact 反推。

Run 创建入口把服务端固定的 `profile_name` 传给 `snapshot_for_new_run`。该方法从当前激活 config 的审核 capability pack/profile policy 解析唯一的 `required_artifact_contract`，并在同一事务中生成快照。没有要求产物的 profile 显式保存 `null`，不能在后续执行中升级为必需产物。

### 1.2 不可变 RuntimeSnapshot

`RuntimeConfigSnapshot` 增加并冻结以下审计字段：

```json
{
  "profile_name": "session_analyst_v1",
  "required_artifact_contract": "brand_report_v3",
  "capability_pack_version": "1.0.0",
  "capability_pack_manifest_digest": "<sha256>"
}
```

`capability_pack` 内已有的完整 capability snapshot 仍保留；顶层版本和 digest 是 terminal/recovery 使用的稳定审计索引，必须与嵌套值一致。新 Run 必须具备这些字段；历史快照只允许按兼容规则读取，不补写、不回溯改变历史 Run 的契约。

正常 terminal、terminal ACK 丢失恢复、Recovery 重试和 force-complete 只读取 Run 自己的 `runtime_config_snapshot_json`。它们不得重新读取当前 active config、重新加载 manifest，也不得根据 builder、Publication 或模型输出推导 required artifact。

## 2. 唯一 `mcp_result_v1` 结果包络

Gateway/adapter 边界只产生下面一种 MCP 成功结果形状：

```json
{
  "mode": "mcpResult",
  "mcpResult": {
    "envelope": "mcp_result_v1",
    "result_status": "available | empty | unavailable",
    "structuredContent": {},
    "upstream_request_id": "<optional stable id>",
    "unavailable_reason": "<only for unavailable>"
  }
}
```

这是严格判别联合：

| `result_status` | `structuredContent` | 其他约束 | 结算 |
| --- | --- | --- | --- |
| `available` | 必须存在且为非空 JSON 值 | 通过服务端 allowlist output schema 后才可写 Evidence | settled + Evidence |
| `empty` | 必须不存在 | 仅代表上游确认成功但没有结果 | settled、无 Evidence |
| `unavailable` | 必须不存在 | `unavailable_reason` 必须是稳定枚举 | settled、无 Evidence |

当前允许的 `unavailable_reason` 仅包括 `payload_too_large`、`payload_not_retrievable`、`invalid_json_text`、`unsupported_content`、`local_persistence_failed`。未知字符串 fail-closed。

### 2.1 adapter 归一化规则

1. 原生 `structuredContent` 非空且可 JSON 序列化时成为 `available` 候选。
2. 没有原生结构化结果时，只读取 MCP CallToolResult 自己的 `content`；只有恰好一个 `type=text` block、文本去空白后非空、且整个文本一次性 `JSON.parse` 成功，才可作为结构化结果候选。DataTap 的审核 output schema 再决定它是否可写 Evidence。
3. 普通文本、多个 text block、混合 text block、UI resource、图片、音频、embedded resource、临时文件路径、`fullResultPath`、summary/omitted 字段都不得被提取为 Evidence；已确认成功时统一变为 `unavailable`。
4. 明确的空成功变为 `empty`。适配器无法判断是否已经外发或是否成功时，不生成结果包络，而走 `mcp/fail(result_unknown)`。
5. 已确认成功但 payload 过大、无法从本地持久化取回或本地持久化失败，生成 `unavailable`，不能伪装为 `result_unknown`。
6. 允许从 `meta`/`_meta` 的 `requestId`/`request_id` 提取并持久化 `upstream_request_id`；路径不可信，永远不作为结果来源。

后端是最终信任边界：拒绝缺少 `mcp_result_v1` 包络、判别联合不一致、非 JSON 值、非 allowlist schema、DataTap result 字符串不可解析的结果。只有最终校验通过的非空结果调用 `EvidenceWriter` 一次；Evidence 写入或结算事务失败时不得把调用标成成功，能确认已外发则保留可重试的已确认成功/不可用语义，无法确认则保留 `result_unknown` 与预留。

## 3. 固定账务状态机与 reconciliation

| 外部事实 | ToolCall | 钱包预留 | Evidence |
| --- | --- | --- | --- |
| confirmed success + valid payload | `settled` | 归零 | 一条且仅一条 |
| confirmed success + empty | `failed(error_type=succeeded_empty)` | 归零 | 无 |
| confirmed success + unavailable | `failed(error_type=result_unavailable)` | 归零 | 无 |
| confirmed failure | `failed(error_type=failed_confirmed/definitely_not_sent)` | 释放 | 无 |
| 无法确认是否外发/成功 | `unknown(error_type=result_unknown)` | 保留 | 无 |

`upstream_request_id` 和 `logical_call_id` 在 Pi 路径的 ToolCall 创建/完成前持久化。管理员 reconciliation 必须锁定 ToolCall/permit，重新执行同一 allowlist output schema 和 Evidence 规范化：可可靠取回并校验才 `confirm_success`；确认失败才 release；无法确认保持 unknown。禁止用自动超时、终态清理或测试分支释放/扣除未知调用。

完成门禁把 `reserved/running/unknown` ToolCall 以及未决 permit 都视为 unresolved；unknown 永远阻止成功终态。历史失败 Run 的 30 积分预留不在本轮处理。

## 4. 统一 completion validator

新增服务端 `CompletionValidator`，返回稳定的 `CompletionValidationResult`，最小错误码包括：

- `pi_gateway_terminal_missing_completion`
- `required_artifact_missing`
- `pi_gateway_unresolved_mcp_calls`
- `pi_gateway_running_agent_steps`
- `required_artifact_invalid_lineage`

对 `completed`/`completed_with_warnings`，必须同时满足：

1. 当前 Run 有持久化、非空的 assistant completion message；
2. 当前 Run 没有 `AgentStep.status=running`；
3. 当前 Run 没有 `reserved/running/unknown` ToolCall 或未决 permit；
4. 快照的 `required_artifact_contract` 为非空时，存在同一 tenant/user/session/Run 的已发布 Artifact、匹配 contract 的 immutable Version、有效 source draft revision、有效 publication/validation 和完整 lineage；
5. Version 的 `source_run_id` 必须是当前 Run，绝不接受同 Session 或历史 Run 的 Version。

该 validator 在以下所有路径调用：

- Pi 正常 `/terminal`；
- terminal ACK 丢失的 Recovery；
- Recovery 的 requeue/second-failure 分支在做成功判断前；
- `AgentRunRepository.force_complete` 及其 `settle_terminal` 系统完成回调。

validator 拒绝后，Gateway 收到稳定业务码并将 Run 收为 `failed` 或 `completed_with_warnings`（按已确认的业务结果），不包装成 `pi_gateway_worker_failed`，不增加恢复 Attempt。任何路径不得只凭 assistant message 声明 `completed`。

失败/取消终态在同一事务关闭所有 running AgentStep；running ToolCall 只能按外发不确定性转 unknown，明确未外发的 reserved 才能 release，终态后不得残留 running Step/ToolCall。

## 5. 跨 Attempt 的持久化 loop guard

`agent_runs` 新增 `loop_guard_json`（迁移 0044），仅存服务端计算结果，不接受模型写入。结构固定为：

```json
{
  "version": 1,
  "builder": {"fingerprint": "<sha256>", "streak": 2},
  "search_evidence": {
    "request_fingerprint": "<sha256>",
    "evidence_set_version": "<sha256>",
    "result_fingerprint": "<sha256>",
    "streak": 2
  },
  "terminal_code": null
}
```

错误指纹是对排序后的 `{tool_name, error_type, normalized_error}` 做 SHA-256；normalized_error 去除 UUID、时间戳和供应商全文，只保留稳定错误码及最多 512 字符的稳定字段。Evidence 集合版本是当前 Session 下 Evidence ID 的排序集合 SHA-256。Search 指纹同时包含规范化查询参数、分页 cursor、结果摘要和集合版本。

阈值固定为连续 3 次相同 builder 校验错误，或在 Evidence 集合版本未变化时相同 search 请求/结果连续 3 次。任何新 Evidence、不同错误指纹、不同查询或成功 builder 都重置对应 streak。达到阈值时一次性持久化 `terminal_code=agent_loop_circuit_open`，追加一次解释性 assistant message，并返回稳定业务 ToolResult；后续 Attempt 读取同一 guard，不得重置或继续循环。

## 6. 有界 delta batching

Pi projector 按 thinking/message 两个通道合并 delta，默认每批最多 4 KiB、最多 32 个片段、最长等待 50ms；任何单片段仍受既有单事件上限约束。批次时间由每次 project/flush 检查，边界事件无条件 flush，不通过扩大 SSE buffer、sleep 或取消断言规避洪泛。

必须在下列边界前 flush：正常完成、tool/usage/turn 事件、cancel、abort、provider error、decision limit、child exit 和 terminal 请求。worker `finally` 负责最后一次 flush，child 只有 flush 完成后才退出；Gateway terminal 发送前等待所有投影事件成功持久化。测试验证 `message.completed` 在唯一 terminal 前、sequence 单调、关键 tool/usage/artifact 事件不丢、无 `event_buffer_overflow`，并在默认 buffer 下通过高量 delta fixture。

## 7. TDD 与回归矩阵

先写红灯再实现：

1. 脱敏真实 adapter 结果形状的 available/empty/unavailable/普通文本/resource/image/path 拒绝；available → Evidence → settled → reserved=0。
2. 真实 failure/unknown/reconcile 语义与并发锁测试。
3. Snapshot 创建时固化 contract/version/digest、existing/recovery 不重新读取 active config、builder 缺失和历史 Version 隔离。
4. Builder → Publication → Version → Excel/BI 同一 Version；lineage 无效拒绝。
5. completion validator 覆盖正常 terminal、ACK 丢失、Recovery、force-complete、unknown、running Step/ToolCall、缺 required artifact。
6. 跨 Attempt loop guard、builder 错误指纹、Evidence set version 变化及稳定失败消息。
7. delta batching 所有 flush 边界和默认容量高事件量。
8. Scenario 1 裸 MCP 名称映射、模型决策限制、usage 去重、terminal ACK、既有 Evidence/Builder/Publication/Version/Gate 测试不回归。
9. 完整 fake topology 离线 Pi UAT 一轮；不设置真实凭证，不调用真实服务。

验证命令、红绿结果和剩余风险追加到 `changelog/2026-08-12.md`，不改写历史 operator report。完成后的唯一对外状态为 `READY_FOR_FUNCTIONAL_SCENARIO_2_RERUN_REVIEW`。
