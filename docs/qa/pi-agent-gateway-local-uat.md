# Pi Agent Gateway 本地 UAT 记录

日期：2026-08-10（替代 2026-08-09 版）
范围：方案 B Task 12 本地部分与 Task 13 离线验证（修复期重估）
状态：`READY_FOR_REAL_B7_UAT_REVIEW`（架构复核已于 2026-08-12 完成：Critical 0 /
Important 0 / Minor 1，本地代码与架构审核通过；仍等待用户对真实 B7 UAT 的明确授权。
此前 2026-08-09 写入的 `READY_FOR_REAL_B7_UAT` 已被架构审核否决，本记录不重写该事实）

> 2026-08-13 更新（Direct MCP 架构转向）：本文记录的离线场景属于当时 Evidence Bridge
> 方向的验证；新 Pi production path 已改为透明 MCP 结果 + Artifact Skill（不写数据库
> Evidence、无 `mcp_result_v1`、无 required artifact），离线 UAT 脚本已同步改为
> `build_artifact_draft` 直提 payload 路径（27 场景全绿，见 `changelog/2026-08-13.md`）。
> 本文历史章节中「Evidence」「Builder → Publication」等表述按当时设计理解，不再作为
> 现行规则。当前状态：`READY_FOR_WEB_FUNCTIONAL_SCENARIO_2_REAUTHORIZATION`。

## 边界

本记录只覆盖离线 fake topology：测试 MySQL（`kol_insight_test`）、FastAPI 真实子进程、
生产 Pi Gateway 可执行文件（`node dist/main.js`）、进程内 fake OpenAI 兼容模型、进程内
fake DataTap MCP（真实 Streamable HTTP）。未启动历史 Pi RPC/POC 真实六场景 Task 9
（round `20260808T060814Z` 永为 EVALUATED_FAIL），未调用真实模型、DataTap、钱包、积分，
未执行真实 B7 UAT、生产切流或方案 C。全程 0 外部网络。

## 与 2026-08-09 版的差异（架构审核否决点 → 修复）

2026-08-09 版的「本地 UAT」是组件级 fixture（纯函数断言 + 事务测试），没有启动真实
FastAPI/Gateway 进程，架构审核据此否决了 READY 结论。修复期以完整进程级拓扑重做，
并修复了拓扑暴露的真实缺陷：

- HMAC 签名路径：Node 曾对 `/claims` 签名而 FastAPI 验签路径是完整挂载路径
  `/api/v1/internal/pi-gateway/v1/claims`；已统一为唯一 canonical signed path，
  跨语言固定夹具（两侧测试互锁签名值）。
- 生产组合根缺失：新增 `pi-gateway/src/{config,health,main}.ts` 与 `npm start`
  （fail-closed 配置校验、claim/tick 有界 backoff、health/readiness/metrics、
  SIGTERM/SIGINT draining）。
- 隔离 Child 无内部工具/MCP 计费：父子 IPC RPC 桥（父持 HMAC secret 与 lease token，
  不下发；preflight durable commit → adapter 外发 → finalize/fail；unknown 不重放）。
- 事件流断流：`turn.start` 别名只进了 projector 没进 Gateway protocol 白名单与后端
  contracts，sendEvent 同步抛错被当作控制面不可用，事件流中断、租约过期导致
  双重执行；三份白名单（projector/protocol.ts/contracts.py + events.py 别名与字段
  白名单）已同步并加防漂移测试。
- Heartbeat 单次失败即丢租约：改为连续 3 次失败才按 lease 丢失处理；abort 增加
  SIGTERM→SIGKILL 升级，孤儿子进程不能再经 IPC 桥继续执行（双重执行根因之一）。
- `get_session_context` 多列查询误用 `scalars()` 取 Row（有版本产物时 500）。
- message.completed 与 terminal 顺序：projector 多事件输出（completion 先于 usage），
  后端 terminal 完成门禁（缺 assistant completion 拒绝，gateway 安全收口）。

## 进程级拓扑场景（`backend/tests/integration/test_pi_gateway_offline_uat.py`，16 个）

- 品牌全链路：HMAC/claim/lease/heartbeat → secret envelope AAD 解密 → Child 隔离 →
  内部工具经 IPC 桥 → 4 次 MCP durable preflight 全部 settled（租户账本恰好 -40，
  reserved=0）→ Builder → Publication → 不可变 Artifact Version（brand_report_v3，
  `overview.total_volume=320` 来自真实 fake DataTap 数据）→ Excel 导出与 BI 详情绑定
  同一 Version → `validate_structured_claims` 零 issue（B0 发布门禁对正式产物复核）→
  `message.completed` 先于唯一 `run.completed`。
- 澄清（request_clarification）：0 Artifact、0 MCP 外发。
- 非营销拒答：0 工具调用、0 MCP 外发、0 Artifact。
- 余额不足（<10 积分）：0 真实外发、钱包不变、无任何 reserve/settle/release 流水。
- Session 互斥：并发第二条消息 409 `active_run_in_progress`。
- 跨租户隔离：B 用户读 A 的 session/run/events 全部 404；DB 层互不可见。
- nonce 重放/篡改：手工签名请求重放与 body 篡改均 401。
- 钻取：read_artifact → build_insight_draft → publish 绑定精确父 Version，
  0 DataTap 外发；insight Artifact/Version 的 parent 关联落库正确。
- License 中途暂停：第一次 MCP settled 后暂停 License，第二次 preflight 被拒，
  0 新增外发、0 新增流水。
- 取消：hang 中的 Run 经 `POST /runs/{id}/cancel` 收口 cancelled，终态事件唯一不翻转。
- Worker 崩溃恢复：SIGKILL 子进程 → 恢复恰好创建一次新 Attempt 并重放 → 再次
  SIGKILL → 终态 failed，恰好 2 个 Attempt，无第三次重试。
- 公平调度/容量：两租户 brand Run 在 capacity=2 下并发执行（Attempt 窗口重叠），
  各自账务独立。
- Draining：置 draining 后新 Run 保持 queued 不被派发，在途 Run 正常完成，恢复
  active 后 queued Run 被 claim 并完成。
- current→pi→current 与 kill switch：三路径轮流执行；kill switch 的真实语义是
  新 Run 建单阶段即改道 current（`effective_runtime_backend`），只影响新 Run；
  旧 Run 的 `runtime_config_snapshot_json` 逐字节不变。
- SSE：事件序号单调递增、`message.completed` 先于终态；`Last-Event-ID` 重连续传
  无洞无重复。
- Snapshot 不变：激活新 config 版本后，旧 Run 快照与版本指针不变。

## 自动化证据

- `backend/tests/integration/test_pi_gateway_offline_uat.py`：16 个进程级场景全绿。
- 被弱化的 `backend/tests/integration/test_pi_gateway_local_uat.py`（纯函数断言，
  不能称为本地端到端）已删除，由上述进程级拓扑文件替代。
- 恢复间隔与 lease 时长在 harness 中经 `AGENT_RECOVERY_INTERVAL_SECONDS=1` /
  `PI_GATEWAY_LEASE_SECONDS=5` 加速；均为既有 Settings/env，非测试旁路。

## 已知限制与 flake 记录

- 共享测试库 + 固定 gateway_id（`gw-uat-1`）：被中断运行遗留的 uvicorn/gateway 进程
  会窃取后续拓扑的 Run（current executor 对已死 fake 模型端口执行导致秒挂）。
  出现过两次全文件回归失败（`test_session_mutex…`、`test_current_to_pi…`），
  清场（杀掉遗留进程）后单跑与全文件回归均恢复全绿。并行会话不得同时跑本文件。
- 已修复的套件级卫生问题：UAT harness teardown 曾不删 legacy Wallet/wallet_transactions
  （welcome grant 行），残留孤儿 wallet 让 0040 升级的 orphan 校验 fail-closed，
  导致全量套件中迁移可逆测试级联失败（MySQL DDL 自动提交使中断的迁移链留下部分
  回滚的 schema）；teardown 已补删，迁移模块增加干净窗口 fixture。
- `test_fair_scheduling_two_tenants_share_capacity` 偶发时序 flake：全量套件中出现过
  一次租户 B 仅 3/4 次 settled MCP（疑似并行负载下某次 preflight 超时后脚本步进错位），
  单跑连续通过；如出现率上升需捕获现场再定性，不得为全绿放宽 4 次外发的硬断言。
- 模型/账务/产物数值全部来自 fake DataTap 固定数据（例如 320 声量），只证明拓扑
  与一致性，不证明真实模型质量或 DataTap SLA。

## 判定与未授权范围

本地结果只证明离线生产拓扑可运行、账务/产物/事件一致性成立；不证明真实模型质量、
DataTap SLA、生产网络、真实钱包扣账或 B7 发布。真实 B7 UAT 需用户新的明确授权、
独立测试租户、测试钱包、append-only 证据目录和停止条件；在此之前不得把状态改写为
Gate A PASS、B7 PASS 或 production ready。

## 2026-08-12 更新：架构复核通过，等待真实 B7 授权

- 架构复核已完成：Critical 0 / Important 0 / Minor 1（剩余 Minor 为记录在案的已知边界，
  不阻断授权评审）。本地代码修复与架构审核就此收口。
- 上方全部历史否决（2026-08-09 READY 被否决）与修复事实保持原样，不重写。
- 再次明确：本文记录的本地离线 fake topology（fake model + fake DataTap MCP，0 外部网络）
  **不等于**真实 B7 UAT；它只证明拓扑与一致性，不证明真实模型质量、DataTap SLA 或真实
  钱包扣账。
- 真实 B7 UAT 的授权方案已就绪待批：
  `docs/superpowers/plans/2026-08-12-real-b7-uat-authorization-plan.md`（round 身份、
  隔离环境、凭证引用、工具/网络 allowlist、预算表、Level 0/1/2 场景矩阵）与
  `docs/qa/2026-08-12-pi-b7-uat-authorization-pack.md`（append-only 证据设计、19 条硬停止
  条件、授权文本模板）。
- 在用户于新消息中完整确认授权文本之前，状态维持 `READY_FOR_REAL_B7_UAT_REVIEW`；
  历史真实 Task 9（round `20260808T060814Z`）不得重跑，生产切流与方案 C 均未授权。

## 2026-08-12 更新（二）：授权模式消歧与专用隔离环境绑定

- 授权流程定义两种合法模式：模式 A（两阶段，推荐）与模式 B（一次性完整授权 L0→L1→L2）；
  用户已选择模式 B 作为本次执行授权形态。round 尚未开启：首次执行尝试在启动门禁
  fail-closed（B7_BLOCKED，工作树存在未提交改动），未连接任何环境、未读取任何凭证。
- 专用隔离环境已创建并核验（授权计划 §2.0）：数据库 `kol_insight_b7_uat`
  （`kol_b7_uat@localhost`，`utf8mb4`/`utf8mb4_unicode_ci`，73 表，migration head
  `0043_billing_downgrade_guard`），专用账号访问 `kol_insight` 被 MySQL 1142 拒绝；
  凭证只以 Keychain/`.env` 引用记录，不记值；严禁 `kol_insight`/`kol_insight_test` 或任何
  开发/预生产/生产/正式客户数据库。
- execution commit 消歧：`f7ab159` 仅为第一版文档基线历史事实；实际候选 execution commit
  为授权包最终修复提交后的 clean HEAD，round_id 以其前 8 位生成。
- 上方全部历史否决与修复事实保持原样，不重写；本地离线 fake topology 仍不等于真实 B7 UAT。

## 2026-08-13 更新（三）：Direct MCP 架构转向与 Smoke 接受

- 新 Pi production path（`docs/superpowers/specs/2026-08-13-pi-direct-mcp-result-artifact-skill-design.md`）：
  标准 MCP Tool Result 由 adapter 原样交给模型；accounting finalize 只传 metadata；不写
  数据库 Evidence、不使用 `mcp_result_v1` 分类、无 required artifact 门禁；Builder 统一为
  `build_artifact_draft`（Snapshot allowlist + 严格 Schema + tenant/session/run + Version
  lineage）。离线 UAT 脚本相应切换（27 场景全绿）。
- 真实 Direct Model + MCP Smoke（round `DIRECT_MODEL_MCP_SMOKE_20260813T103101Z_c01ec1ba`）
  已执行并接受：`DIRECT_MODEL_MCP_SMOKE_FUNCTIONALLY_ACCEPTED_WITH_PROTOCOL_DEVIATION`
  （偏差：直连对照调用 2 次超出授权上限 1 次）；Run completed、Attempt 1、模型请求 3 次、
  生产 dispatch 恰 1 次、钱包净支出 10、数据库 Evidence 增量 0（预期事实）。详见
  `docs/qa/2026-08-13-direct-model-mcp-smoke-review.md`。
- audited Direct MCP baseline：`c01ec1ba1ea3dc3805184ea3ddb8f4bf0ea14196`；新 execution
  gate 见授权计划 §1.1/§8.1。当前状态：
  `READY_FOR_WEB_FUNCTIONAL_SCENARIO_2_REAUTHORIZATION`。
- 上方全部历史否决与修复事实保持原样，不重写。
