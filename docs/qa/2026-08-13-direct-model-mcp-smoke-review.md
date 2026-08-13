# Direct Model + MCP Smoke 验收复核记录

日期：2026-08-13
性质：架构验收复核记录（不是 Scenario 2 PASS、不是 B7 PASS、不是生产就绪）

```text
Status: READY_FOR_WEB_FUNCTIONAL_SCENARIO_2_REAUTHORIZATION
Real external calls authorized: NO（本轮为已执行 round 的复核记录；下一步 Web Functional
  Scenario 2 需要用户按新授权模板逐字重新授权）
Production cutover authorized: NO
Plan C authorized: NO
```

## 1. 执行身份

| 项 | 值 |
| --- | --- |
| branch | `codex/real-mcp-evidence-bridge-repair` |
| execution SHA | `c01ec1ba1ea3dc3805184ea3ddb8f4bf0ea14196` |
| round_id | `DIRECT_MODEL_MCP_SMOKE_20260813T103101Z_c01ec1ba` |
| 线性提交 | `33d37c0` / `0d87d4e` / `96e8fd9` / `c01ec1b`（`e00690fb` 线性后代） |
| Gateway build | `sha256(dist/main.js)` 前 16 位 `6639132a9cab6c8c` |
| 隔离数据库 | `kol_insight_b7_uat`（`kol_b7_uat@localhost`，utf8mb4，73 表，head `0044_agent_run_loop_guard`） |
| 隔离证明 | 专用账号访问 `kol_insight.users` 被 MySQL 1142 拒绝 |

## 2. 结果摘要

| 项 | 结果 |
| --- | --- |
| Run | `completed`（runtime_backend=pi），**Attempt 恰 1** |
| 真实模型请求 | 3 次（RuntimeUsageRecord 去重后 3 条；上限 3，`max_decisions=3` 生效） |
| 生产 DataTap dispatch | **恰 1 次**（`match_best_tag`，入参 `{"tag_type":"品牌标签","tag_names":["蔚来"]}`，与指令一致） |
| ToolCall 终态 | `settled`，`dispatch_count=1`，`points_settled=10`，`points_reserved=0`，无 error_type |
| 钱包 | 1000 → 990，reserved 回到 0，净支出**恰 10** |
| 数据库 Evidence 增量 | **0**（新 Pi 路径的预期事实：标准 MCP Tool Result 由 adapter 直接交给模型，不写数据库 Evidence） |
| Artifact/Draft/Revision/Version/Publication | 0/0/0/0/0（本 Smoke 不要求生成，0 是合法结果） |
| 模型最终回答 | `DIRECT_MCP_OK 蔚来`（与直连只读对照结果一致：直连返回非空紧凑 JSON，模型 thinking 明示工具返回含匹配标签「蔚来」） |
| 事件顺序 | `message.completed` 先于唯一 `run.completed`；无第二 Attempt、无恢复重放、无 reconciliation 行 |
| mcp_result_v1 / result_unavailable / result_unknown 业务分类 | 未进入 Pi 新路径 |
| 进程/端口 | FastAPI 与 Gateway SIGTERM 有界退出，无残留监听 |
| 日志 | 5 项 secret 逐字比对 0 命中；无完整 MCP payload 落盘 |

## 3. 流程偏差（如实记录）

授权的 DataTap 直连只读对照上限为 **1 次**，实际执行 **2 次**：

1. 第 1 次：同参数只读调用（对照脚本投影函数遗漏单数 `tag` 键，误判 EMPTY）；
2. 第 2 次：同参数诊断重复调用（仅输出结构形状，用于交叉验证）。

两次均为同参数只读调用，未发生第三次；生产业务 dispatch 仍严格 1 次。该偏差不推翻真实
生产链路验证结论，但本 round **不得表述为「严格零偏差授权执行」**。

另记录（不影响会计恒等式）：模型在 turn 1 有一次被本地拦截的 mcp 代理尝试（无 tool_call
行、0 外发、0 扣费），turn 2 完成真实业务 dispatch。

## 4. 架构接受结论

```text
DIRECT_MODEL_MCP_SMOKE_FUNCTIONALLY_ACCEPTED_WITH_PROTOCOL_DEVIATION
```

新 Pi production path 的事实得到真实链路验证：

- 标准 MCP Tool Result 原样交给模型（模型正确读取并引用真实返回值）；
- accounting finalize 只传 metadata，计费恒等式成立（durable-before-send、净支出 10、reserved 归零）；
- Pi 路径不写数据库 Evidence、不使用 `mcp_result_v1` 分类、无 required artifact 门禁；
- Run 在无 Artifact 的情况下合法 completed（CompletionValidator 不以 Evidence/required artifact 为完成条件）。

## 5. 边界声明

- 本结论只覆盖本次最小 Smoke；**不是 Web Functional Scenario 2 PASS、不是 B7 PASS、不是生产就绪**。
- 下一步 Web Functional Scenario 2 必须由用户按新授权模板逐字重新授权，execution HEAD
  必须是 `c01ec1ba…` 的线性后代且包含 `33d37c0`/`0d87d4e`/`96e8fd9`/`c01ec1b`。
- 历史失败 round（`REAL_B7_20260812T045636Z_b801c490`）、旧 Functional Scenario 2 失败
  round、历史 reserved=30 与旧证据目录均未修改，保持只读封存。
- 本文件不含完整 MCP payload、凭证、DSN、未脱敏手机号。
