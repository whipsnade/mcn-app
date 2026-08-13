# Pi 自主决策边界纠偏设计

日期：2026-08-13
范围：Pi Agent Runtime、MCP accounting bridge、Evidence/Artifact terminal gate 及离线回归
目标：恢复模型主导的业务决策边界，同时保留平台级安全、计费、证据完整性和终态一致性约束。

## 1. 决策结论

### 1.1 三层职责

| 层 | 允许决定的内容 | 明确禁止 |
| --- | --- | --- |
| Pi / 模型 | 意图、Skill、工具和参数、调用顺序、重试、证据是否充分、是否调用 Builder、选择允许的 artifact contract、文本完成或带产物完成 | 绕过工具 allowlist、伪造 Evidence/Artifact、使用不属于当前 Run 的资源 |
| Backend / Gateway 平台 | tenant/user/session/run 隔离、凭据、MCP allowlist/digest/schema/HMAC/nonce/lease/fencing/idempotency、计费、Evidence 可信边界、Builder 输入、Publication/Version/lineage、终态一致性、恢复幂等 | 根据 Profile、用户文本、模型自报或 Builder 调用推导必达业务产物；规定固定 MCP 顺序、固定工具清单、固定 Builder/search 业务循环 |
| UAT / 评价 | 按用户目标判断是否达成 | 改写生产工作流或把评价目标变成 Runtime 的 required artifact gate |

### 1.2 Runtime Snapshot

新 Run 创建时从已审核 capability pack/profile 固化：pack version、manifest digest、允许的 Skill、允许的 artifact contract、模型/adapter/计费/平台限制。Snapshot 是 terminal 和 Recovery 的唯一运行时依据。

Snapshot 不再写入由 Profile 推导的 `required_artifact_contract`。历史 Snapshot 保留只读兼容解析，不被迁移或回写；新 Snapshot 将 artifact contract 作为“候选能力集合”而非必达目标。Profile 的 `allowed_artifact_contracts` 只做 allowlist，不能表达一个 Run 必须产出哪个报告。

Artifact contract 只有在 Pi 选择并真正进入 Builder/Draft 链时才冻结。Builder、子 Run、用户文本和模型输出都不能把选择扩展到 Snapshot allowlist 之外。

### 1.3 统一完成契约

所有正常 terminal、terminal ACK 丢失后的 Recovery、force-complete 和其他完成入口调用同一个 `CompletionValidator`。它只判断平台一致性：

1. 有 durable assistant completion；
2. 没有 running AgentStep；
3. 没有 planned/reserved/running ToolCall、未决 permit 或未结算预留；
4. 没有 active Artifact Draft；abandoned/failed Draft 不阻断，但在结果中留下 limitation；
5. 当前 Run 若存在 Publication/Version，则逐项校验 tenant/session/run 归属、Snapshot allowlist、schema、publication/version、validation 和 lineage；不存在产物也合法；
6. `result_unknown` 保留预留并形成 warning，但不触发重放或 Recovery；running/未决 MCP 仍阻止完成；
7. 旧历史 Run 的 Version 不满足当前 Run 的产物条件；
8. 违规时返回稳定业务错误，不映射成 worker crash，也不创建新 Attempt。

unknown 只要没有 running/unresolved row，文本完成可以进入 `completed_with_warnings`；计费状态仍是 unknown，预留继续保留，等待正式 reconciliation。

### 1.4 MCP 双平面桥

MCP 返回分为两个平面：

- 模型可见平面：保留 adapter 原始语义（structured result、text、empty、error、timeout），不把内部会计 envelope 替换成模型工具结果；
- 控制面 sidecar：仅为预检、结算和 Evidence 提供规范化元数据，并由后端再次执行 allowlist output schema 校验。

`mcp_result_v1` 作为 sidecar 的严格判别联合：

```text
available   = 非空 structuredContent
empty       = 禁止 structuredContent
unavailable = 禁止 structuredContent + 稳定 reason
```

仅允许“恰好一个 text block 且整个文本一次 JSON 解析成功”转换为 `structuredContent`。普通文本、多个 text block、UI resource、图片和任意路径都不能生成 Evidence。adapter 真实使用临时落盘时，只允许当前 Run 专属目录内、通过 owner/权限/realpath/traversal/symlink/size/lifetime 检查的文件；不满足条件统一 `payload_not_retrievable`，不能伪造 payload。adapter 没有实际落盘时不创建或读取临时文件。

结算语义固定为：

| upstream 事实 | sidecar | 计费 | Evidence | Run 影响 |
| --- | --- | --- | --- | --- |
| confirmed success + valid payload | available | settled，reserved=0 | 写入一条合法 Evidence | 正常 |
| confirmed success + genuinely empty | empty | settled，reserved=0 | 无 | 正常 |
| confirmed success + payload 不可取回 | unavailable | settled，reserved=0 | 无 | 正常，可 warning |
| confirmed failure | failure | release | 无 | 由 Pi 决定业务是否继续 |
| 无法确认是否外发/成功 | result_unknown | 保留 reserved | 无 | 可随文本进入 `completed_with_warnings`；不自动 replay/release，账务仍待 reconciliation |

payload 太大或本地 Evidence 持久化失败只有在已确认成功时才能 `unavailable`；任何无法确认外发的异常仍是 `result_unknown`。

### 1.5 LoopGuard 与事件

`loop_guard_json` 跨 Attempt 持久化，但只记录错误指纹、Evidence 集合版本、连续次数、最近时间和 operator/UI warning；它不能写入决定 Run terminal 的业务码，也不能阻止 Pi 再次选择相同 Builder/search。

仍保留平台级的 logical_call_id 幂等、每 Run 跨 Attempt 的高层 decision fuse、MCP 账本和外部操作员可控的成本/时间停止。相同 logical call 的重复请求按平台幂等处理；不同 logical_call_id 不因工具名相同而被平台当作重复业务调用。

delta projector 继续同时受批次最大字节数和最大等待时间约束。正常完成、tool/usage/turn、cancel、abort、provider error、decision limit、child exit 和 terminal 前都必须 flush；关键 tool/usage/artifact/`message.completed` 事件不得被 delta 合并丢失，terminal 之前不得出现 `event_buffer_overflow`。

## 2. 状态与不变量

### 2.1 Artifact 选择

```text
RuntimeSnapshot.allowed_artifact_contracts
          │ allowlist
          ▼
Pi chooses artifact contract
          │ first Builder/Draft invocation freezes contract
          ▼
Evidence → Builder → Draft → Validator → Publication → Version → BI/Excel
```

没有 Builder/Draft 的 Run 可以只有文本 assistant completion。不能通过创建空 artifact、历史 Version 或跨 Run lineage 冒充产物完成。

### 2.2 终态

`completed` / `completed_with_warnings` 是平台一致性成功，不是用户目标评价。用户要求品牌报告而 Pi 只返回文本时，Runtime 可以完成；UAT 负责判定“目标未达成”。若 Pi 自主调用 Builder，则 Builder、Publication、Version 和同 Version 的 BI/Excel 绑定必须严格校验。

### 2.3 Recovery

Recovery 读取 Run 的不可变 Snapshot 和统一 validator。unknown 不触发 Attempt 2；只有明确的基础设施级缺失 completion、且没有未决 MCP/active Draft/运行步骤时，才允许按既有一次性 Recovery 策略处理。任何完成路径不能只凭 assistant message 越过平台 gate。

## 3. 兼容与迁移

不修改历史 Runtime Config、旧 Run、旧 UAT 或历史 Evidence。新配置/新 Run 使用纠偏后的 Snapshot contract version；`snapshot_for_existing_run` 保持对旧 Snapshot 的只读兼容解析，并不把旧配置的 Profile mapping 应用于新 Snapshot。若需要数据库字段，仅新增 Alembic migration；`loop_guard_json` 继续复用现有列，除非测试证明无法保存所需观测字段。

## 4. 验证矩阵

实现必须以红灯测试先行，覆盖：

1. 无 artifact 的文本完成；
2. 用户要求报告但无报告时 Runtime 完成、UAT 失败；
3. `session_analyst_v1` 不强制 brand report；
4. Pi 可在 allowlist 中选择 brand/campaign/kol/insight，越权 fail closed；
5. active Draft 阻断，abandoned Draft 带 limitation 放行；
6. 当前 Run 合法 Publication/Version/lineage 放行，历史 Version 不满足；
7. unknown 不 Recovery/replay、保留预留；确认成功的 empty/unavailable settled；
8. raw MCP 语义与 sidecar 规范化保持语义一致；合法 JSON text 为 available，普通文本不生成 Evidence，empty 不误判 unavailable；
9. trusted offload 安全读取，路径穿越/symlink 拒绝；
10. Builder/Search 重复不被服务端强制阻断；跨 Attempt max_decisions 仍有效；
11. ACK/Recovery/force-complete 与正常 terminal 使用同一 validator；
12. 默认 delta 配置覆盖所有退出边界；Scenario 1 裸 MCP 名称映射、决策限制、usage 去重、terminal ACK 不回归；
13. Artifact 链所有导出绑定同一 Version，不产生空 artifact。

## 5. 非目标与禁止事项

- 不调用真实模型、DataTap、钱包，不创建真实 UAT Run；
- 不处理历史失败 Run 的 reserved=30；
- 不通过关闭隔离/lineage、直接写 Evidence/Artifact、mock 生产路径、放宽断言、sleep、无限扩大 buffer 使测试变绿；
- 不执行真实 Scenario 2，也不宣称 B7 PASS 或生产就绪。
