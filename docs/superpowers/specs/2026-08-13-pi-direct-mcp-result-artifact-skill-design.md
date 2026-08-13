# Pi 透明 MCP 结果与 Artifact Skill 架构决策

日期：2026-08-13

状态：用户明确批准的架构转向；已完成离线实现与验证（3 个提交 `284e4c7`/`45ec465`/`260f5cc`），
状态 `READY_FOR_WEB_FUNCTIONAL_SCENARIO_2_RERUN_REVIEW`（待独立审查确认）。

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

Artifact Skill 是正式 BI/Excel 的唯一业务结构门禁。模型若决定生成产物，调用受 allowlist 约束的
内部 `build_artifact_draft` 工具，提交 **typed model input**（提交 1–3 实现），例如：

```json
{
  "artifact_type": "brand_report_v3",
  "payload": {
    "scope": {"...": "brand/period/platforms/keywords/comparison_mode"},
    "data": {"...": "业务章节"},
    "narrative": {"...": "executive_summary/findings/recommendations"},
    "availability": {"overview": {"status": "complete"}, "...": "..."},
    "limitations": [],
    "methodology_input": {"data_as_of": "...", "source_names": [], "notes": []}
  },
  "source_tool_call_ids": []
}
```

模型**不得**提交 `schema_version`/`module`/`data_status`/`canonical_data`/`field_lineage` 等
服务器字段（提交 `server_owned_field_rejected`，明确拒绝并说明）。服务器流程固定为：

1. 从当前 Run 的不可变 RuntimeSnapshot 读取 `allowed_artifact_contracts`（不读取用户文本、
   prompt snapshot、当前 Builder 调用推导 contract）；
2. 按 `artifact_type` 从 `MODEL_INPUT_BY_ARTIFACT_TYPE` 选择模型输入 DTO
   （brand_report_v3 / campaign_report_v2/v3 / kol_selection_v3 / insight_board_v1；
   DTO 全部 `extra="forbid"` + `frozen`，复用 `payloads/` 下的既有类型，不复制定义）；
3. 严格校验 payload（tenant/session/run 归属、非空、DTO Schema）；失败返回结构化字段级错误；
4. 确定性组装完整发布 payload：`schema_version`/`module`/`data_status` 由服务器确定
   （data_status 按 REQUIRED_SECTIONS availability 推导 complete/restricted）；`methodology`
   由 `methodology_input` 组装；`canonical_data`/`field_lineage` 由 `publish_canonical`
   从 `data` 确定性生成（恒等 lineage，精确覆盖全部 data 叶子）；
5. 最终 payload 过现有 `ArtifactPayloadValidator` 严格校验（`direct_model_payload=True`），
   再经既有 Draft → Publication → Version → BI/Excel 同版链路。

可选 `source_tool_call_ids` 只检查这些调用属于当前 Run；不得读取或重新校验 MCP payload，也
不得要求该字段存在。

**结构化错误回喂（提交 1）**：DTO/发布校验失败以 RFC6901 字段级错误反馈模型
（`backend/app/agent_artifacts/payload_errors.py`）：每条 `{"path": "/data/overview/total_volume",
"type": ..., "reason": ..., "retryable": true}`；最多 8 条、序列化 ≤2048 字节、超限截断并标记
`truncated`；`reason` 只取校验 msg，绝不携带输入值（`errors(include_context=False)`）。
模型按 path/type/reason 逐条修正后重试（离线 UAT 已进程级验证「第一次缺字段失败 → 按错误
修正 → 第二次成功」，build_artifact_draft 恰 2 次而非几十次盲改）。

Profile 只提供候选 artifact allowlist，不产生 required contract；用户文本、模型输出、Builder
调用和历史 Artifact 都不能扩展或决定 Snapshot 的 contract 集合。模型可以只返回文本，也可以
选择 allowlist 中任意一种产物。没有调用 Artifact Skill 时，Runtime 仍可正常完成文字 Run，
UAT 另行评价用户目标是否达成。

### 2.4 模型输入契约暴露（提交 2）

`load_marketing_skill` 加载 skill 时，若其 `artifact_contract` 已在 `MODEL_INPUT_BY_ARTIFACT_TYPE`
注册，返回体追加 `model_input_contract`（提交 2 实现）：

```json
{
  "artifact_type": "brand_report_v3",
  "input_schema_version": "direct_model_input_v1",
  "model_input_schema": {"<DTO 的 JSON Schema，单一事实源>"},
  "concise_example": {"<DTO 类方法生成的合法最小示例，不含预计算业务结果>"},
  "required_tools": ["build_artifact_draft", "publish_artifacts"],
  "publication_expectations": {"via": "publish_artifacts", "same_version_bi_excel": true}
}
```

- `model_input_schema` 完全来自 DTO 类 `model_json_schema()`（单一事实源，不手写第二份 schema；
  测试用 drift test 断言二者逐字节一致）；
- schema/示例完整返回、不截断；整体响应 JSON 序列化超过 512 KiB 时 fail-closed 拒绝
  （`marketing_skill_contract_too_large`）；
- `concise_example` 只保证形状合法（业务值为简单占位），不含任何预计算/真实业务结果；
- 返回内容不含 secret 或数据库内部身份（测试递归断言）。

### 2.5 capability pack 版本化（提交 3）

- 当前包目录 `backend/app/marketing_capability_pack/packs/marketing-v2/`（manifest
  `pack_version="1.1.0"`，6 个 skill 的 `version` 全部为 1.1.0）；`build_marketing_run_capability`
  现加载 `marketing-v2`；
- 旧 `marketing-v1`（1.0.0）目录原样保留、只读：历史 RuntimeSnapshot 经 digest 仍可解析，
  旧 Run 语义不变；不得原地修改 v1；
- v2 的 skills `required_tools` 全部属于 Pi production 工具面
  （get_session_context / load_marketing_skill / read_artifact / build_artifact_draft /
  publish_artifacts / request_clarification），不再引用已废止 Builder/检索工具；
- v2 的 SKILL.md 与 root-policy.md 已删除 Evidence 必经、`search_evidence`/`read_tool_result`/
  `mcp_result_v1`、旧 Builder 指令；改为「MCP 标准 Tool Result 直接消费 + 按
  load_marketing_skill 的 model_input_contract 构造输入 + publish_artifacts 发布」；
- manifest 内全部 digest（root_policy / skill / contract）为文件内容 SHA-256（hex），随文件
  修改重算；manifest 自身 `manifest_digest` 由 loader 对 manifest 内容计算（不可变快照身份）。

## 3. Completion 与隔离

正常 terminal、terminal ACK 丢失 Recovery、Recovery 重试和 force-complete 必须调用同一个 `CompletionValidator`。成功终态不得仅凭 assistant message；至少要有 durable assistant completion、无 running AgentStep、无 running/planned/reserved ToolCall 或未决 permit，并处理 active Draft。实际存在的 Publication/Version 必须满足当前 tenant/session/run、Snapshot allowlist、Schema、Publication、Version 和 lineage 校验；历史 Run 的 Artifact 不得满足当前 Run。

CompletionValidator 不依赖 Evidence 数量，也不要求某个 required artifact。账务 unknown 是会计未决事实，不接管模型业务决策；它不能被自动释放或伪造成功，但不应把文字完成变成业务 Artifact 强制门禁。失败终态仍必须关闭运行中的 Step，并按事实区分 definitely-not-sent、failed-confirmed 和 unknown。

## 4. Evidence 兼容边界

Pi production path 不调用 `parse_mcp_result_details`、`_validate_mcp_output` 或 `EvidenceWriter.write`，不使用 `mcp_result_v1`、available/empty/unavailable 分类，不把 `search_evidence`/`read_tool_result` 作为报告必经步骤，不通过 parent IPC 传输 MCP payload。Evidence 表和历史数据继续保留；current/legacy runtime 仍可使用旧 Evidence 工具和测试，但不得被新 Pi transparent-result path 隐式调用。

## 5. 可验证不变量

- MCP Tool Result 在 adapter hook 后仍与模型收到的 `event.content` 相同；accounting 调用不返回替代 payload。
- successful finalize 控制面请求不含业务 payload；普通文本、多 text block、空成功和 `{result: JSON string}` 都按标准 Tool Result 原样传递并正常结算。
- 明确错误 release；未知保持 unknown/reserved；ACK 丢失不 replay、不改写模型结果。
- `build_artifact_draft` 的 payload 是 typed model input：模型禁止提交服务器字段（`server_owned_field_rejected`）；服务器按 allowlist 选 DTO → 严格校验 → 确定性组装；canonical/lineage 从 `data` 生成并精确覆盖全部叶子；最终 payload 过现有严格校验（提交 1 测试断言 canonical path 集合 == `walk_data_leaves(data)` 集合）。
- DTO/发布校验失败回喂结构化字段级错误（RFC6901 path/type/reason/retryable，≤2048 字节、truncated、不泄漏提交值）；离线 UAT 验证模型按错误自纠错（恰 2 次调用成功发布）。
- `load_marketing_skill` 对已注册契约返回 `model_input_contract`（`model_input_schema` 与 DTO `model_json_schema()` 完全一致——单一事实源 drift test）；完整不截断、超限 fail-closed；不含 secret/数据库身份。
- capability pack 当前为 `marketing-v2`（1.1.0），skills required_tools ⊆ Pi production 工具面，SKILL.md/root-policy 无 Evidence 必经与旧 Builder 指令；旧 `marketing-v1`（1.0.0）目录不可变保留、旧 RuntimeSnapshot 经 digest 仍可解析。
- result_unknown 元数据可观测性：`PiGatewayMcpFailureMetadata` 携带 error_class / received_jsonrpc_response / dispatch_phase / is_standard_mcp_error / upstream_request_id（全部可选）；分类语义不变（call_failed → result_unknown 保持预留、不自动释放、不自动重放）。
- 已发布产物的 BI/Excel 使用同一个 Version；active Draft 必须 publish 或 abandon。
- legacy Evidence 测试保持兼容，历史 Run/UAT/账务不被本轮修改。
