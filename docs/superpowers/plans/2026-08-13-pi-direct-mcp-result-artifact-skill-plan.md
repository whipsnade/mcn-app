# Pi 透明 MCP 结果与 Artifact Skill 实施计划

> 本计划执行于 `codex/real-mcp-evidence-bridge-repair`。仅使用脱敏 fixture、fake model/MCP transport、测试数据库和离线 Pi UAT；不调用真实模型、DataTap、钱包，不创建 UAT Run，不处理历史 reserved=100/30，不修改旧 UAT/evidence。

## 总体不变量

1. 标准 MCP Tool Result 透明进入模型；accounting 是旁路观察者。
2. Pi production path 不解析或改写业务 payload，不写 Evidence，不把 Builder 绑定到 Evidence。
3. finalize 只传严格 metadata，业务 payload 不进入 IPC、日志或控制面数据库。
4. Snapshot 只固化服务端审核 pack/profile 的 allowlist、pack version 和 manifest digest；没有 required artifact。
5. Artifact Skill/Builder 负责最终业务 Schema、Draft、Publication、Version、lineage 和同版导出。
6. CompletionValidator 是所有完成入口的统一平台门禁；Evidence 数量和用户目标不构成 Runtime 必达条件。

## 阶段 1：保护基线与架构记录

- 记录 branch、HEAD、dirty 状态、最近提交、进程和端口。
- 保留已有未提交会计/诊断测试，逐文件移除或改写旧 Evidence payload 分类改动；不 reset、restore、stash、rebase、amend 或重写提交。
- 创建本设计文档和本计划；在旧 Evidence Bridge 设计顶部加入 superseded 标记；在当日 changelog 追加用户明确作出的架构简化决策。

## 阶段 2：先红的 direct-result/accounting 测试

### Pi Gateway/Vitest

- 用脱敏真实 adapter 形状覆盖 non-empty `structuredContent`、普通文本、多 text blocks、空成功、内部 `{result: JSON string}` 和明确 Tool Error。
- 断言模型看到的 `event.content` 原样保留，hook 不注入自定义 envelope、不替换 content，且 accounting 不接收业务 payload。
- 核对受控 MCP 使用标准 adapter 透明输出；关闭会把成功响应截断/摘要化并写临时路径的 adapter output guard，由 MCP 自身分页和控制响应上限。
- 断言成功 finalize RPC 只有 permit/outcome 和允许的安全元数据；拒绝 details、payload、structuredContent、text、path 和完整 Tool Result。
- 断言 unknown/ACK loss 只改变 accounting 状态，不 replay、不创建新 Attempt。

### Backend/pytest

- finalize DTO 对业务 payload/旧 envelope fail closed，对最小 metadata 通过。
- Pi finalize settles 且 Evidence 数量不变；明确失败 release；unknown 保持 reservation。
- Pi service 不调用 `parse_mcp_result_details`、`_validate_mcp_output`、`EvidenceWriter.write`。

## 阶段 3：实现透明 MCP 与 metadata-only accounting

- 删除新 Pi adapter/accounting extension 的业务 envelope、分类、JSON wrapper 解析、offload/Evidence 逻辑；保留 Scenario 1 裸 MCP 名称映射、allowlist、secret、幂等和 lease。
- 将 IPC、control-plane-client、worker bridge、FastAPI contracts/service 改为最小 finalize metadata，并限制字节数；accounting hook 失败只记录安全状态。
- Pi 成功结果直接走 SDK/adapter 标准 tool_result 生产事件；标准错误同样原样交给模型。
- 为 finalized call 记录 upstream/logical call id 和安全计费状态，unknown 交给 admin reconciliation；Pi Recovery 不实例化 legacy AgentMcpTool，不自动重放、解析或写 Evidence。

## 阶段 4：实现模型输入的 Artifact Builder

- 新增内部 `build_artifact_draft`（名称如已有 registry 约定可调整），输入至少为 `artifact_type`、严格 `payload`，可选 `source_tool_call_ids`。
- 从当前 Run 的 RuntimeSnapshot 读取 allowlist；不读取用户文本、prompt snapshot、当前 Builder 调用推导 contract。
- 复用现有 ArtifactPayloadValidator/ArtifactService/ArtifactPublicationService，保证 Schema、租户/会话/Run 归属、非空、Draft、Publication、Version、lineage 严格；错误以结构化 ToolResult 回喂 Pi。
- 让 `session_analyst_v1` 无 Evidence 也可调用该 Builder；保留 legacy Evidence builder、history tool 和旧数据兼容，但不让新 direct path依赖它们。

## 阶段 5：统一 completion/recovery/force-complete

- 删除 Pi 新路径的 required artifact 和 Evidence 数量门禁；保留 Snapshot allowlist/pack version/manifest digest 审计与已存在 Artifact 的严格 lineage 校验。
- 正常 terminal、terminal ACK recovery、Recovery retry、force-complete 全部调用统一 CompletionValidator；assistant-only 只能满足 durable message 子条件，不能绕过 Step/ToolCall/permit/active Draft/实际产物链校验。
- unknown 不自动释放、不自动 replay；它不阻止文字完成，但任何未决 running/reserved/planned 状态仍禁止成功终态。
- 失败/cancel/abort/provider error/decision limit/child exit 前收口 Step、ToolCall 和 delta flush 语义，不改变既有安全结算规则。

## 阶段 6：离线验证与审查

- 运行 direct-result/accounting/Builder/Completion/Recovery/Publication/Version/Gate 定向测试。
- 运行 backend 全量 pytest、Ruff、migration suite；pi-gateway 全量 Vitest、typecheck、build；如契约影响前端则补 Vitest/tsc/build。
- 运行完整离线 Pi UAT 一轮，仅 fake model/MCP；做默认事件 buffer、terminal ACK、usage 去重、Scenario 1 裸名映射回归。
- 执行 `git diff --check`、每个提交 `git show --check`、secret scan、进程/端口残留检查；移走测试生成 xlsx，删除 worktree 内 `.venv` 软链接本身。
- 独立审查 Critical 0 / Important 0：重点检查透明 Tool Result、metadata-only control plane、无 Evidence 写入、无 required artifact、Artifact Schema/Version/lineage、租户隔离/allowlist/secret/计费/幂等和 legacy 兼容。

## 交付状态

> 更新（2026-08-13 后续）：阶段 6 已完成——4 个线性提交
> `33d37c0`/`0d87d4e`/`96e8fd9`/`c01ec1b`（`e00690fb` 线性后代），全量离线验证通过，
> 独立审查 Critical 0 / Important 0，真实 Direct Model + MCP Smoke 已执行：
> `DIRECT_MODEL_MCP_SMOKE_FUNCTIONALLY_ACCEPTED_WITH_PROTOCOL_DEVIATION`
> （详见 `docs/qa/2026-08-13-direct-model-mcp-smoke-review.md`；偏差：直连对照调用 2 次
> 超出授权上限 1 次，如实记录）。当前基线 `c01ec1ba1ea3dc3805184ea3ddb8f4bf0ea14196`。
> 下一状态：`READY_FOR_WEB_FUNCTIONAL_SCENARIO_2_REAUTHORIZATION`。

> 更新（2026-08-13 当日，Direct Artifact Skill 契约修复）：阶段 4（模型输入 Builder）在
> 本分支 `codex/direct-artifact-skill-contract-repair` 完成三个提交——
> `284e4c7`（模型输入 DTO + 服务器组装边界 + 结构化字段级错误反馈）、
> `45ec465`（把精确模型输入契约经 load_marketing_skill 交给模型）、
> `260f5cc`（capability pack 1.1.0 升级 + 离线 UAT 自纠错场景 + result_unknown
> 元数据可观测性）；验证：backend 定向 165 passed、agent_artifacts 591 passed、
> pi-gateway vitest 179 + typecheck + build、离线 UAT 27 场景含自纠错（详见当日
> `changelog/2026-08-13.md`「提交 2/3/4」段）。下一状态：
> `READY_FOR_WEB_FUNCTIONAL_SCENARIO_2_RERUN_REVIEW`。

历史交付状态（当时记录，保持原样）：验证完成后只报告
`READY_FOR_DIRECT_MODEL_MCP_SMOKE_REVIEW`。本分支停止等待架构复核，不直接运行完整
Scenario 2，也不进入 Scenario 3–7。
