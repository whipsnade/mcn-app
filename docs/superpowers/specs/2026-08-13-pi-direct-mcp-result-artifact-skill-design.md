# Pi 透明 MCP 结果与 Artifact Skill 架构决策

日期：2026-08-13

状态：用户明确批准的架构转向；本轮只做离线实现与验证，等待 direct model MCP smoke review。

## 1. 决策范围

DataTap MCP 是内部自研、受控服务。MCP 的字段、类型、分页、响应上限、错误和敏感信息契约由 MCP 服务自身及 adapter contract test 保证。Pi 主链路不再对成功 MCP 返回做 Evidence 二次解析、归一化、业务 Schema 复核或可信判定。

新的生产数据流固定为：

```text
用户请求 → Pi → 内部 MCP → 标准 Pi adapter → 模型
                                      ↓
                         模型自主选择继续调查/文本完成/Artifact Skill
                                      ↓
                        Builder → Publication → Version → BI/Excel 同版
```

本决策覆盖此前 Evidence Bridge 修复方向，但不删除 Evidence 表、历史 Evidence、旧 Run 或 current/legacy runtime 的兼容路径。

## 2. 责任边界

### 2.1 MCP 与 adapter

内部 MCP 负责输出契约、分页、数据量、标准 MCP error、凭证隔离和契约测试。标准 Pi adapter 将 Tool Result 原样交给模型：保留 `structuredContent`、文本块、多个文本块、resource/image 等标准内容的原始语义。不得添加业务 envelope，不得把成功结果转换为 `result_unavailable`，不得截断后伪装成功。

如果 adapter 需要进程内存保护，超限只能变成模型可见的标准 tool error；它不能生成 Evidence 状态，也不能作为正常成功结算。

### 2.2 Pi Gateway accounting

Gateway 只负责 tenant/run/tool-call 身份、工具 allowlist、secret 隔离、调用前预留、幂等、`logical_call_id`、状态审计和结算。accounting hook 是旁路观察者，失败不得替换已经返回给模型的 Tool Result。

成功 finalize 只携带不含业务数据的控制面元数据：

```json
{
  "permit_id": "...",
  "outcome": "succeeded",
  "upstream_request_id": "...",
  "response_bytes": 1234,
  "adapter_version": "...",
  "completed_at": "...",
  "response_hash": "sha256:..."
}
```

其中除 `permit_id` 和 `outcome` 外的字段可选，且只能是稳定 ID、字节数、版本、时间和不含业务字段的哈希。不得传输 MCP payload、`structuredContent`、text blocks、UI resource、图片、临时路径、模型上下文或完整 Tool Result。控制面大小上限应足以容纳该元数据，而不是业务响应。

结算语义固定为：正常 `isError=false` 结果 `settled`；明确 Tool Error 或调用前本地拒绝 `release`；已外发但无法确认外发/成功 `unknown` 并保持预留。finalize ACK 丢失不改写模型结果、不创建 Attempt 2，accounting 进入 unknown 等待 reconciliation。unknown 不自动 replay、release 或扣除。

### 2.3 Artifact Skill

Artifact Skill 是正式 BI/Excel 的唯一业务结构门禁。模型若决定生成产物，调用受 allowlist 约束的内部 Builder Tool，提交严格结构化输入，例如：

```json
{
  "artifact_type": "brand_report_v3",
  "payload": {"...": "完整 brand_report_v3 结构"}
}
```

Builder 必须从当前 Run 的不可变 RuntimeSnapshot 读取 `allowed_artifact_contracts`，校验 artifact type、payload schema、tenant/session/run 归属、非空 payload、Draft 生命周期、Publication、Version 和 lineage。可选 `source_tool_call_ids` 只检查这些调用属于当前 Run；不得读取或重新校验 MCP payload，也不得要求该字段存在。

Profile 只提供候选 artifact allowlist，不产生 required contract；用户文本、模型输出、Builder 调用和历史 Artifact 都不能扩展或决定 Snapshot 的 contract 集合。模型可以只返回文本，也可以选择 allowlist 中任意一种产物。没有调用 Artifact Skill 时，Runtime 仍可正常完成文字 Run，UAT 另行评价用户目标是否达成。

## 3. Completion 与隔离

正常 terminal、terminal ACK 丢失 Recovery、Recovery 重试和 force-complete 必须调用同一个 `CompletionValidator`。成功终态不得仅凭 assistant message；至少要有 durable assistant completion、无 running AgentStep、无 running/planned/reserved ToolCall 或未决 permit，并处理 active Draft。实际存在的 Publication/Version 必须满足当前 tenant/session/run、Snapshot allowlist、Schema、Publication、Version 和 lineage 校验；历史 Run 的 Artifact 不得满足当前 Run。

CompletionValidator 不依赖 Evidence 数量，也不要求某个 required artifact。账务 unknown 是会计未决事实，不接管模型业务决策；它不能被自动释放或伪造成功，但不应把文字完成变成业务 Artifact 强制门禁。失败终态仍必须关闭运行中的 Step，并按事实区分 definitely-not-sent、failed-confirmed 和 unknown。

## 4. Evidence 兼容边界

Pi production path 不调用 `parse_mcp_result_details`、`_validate_mcp_output` 或 `EvidenceWriter.write`，不使用 `mcp_result_v1`、available/empty/unavailable 分类，不把 `search_evidence`/`read_tool_result` 作为报告必经步骤，不通过 parent IPC 传输 MCP payload。Evidence 表和历史数据继续保留；current/legacy runtime 仍可使用旧 Evidence 工具和测试，但不得被新 Pi transparent-result path 隐式调用。

## 5. 可验证不变量

- MCP Tool Result 在 adapter hook 后仍与模型收到的 `event.content` 相同；accounting 调用不返回替代 payload。
- successful finalize 控制面请求不含业务 payload；普通文本、多 text block、空成功和 `{result: JSON string}` 都按标准 Tool Result 原样传递并正常结算。
- 明确错误 release；未知保持 unknown/reserved；ACK 丢失不 replay、不改写模型结果。
- Builder 可在无 Evidence 时接收符合 allowlist 的模型 payload；非法 Schema、非 allowlist type、跨 Run 归属均返回结构化错误。
- 已发布产物的 BI/Excel 使用同一个 Version；active Draft 必须 publish 或 abandon。
- legacy Evidence 测试保持兼容，历史 Run/UAT/账务不被本轮修改。
