# Post-brand 营销功能架构设计复核记录

## 复核范围与身份

- Goal：`PI_POST_BRAND_FUNCTIONAL_DESIGN_GOAL`
- 基线：`origin/main@d0a16b9524c556c31bb894916069cc02ec2cd131`
- 设计分支：`codex/post-brand-functional-design`
- 被审文件：
  - `docs/superpowers/specs/2026-08-22-post-brand-marketing-functional-design.md`
  - `docs/superpowers/plans/2026-08-22-post-brand-marketing-functional-plan.md`
- 审核者：独立只读 reviewer `design_reviewer`；主会话只根据审核意见修改设计与计划，审核者没有写文件或修改 Git。

本轮只审核架构与实施计划。没有实现 Python、TypeScript、JSON、migration、测试或 Skill 正文，也没有启动服务、读取真实凭证、执行模型/DataTap/钱包/Web UAT、部署、push 或合并 main。

## 事实核对方法

主会话先读取 `AGENTS.md`、最新三篇 changelog、指定计划/QA/runbook，再用 CodeGraph 做结构定位；对 CodeGraph 尚未覆盖的最新目录与严格协议边界，直接只读当前基线源码。重点核对了：

- `SkillRevision` / `SkillActivation` / Run Snapshot 的现行解析和恢复语义；
- Pi Gateway claim、Skill manifest digest、标准 MCP Result 与 accounting metadata；
- 主 Artifact completion gate、clarification、cancel/resume 与 SSE 终态顺序；
- `kol_value_score_v3`、KOL/analysis model input、Version/Workbook/export 与 Skill 管理工作台；
- 当前 migration head `0049_skill_rollout_history`、production pack `marketing-v2@1.1.0`，以及成功 B Run 的保留记录。

## 审核轮次与修订

### 第一轮：C1 / I5 / M1，未通过

| 级别 | 发现 | 设计修订 |
|---|---|---|
| Critical | KOL 官方评分仍可能建立在模型提交的原始事实、字段映射和去重规则上 | 增加 `source_bound_input_v2`：只接受当前 Run settled Tool Result 的完整原样副本、调用 ID 与行指针；adapter 计算非语义 canonical hash/bytes，服务端核对归属、账务 hash、审核字段映射后，才生成内部 `BoundSourceRows`；分数、rank、rating、去重与 fulfillment 全部 server-owned |
| Important | 原地替换 Builder 输入合同会让旧 Skill Snapshot 与新 schema 错配 | 为 `SkillRevision`、`skill_manifest_v2` 和 `RuntimeConfigSnapshot` 增加不可变输入合同版本；旧 Run/resume 固定 v1，新 Run 按冻结 Snapshot 选择 v1/v2 |
| Important | 历史钻取可经 Session 级 history 工具读取未选定 Version | 钻取入口冻结 artifact/version/version_id/payload_hash 与 lineage allowlist，只开放精确 scoped `read_artifact/read_tool_result` |
| Important | 钻取 profile 允许 `ask_user`，可逸出为 clarification child | 钻取 profile 只允许 `call_tool/complete`，不允许 `ask_user` 或 `search_evidence` |
| Important | Revision 4/KOL/analysis successor 在真实 UAT 前可能成为新环境默认 | 成功 B Snapshot/rev3 保持已验收默认和回滚基线；所有 successor 只入库为 candidate，initializer 不激活，真实 UAT 后才可另行推广 |
| Important | 成功 Snapshot 的非 root Skill 可能按当前 Activation 或时间猜 source Revision/scope | Task 1 要求 manifest 全 entry 的显式 `revision_id + scope_key` source map，候选扫描只提供无正文元数据，不自动选取 |
| Minor | “每场景一个业务 Run”没有区分零副作用 clarification parent | 授权包同时冻结 `max_data_bearing_runs` 与 `max_total_user_runs`；澄清场景最多 1 个 data-bearing child、2 个总用户 Run |

### 第二轮：C0 / I1 / M0，未通过

唯一 Important 是 backend `skill_manifest_v2` 尚未成套进入 Pi Gateway 的严格 claim/digest 边界。设计与 Task 2/9 随后补齐：

- `pi-gateway/src/protocol.ts`、`main.ts`、`skill-snapshot.ts` 及对应两个测试文件；
- 无 discriminator 的历史 v1 继续使用原七字段和原 digest bytes；
- v2 exact keys 包含 `revision_id/scope_key/model_input_contract_version`，digest 纳入 discriminator 与新字段；
- claim 保留并校验 `artifact_input_contract_versions`，缺失、未知、篡改、冲突均在 worker spawn 前 fail-closed；
- Python/TypeScript 共用 v1/v2 golden vectors；Task 9 汇总命令包含 Pi claim/digest 测试、typecheck 与 build。

### 第三轮全量 Gate：C0 / I0 / M0，通过

同一独立 reviewer 对修订后的 spec/plan 做全量复核，确认以下 Gate 全部通过：Pi 自主性、无固定阶段/工具顺序/次数、标准 MCP Result 原样、无模型可见 Evidence Bridge、来源绑定与 server-owned 评分、主报告与合法例外、clarification 零副作用、cancel/unknown/预留、Skill v1/v2 Snapshot、不形成 seed/DB 双事实源、candidate 不提前推广、BI/Excel 同版、牛霸霸 Workbook、Version-bound read-only drilldown、campaign 排除、10-Task 可执行性、一次性离线门与真实 UAT 授权边界。

### 交付级字段一致性确认：C0 / I0 / M0，通过

主会话在终审后发现 spec 将 candidate bundle 标志误写为 `candidate_activation=false`，遂只做术语修正：candidate 明确为 `default_activation=false`、`candidate_activation=true`，且 initializer 不为其创建 Activation。独立 reviewer 再次只读确认该句与 plan 的测试、initializer 和 KOL/analysis candidate 语义一致，第三轮全量 Gate 结论不受影响。

## 最终审核结论

**Critical 0 / Important 0 / Minor 0，READY。**

审核者确认全程只读：未编辑文件、未修改 Git、未运行测试或服务、未访问模型、DataTap、钱包或 Web UAT。

## 交付自检

- spec 明确比较 A/B/C 并推荐 A；18 个主题章节覆盖七部分范围、数据流、状态流、权限/计费/安全、错误语义、迁移兼容与风险延期。
- 16 项必须决策均有结论，没有 TBD/TODO/FIXME/占位。
- plan 恰好 10 个 Task、62 个 checkbox；每个 Task 均有文件、输入、输出、接口/依赖、RED、GREEN、受影响验证和 commit。
- 所有 `Modify` 路径在当前基线存在；所有 `Create` 路径在当前基线尚不存在。
- campaign 只出现在排除声明、共享兼容断言和负向验证中，没有 campaign 专项 Task、Schema、Skill、视图、UAT 或 Pack 1.3.0 计划。
- 本设计没有授权实施、真实 UAT、部署、push 或 main 集成；后续必须新开授权会话。
