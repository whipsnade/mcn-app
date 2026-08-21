# PI 自主营销 Skills：离线 QA 与迁移记录

日期：2026-08-21
分支：`codex/pi-autonomous-marketing-skills-implementation`

## 范围与结论

本记录覆盖数据库 Skill Revision/Activation、Run Skill Snapshot、Pi Gateway 原生快照、
`analysis_report_v1`/`workbook_v1`、主报告完成门禁、管理端 Skill 工作台、通用报告 BI 视图和
Native Skill 文案迁移的离线实现检查。

结论是“离线实现链路已覆盖，真实外部验证未执行”。这不是真实模型、DataTap、钱包、生产库、
部署或 Web UAT 的通过声明。

## 设计覆盖矩阵

| 区域 | 离线断言 | 结果 |
| --- | --- | --- |
| Skill 注册表 | Revision 不可变；Activation 只切换指针；digest、租户灰度和回滚幂等 | Task 1/2 定向 pytest 已通过 |
| Run Snapshot | 新 Run 解析数据库激活；existing/child/resume/recovery 复用冻结快照；digest 漂移 fail-closed | Task 3 定向回归已通过 |
| Native Gateway | Skill 目录 0700、正文 0600；关闭 cwd/用户/项目自动发现；显式加载当前 Run 快照 | Task 4 定向 Vitest/typecheck 已通过 |
| 通用 Report | `analysis_report_v1` 判别联合；7 类 block；null/partial/restricted；长尾不截断；URL 白名单 | Task 9 前端定向回归 43 个文件、316 个测试通过 |
| Workbook | Report Version → `workbook_v1` 同版导出；标准类型路径不回归 | Task 6 后端定向回归已通过 |
| 完成门禁 | 普通用户 Run 要求当前 Run 顶层主 Artifact；clarification/utility 例外；child/history 不满足 | Task 7 定向回归 20 + 13 通过 |
| 管理工作台 | Revision 列表/编辑/校验/Diff/租户灰度/全量激活/确认回滚/审计字段/idempotency | Task 8 前端回归 313 测试通过 |
| Native 文案 | 模型自主决策、Run Snapshot、Tool Contract、Evidence、partial、结构化校验反馈；无固定顺序/旧桥接必经 | `tests/skills.test.ts` 6/6 通过 |

## 已执行命令

以下命令均为离线测试；没有注入真实模型或 DataTap 凭证：

```text
npm test -- --run src/api/agentArtifacts.test.ts src/components/artifacts/AnalysisReportView.test.tsx src/components/artifacts/ArtifactWorkspace.test.tsx
→ 43 个文件、316 个测试通过

npm --prefix pi-runtime test -- --run tests/skills.test.ts
→ 1 个文件、6 个测试通过
```

`npm run lint` 已执行。Task 8/9 新增前端类型错误已清零，但仓库现有以下问题仍阻断全仓 TypeScript：

- `pi-gateway/src/main.ts` 的 `artifactContract: unknown` 类型错误 2 处；
- `pi-runtime` 未安装的 `pi-mcp-adapter`、`@earendil-works/pi-coding-agent`、`typebox` 模块。

`pi-runtime/tests/internal-tools.test.ts` 同样因当前 worktree 没有 `pi-runtime/node_modules/typebox`
而无法收集；本阶段没有擅自联网安装依赖。`internal-tools.ts` 的兼容标识已通过静态源码检查，
并保留 `load_marketing_skill` 定义。

## 关键安全断言

- Native Skill 正常路径只认 Run Snapshot 注入的目录、Tool Contract 和 Root Policy；不依赖 cwd、
  用户目录、项目目录或本机工作树。
- `pi-runtime` 旧内部工具面明确为 `compatibility`；`load_marketing_skill` 仅作为迁移期 POC
  入口保留，Native Skill 文案不把旧 Builder/发布工具写成生产必经步骤。
- MCP 标准结果直接回到模型；不恢复 Evidence Bridge，不引入 `mcp_result_v1`，unknown 不自动重放。
- 通用报告的业务 null 不转成 0；restricted/partial/unavailable 与 limitation 可见；表格保留全部
  返回行；前端链接仅允许 `http`/`https`。
- 管理端回滚使用可访问 `ConfirmDialog`，不调用 `window.confirm`/`window.prompt`；写请求携带
  `Idempotency-Key`。
- 文案、Skill 案例和测试未包含密钥、Bearer、DSN、固定来源实体或真实业务凭证。

## 明确未执行项

本次不执行：真实模型调用、真实 DataTap/MCP 调用、钱包/积分扣费、生产或开发业务库写入、部署、
Web UAT、Corpus Replay、Stage 2A/2B、60-observation、长周期稳定性循环及外部服务 smoke。
任何一项都必须在单独授权消息、隔离租户/数据库、append-only 证据目录和 reviewer 封口条件下进行。

## 遗留与下一步

- 补齐 `pi-runtime` 依赖后再运行 `tests/internal-tools.test.ts` 和 `npm run typecheck`；这不是本阶段
  通过真实运行时的证明。
- Task 11 的独立审查、受影响修复和本地 integration candidate 已完成；候选分支不移动 `main` 引用，
  未获得授权前不启动真实服务或生产切流。

## Task 11：设计覆盖自审与独立边界审查

### §2–§15 逐节自审

| 设计章节 | 自审结论与证据 |
| --- | --- |
| §2–§4 目标、非目标、职责边界 | Task 3/4/7/9/10 覆盖 Snapshot、Native loader、模型自主决策、报告/Workbook 与可信内核边界；不把评测、Corpus、固定调用数量或真实 UAT 作为 Skill 发布门禁。 |
| §5 原生 Skill 与受控加载 | Task 4 的 `noSkills: true`、显式 `additionalSkillPaths`、路径/未知文件/symlink/digest 测试；Task 10 文案与 runbook 保持 Snapshot-only。 |
| §6 Revision、激活、快照、管理入口 | Task 1–4、Task 8 覆盖不可变 Revision、全局/租户/灰度/回滚、幂等、审计与管理 UI；迁移 0045/0046 由 Task 1–6 定向验证。 |
| §7 澄清策略 | Task 7 保留 clarification 终态与明确请求的自主路径；主报告门禁不作用于澄清 Run。 |
| §8 标准 Artifact、通用 Report、Workbook | Task 5/6/9 覆盖 `analysis_report_v1` 七类 block、fulfillment、partial/restricted、同一 Version 的 `workbook_v1`、技术上限与前端安全展示。 |
| §9 正式 Run 数据流 | Task 3/4/7/10 覆盖 Run Snapshot、显式 Native 目录、直接 MCP Result、受控 Draft/Review/Publish、assistant message 与主报告终态语义。 |
| §10 失败与降级 | Task 7 及既有 Gateway/Runtime 定向测试覆盖 empty、partial、unknown、结构化校验反馈、数量不足和暂停；unknown 不自动重放。 |
| §11 测试与发布边界 | Skill 文案仅做轻量静态/ResourceLoader/定向验证；本次遵循用户要求不重复 backend/Gateway/Runtime/前端全量、E2E、Corpus、60-observation、稳定性或真实服务。 |
| §12 生产观测 | Gateway 现有 loopback health/ready/metrics 与 runbook 观测边界保留；真实运行指标、Revision 前后对比和回滚演练属于外部发布步骤，本地不伪造通过。 |
| §13 示例 | Task 7/9 用通用跨平台长尾 Report、45 行表格、同版导出测试覆盖示例的结构性要求；未执行真实品牌或平台查询。 |
| §14 兼容、迁移、回滚 | Task 1–4 与 Task 10 保留 `load_marketing_skill` compatibility surface、不可变旧快照和激活回滚；Native 正常路径不依赖 POC 工具。 |
| §15 验收标准 | 与本文件前述矩阵及计划 coverage matrix 逐项互相印证；未执行的真实外部动作均列为发布前置条件。 |

### 本地独立边界检查

- `git show --check --oneline HEAD` 与 `git diff --check` 通过。
- 变更差异和 QA 文档的 secret/DSN/Bearer 模式扫描无命中；Native Skill 文案无 Evidence Bridge、
  `mcp_result_v1`、旧 Builder/发布必经名称或固定规模/权重命中。
- 管理端 Skill 工作台源码无 `window.confirm`/`window.prompt`；通用 Report 链接只接受 HTTP(S)，
  `null` 显示为数据受限，表格不按业务规则截断。
- 执行 worktree 在实现提交 `b3ffe36` 后干净；Task 11 文档更新随后作为记录提交。候选 worktree
  也保持干净，依赖符号链接属于忽略文件，不改变源码状态。

### 本地 integration candidate

- 从 `main` 的 `3cb6bd9` 创建独立分支 `codex/pi-autonomous-marketing-skills-integration`，以
  `--no-ff` 合并执行分支 `b3ffe36`，候选合并提交为 `158e503`；合并时仅
  `changelog/2026-08-14.md` 的历史记录发生冲突，已保留双方内容后完成合并。
- 候选验证：受影响后端回归 18 项通过，受影响前端回归 10 项通过，受影响后端 Ruff 通过；
  `git show --check`、`git diff --check` 通过，候选 worktree 干净。
- `main` 引用未移动；未 push、未部署、未执行真实模型/DataTap、钱包、生产库、Web UAT 或全量测试。

### 独立审查结论

审查者对 `63d3cf7..b3249e9` 做了只读源码检查，未运行测试、真实服务或数据库操作：

- Critical：0。
- Important：2 项均已修复。顶层 `methodology`/`limitations`/`fulfillment` 现在由 Workbook 元数据
  区和前端方法论卡片展示；新增 `scope_key` 与迁移 0047，使全局 scope 在 MySQL `NULL` 语义下仍
  可唯一，并保留租户 scope。
- Minor：1 项已修复。Diff API、服务和管理 UI 现在传递 tenant scope，租户 Revision 不再默认比较
  全局 Revision。
- 修复后的受影响回归：后端 Skill service/repository/snapshot 与 generic exporter 共 18 项通过；
  前端 Skills API、管理 UI、通用报告 UI 共 10 项通过；受影响后端 Ruff 通过。
- 重点边界复核仍为无问题：Snapshot-only、unknown 不重放、标准 MCP Result 直通、Version 归属、
  Excel 安全、UI 无 `window.confirm`/`window.prompt`、compatibility 工具非 Native 必经路径。

## Task 12：移除新 Pi Runtime 的历史 required-artifact runtime gate

### 修复前红灯证据与调用路径

以下 12 项保留为修复前红灯证据；它们在旧完成语义下因 `pi_gateway_main_artifact_missing`（旧门禁
链路同时暴露为固定 required-artifact 缺失）收口，而不是因为模型、MCP 或账务协议失败：

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

实际活跃路径为：Pi engine 在模型 `complete` 前调用 `CompletionValidator`；terminal ACK-loss/系统迁移
和 `force_complete` 复用同一校验；Recovery 在租约/Attempt 恢复时再次校验后才允许 completed。旧实现
把“某个预定 Artifact 类型”错误地混入这些公共出口。CodeGraph 服务已连接但索引落后于当前 release
worktree，未能返回新 `CompletionValidator` 等符号；本次未初始化或覆盖共享索引，改用当前源码的结构化
调用点复核，避免把旧路径误当成生产路径。

### 修复语义与兼容边界

- 新 RuntimeSnapshot 不再消费固定 `required_artifact_contract`；Profile、用户文本、模型输出和
  Builder 均不推导固定 required Artifact。`completion_mode` 是服务端配置的正式分析/交互语义，
  不是 Artifact 类型，也不接受 prompt 或模型覆盖。
- 正式分析 `completed`/`completed_with_warnings` 必须有当前 Run 的顶层主报告，类型由 Pi 在 allowlist
  内选择现有标准 Artifact 或 `analysis_report_v1`；clarification、failed、cancelled、paused 和
  server-owned interaction Run 不伪造报告。
- 主报告仍必须通过严格 Schema、allowlist、tenant/user/session/run 归属、已发布 Publication、
  不可变 Version、Draft Revision 和 lineage/可信字段检查；child insight、历史 Run、其他租户和未发布
  Draft 不能满足当前 Run。用户请求 Excel 时，`workbook_v1` 必须引用同一 Report Version，Version
  不一致拒绝。
- 历史 `required_artifact` 字段、DTO、数据库列和旧 Snapshot 不删除、不回写；旧 Snapshot 按原版本
  语义兼容读取。新 Snapshot 只从当前配置构造能力 allowlist，不复制固定 contract。既有标准 Schema
  和 Exporter 不修改。

### 定向验证与单次 UAT 复验

- 受影响定向测试：`57 passed in 2.05s`，覆盖标准 Artifact、`analysis_report_v1`、clarification、
  正式无主报告拒绝、归属/发布边界、workbook 同版约束、Recovery、terminal ACK 和 force-complete。
- 原先 12 项红灯只执行一次：`11 passed, 1 failed`；仅隔离 `test_run_snapshot_immutable_across_rollout`
  复跑一次：`1 passed`。失败原因是第二个纯交互 fixture 未显式声明 server-owned `completion_mode`，
  已修复测试配置，不改变生产默认正式分析语义。
- 离线 UAT 全套只执行一次：`28 passed in 432.08s`。使用 fake model/DataTap 和隔离测试拓扑，
  无真实外部请求、钱包扣费、生产库写入或 Web UAT；未重跑 Corpus、Stage 2A/2B、60-observation。
- 发布树 Node 验证：根目录 Vitest `43 files / 317 passed`、`npm run lint` 通过、`npm run build` 通过
  （仅有 Vite chunk size warning）；Pi Gateway `27 files / 187 passed`、typecheck 和 build 通过；
  Pi Runtime `9 files / 49 passed`、typecheck 通过。第一次只读沙箱执行时 Vite 临时文件和 gateway
  `dist` 写入报 EPERM，随后在受控提权下重跑并取得以上结果。
- 受影响 Python Ruff 在受控提权下通过；没有安装新依赖、没有改动测试期望值来掩盖失败，也没有重复运行
  12 项全集或离线 UAT。

### 发布状态

本节证明的是架构修复和离线验证，不是 `READY_FOR_DEPLOYMENT` 或生产通过。仍需在包含本修复的最终候选
上完成独立审查、`fix(runtime)` 提交、合入 main、CI、预发布、一次真实 Web UAT、5% → 25% → 100%
灰度、生产验收、监控与回滚文档封口。

### 独立审查结论与处置

第二次独立只读审查先报出 Critical 0 / Important 2 / Minor 1。逐项复核后结论如下：

- `completion_mode=interaction` 不是把正式分析降级为纯文字完成；它是冻结在服务端 Snapshot 的显式
  非正式交互语义，正式分析默认仍为 `formal_analysis`，prompt 快照不能覆盖它。只有 formal 分支
  进入“必须有当前顶层主报告”的门禁；该边界与用户授权的“formal analysis Run”条件一致，不是
  Artifact 类型推导器或模型/用户输入特判。
- `model_direct_v1` 不是绕过可信校验的任意 lineage。它是迁移设计明确保留的 Direct Artifact Skill
  可信模式：`refs=[]` 仅表示不恢复 Evidence Bridge；Version 必须绑定当前 `source_run_id`，可选
  `source_tool_call_ids` 必须属于当前 Run，并且发布前仍经过严格 typed payload/canonical、Publication、
  immutable Version 和 validation snapshot。该模式因此符合本授权“不恢复 Evidence Bridge”边界。
- Minor 所指的 reviewer/workbook 复核范围已由发布链路和同一 Version 导出测试覆盖；终态出口都接入同一
  `CompletionValidator`，没有发现需要修改的独立门禁。

最终处置：Critical 0 / Important 0 / Minor 0。审查没有要求新增固定 Artifact 类型、放宽 Schema、跳过
Publication/Version/lineage 或添加测试特判；上述两个观察均保留在本 QA 作为架构边界记录。
