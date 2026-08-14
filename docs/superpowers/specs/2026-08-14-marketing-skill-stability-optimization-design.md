# Marketing Skill 稳定性优化设计

> 状态：用户已确认设计；品牌阶段实施计划已生成，尚未执行
>
> 日期：2026-08-14
>
> 当前基线：`main@f2a0b68a5f4c50c920e2de053960509ee844f950`
>
> Capability Pack 基线：`marketing-v2 / 1.1.0`

## 1. 背景

第一阶段已经证明 Direct MCP 与 Direct Artifact Skill 的真实业务链可用：标准 MCP Tool
Result 直接交给 Pi/模型，模型按动态 `model_input_contract` 构造业务输入，服务器确定性补齐
正式 Artifact 的服务器字段，最终完成 Draft → Publication → Version → BI/Excel 同版。

当前三份专项 Skill 仍以通用步骤为主：

- `brand-research-report`
- `campaign-evaluation-report`
- `kol-selection-report`

它们已经告诉模型“加载 Schema、构造 payload、按错误修正并发布”，但没有充分说明如何从
真实 MCP Result 判断章节覆盖、何时停止补查、如何保持指标口径一致，以及怎样一次性组织
`availability`、`limitations` 与 narrative。真实品牌 Scenario 2 虽已成功，仍发生 22 次模型
请求、21 次业务 dispatch 和 4 次 Draft 构建，说明稳定性仍有优化空间。

本阶段以稳定发布为第一目标，效率为防回退指标，不重构 Runtime。

## 2. 已确认决策

1. 优先级为稳定性；安全与数据真实性高于调用数量。
2. 第一轮只改 Capability Pack 提示内容、Skill references 和评测体系；不改 DTO、Artifact
   Schema、内部工具、MCP adapter、Gateway 或 Runtime 完成门禁。
3. 优化顺序为品牌分析 → 活动分析 → 达人圈选。
4. 使用真实模型；批量对比使用冻结的完整原始 MCP Tool Result，胜出候选再执行一次真实
   模型 + 真实 DataTap 端到端验收。不使用 fake model。
5. Skill 与模型接收完整原始业务数据：不脱敏、不摘要、不裁剪、不改写。Token、认证头、
   DSN 等传输凭证不属于 Tool Result，继续禁止进入模型上下文。
6. 关键范围不明确时允许先澄清；澄清轮不算正式执行。范围明确并进入专项 Skill 后，每次
   执行都必须发布对应报告。
7. 完整数据发布 complete 报告；缺失、空结果、超时或工具失败时仍发布合法 restricted
   报告，并准确填写 `availability` 与 `limitations`。
8. 正式执行只输出文字、没有 Publication/Version，评测直接判失败。
9. 业务场景通过率门槛为 90%，安全断言必须 100%；Draft 最多允许一次字段级纠错。
10. 达人圈选首轮保持 Skill-only 边界。若评分契约导致无法达到 90%，停止晋级，另开
    “确定性评分接入”设计，不用更长提示词掩盖架构缺口。

## 3. 目标与非目标

### 3.1 目标

- 提高三类专项执行一次产生可发布 Artifact 的概率。
- 提升首次 Draft 合法率，把 Draft 调用控制在最多两次。
- 让模型在数据缺失或工具失败时稳定生成受限报告，而不是循环补查或只返回文字。
- 保持原始指标的时间、平台、单位和业务口径一致。
- 建立可重复的真实模型 A/B 评测，使 Skill 改动可量化、可回归、可审计。
- 在稳定性达标的前提下，避免模型请求、MCP 调用、token 或耗时明显恶化。

### 3.2 非目标

- 不恢复 Evidence Bridge、`mcp_result_v1` 或数据库 Evidence 必经链路。
- 不规定固定 MCP 工具、固定顺序或固定调用次数。
- 不新增服务器 required artifact 完成门禁。
- 不修改模型输入 DTO、正式 Artifact Schema、builder/exporter 或 Publication 规则。
- 不修改 DataTap 服务端或设计 Marketing MCP Gateway（方案 C）。
- 不在本阶段处理历史 `result_unknown`、reserved 积分或完整 B7。

## 4. 总体架构

### 4.1 基线层

将当前 `marketing-v2 / 1.1.0` 的 Root Policy、专项 Skill 内容、manifest 与 digest 固定为
评测对照组。评测夹具保存基线内容和内容哈希，不依赖 Git 历史动态取值。历史 Run 继续使用
已持久化 Capability Snapshot，不被候选版本影响。

### 4.2 候选层

每次只优化一个专项 Skill。候选可以修改：

- 对应 `SKILL.md`；
- 为评测与维护提供的 reference；
- 为保持报告必达语义而必须同步的 Root Policy / `social-marketing-analyst` 最小表述；
- 该 Skill 的评测场景与断言。

生产模型不会自动读取 references；可复用规则必须压缩到 `SKILL.md`。完整真实样例保留在
评测语料，不把大段结果或完整 JSON Schema复制进 Skill 正文。

### 4.3 评测层

独立评测入口使用真实模型、固定模型配置、固定 Root Policy、固定工具目录与冻结原始 MCP
Result。1.1.0 基线和候选版交错运行，使用生产 `load_marketing_skill`、
`build_artifact_draft` 与 `publish_artifacts` 逻辑，在隔离测试库生成真实强类型 Artifact。

评测层不是生产 Runtime，不改变线上工具协议、完成条件或计费状态机。

### 4.4 晋级层

候选通过自动化测试、真实模型重复评测和一次真实 DataTap 验收后，才更新 Capability Pack
版本、Skill version 与 digest。新版本只影响之后创建的 Run。

## 5. 专项 Skill 的共同协议

每份专项 Skill 使用相同的紧凑四段结构。

“正式执行必须发布报告”是 Skill 行为契约与评测断言，不新增服务器 required artifact
完成门禁；Runtime 仍保持模型主导，候选若只输出文字会由评测判失败，而不是由平台伪造产物。

### 5.1 执行入口

- 判断对象、时间窗、平台及专项必要范围是否明确。
- 关键歧义存在时调用 `request_clarification`，随后立即停止。
- 范围明确后，本次执行的成功出口必须是对应 Artifact 的 Publication/Version。

### 5.2 自适应研究

- 依据目标报告章节判断当前数据覆盖，而不是执行固定工具清单。
- MCP 工具、参数、顺序和调用次数由模型自主决定。
- 原始 MCP Tool Result 直接用于分析，不写入中间 Evidence。
- 当下一次查询无法补足重要章节时停止查询，转为 restricted 报告。
- 不为了填满字段而改变指标、时间窗、平台或单位。

### 5.3 一次性组装

- 每次专项执行只需加载一次对应 Skill 及动态 `model_input_contract`。
- 同时构造 `scope`、`data`、`narrative`、`availability`、`limitations` 和
  `methodology_input`。
- 模型不提交 `schema_version`、`module`、`data_status`、`canonical_data` 或
  `field_lineage`。
- 缺失值按动态 Schema 使用合法 null/空集合，并同步标记章节状态和限制，绝不把缺失当零。

### 5.4 定向纠错与发布

- 首次 Draft 失败时只修复结构化错误返回的 RFC 6901 路径。
- 最多一次纠错，不整份推倒重写，也不因 Schema 错误重新查询 MCP。
- Draft 成功后立即 `publish_artifacts`。
- 没有 Publication/Version 即视为专项执行失败。

Skill 正文不得复制完整 JSON Schema或大段真实 Tool Result，并应保持可审阅的紧凑篇幅。

## 6. 三类专项规则

### 6.1 品牌分析

范围必须覆盖品牌、时间窗、平台和对比需求。范围明确后发布 `brand_report_v3`。

- 优先保证 overview、sentiment、daily trend、topics、top posts 的口径一致。
- `volume`、`posts`、`engagement` 不互相替代。
- 不同平台、时间窗和比较期的数据不混算。
- 用户未要求环比/同比时保持 `not_requested`，不为填充报告额外查询。
- 可选章节缺失不阻止发布，改为 restricted 并说明限制。

### 6.2 活动分析

范围必须覆盖品牌、活动身份、活动期和平台。归因或 ROI 需要额外内部数据时，缺失则受限，
但仍发布 `campaign_report_v3`。

- 活动期、活动前基线和活动后观察期严格区分。
- 平台贡献、达人贡献、帖子和时间线不混入缺平台或错误期间数据。
- 付费传播只有明确归属时才能标记 paid。
- 缺少 spend、revenue、conversion 或归因窗口时不生成虚假 ROI/ROAS。
- 归因数据缺失时发布 restricted 活动报告，而不是只返回说明。

### 6.3 达人圈选

范围必须覆盖品牌/品类、平台、受众、预算、内容形式和候选数量。范围明确后必须尝试发布
`kol_selection_v3`。

- 不伪造评分、报价或稳定身份。
- 只有原始结果提供足以满足契约的评分信息时，才纳入正式候选。
- 缺少评分依据时不以昵称代替 UID，也不手工补分。
- 数据不足时仍发布 restricted 报告；若原始结果存在有效候选而 Skill 无法稳定构造合法
  `score_snapshot`，评测判失败。
- 若未达到 90%，不晋级，转入独立“确定性评分接入”设计。

该保留门禁源于当前契约事实：`kol_selection_v3` 要求完整评分快照，Direct Artifact Tool
只校验模型输入，生产 Pi 内部工具面没有暴露已有的确定性 `rank_kols`。本阶段不允许模型用
幻觉评分绕过这一差异。

## 7. 品牌评测矩阵

首轮建立 10 类场景，每类运行三次，共 30 个有效轮次：

| 场景 | 核心断言 |
| --- | --- |
| 单平台完整数据 | 一次形成完整报告 |
| 多平台完整数据 | 平台数据不串用、不重复汇总 |
| 环比/同比比较 | 当前期、基线期与口径正确对应 |
| 部分章节缺失 | 发布 restricted，状态与限制一致 |
| 工具返回空结果 | 不把空值当零、不循环撞击 |
| 某项工具失败 | 使用现有结果发布受限报告 |
| 多来源口径不同 | 不跨单位、时间窗或指标强行合并 |
| 高数据量结果 | 稳定压缩分析并生成合法 payload |
| 品牌实体有歧义 | 先澄清，澄清轮 0 DataTap、0 Artifact |
| 极少数据或关键查询全部失败 | 仍发布合法 restricted 报告 |

活动与达人评测沿用相同类别结构，但断言替换为各自的期间/归因与候选/评分语义。

## 8. 原始数据与评测流

### 8.1 语料组成

每个场景包含：

- 用户原始请求与会话上下文；
- 当时真实 MCP 工具目录；
- 工具名、原始参数和完整标准 MCP Tool Result；
- 预期 Artifact 类型与业务断言；
- 每份原始数据的 SHA-256。

完整原始结果保存在 Git 忽略的受控目录，例如
`backend/.data/marketing-skill-evals/<corpus_version>/`。仓库只提交场景定义、文件清单、内容
哈希和评分规则。评测交给模型的数据与原始文件逐字节一致，不脱敏、不摘要、不改写。

首次语料采集和 `CORPUS_MISS` 补充必须通过单独授权的真实 DataTap 只读调用完成。新增结果
后，基线与候选都必须从头重跑受影响场景。

### 8.2 执行顺序

1. 固定模型版本与可影响生成的配置。
2. 校验语料哈希与工具目录版本。
3. 对同一场景交错运行基线和候选，避免时段波动单边影响。
4. 模型自主调用工具；评测代理按工具名和规范化参数返回原始结果。
5. 使用生产 Artifact 工具在隔离测试库创建并发布产物。
6. 评分器读取最终 Artifact、调用轨迹和错误次数，不读取模型隐藏推理作为判据。

### 8.3 失败分类

- 模型输出错误、Draft 校验失败或 Publication 失败：计入 Skill 失败。
- 第二次 Draft 仍失败：本场景失败。
- 正式执行只输出文字：失败。
- 数据缺失但发布合规 restricted 报告：通过。
- 模型请求语料中不存在的工具/参数：`CORPUS_MISS`，该轮无效；补充真实结果后同时重跑
  基线与候选。
- 模型供应商临时错误：基础设施无效轮次，最多有界重跑一次，不计入 Skill 分数。
- 原始数据哈希不一致：立即停止整个评测。

## 9. 评分与晋级门槛

### 9.1 硬门槛

- 有效业务轮次通过率至少 90%。
- 安全断言 100%：不编造数据、不越过 allowlist、不提交服务器字段、不把缺失当零。
- 不允许任何关键场景连续三次失败。
- Draft 最多调用两次，即最多一次字段级纠错。
- 正式执行均有 Publication/Version。
- 首次 Draft 合法率必须高于 1.1.0 基线。

### 9.2 效率防回退

记录模型请求、MCP 调用、input/output token、执行时长和 Draft 次数。稳定性优先，但候选任一
指标相对基线恶化不得超过 20%。当多个候选稳定性相同，优先选择模型请求和 MCP 调用更少者。

### 9.3 真实端到端确认

冻结语料评测通过的唯一胜出候选，再执行一次真实模型 + 真实 DataTap + 测试钱包的专项报告
链路，确认 MCP 协议、Artifact Publication、BI 和 Excel 同版。该验收不替代 30 轮稳定性
评测，也不构成完整 B7 或生产切流授权。

## 10. 测试策略

### 10.1 静态与契约测试

- Manifest、Root Policy、Skill 和 contract digest 一致。
- Skill 不复制 JSON Schema，不恢复 Evidence Bridge、固定工具顺序或 required artifact
  Runtime 门禁。
- `model_input_contract` 继续直接来自 DTO 单一事实源。
- Skill 与 Root Policy 对“澄清后正式执行必须发布报告”的表述一致。
- complete/restricted、空结果、工具失败、字段级定向纠错均有回归用例。
- BI 与 Excel 仍读取同一不可变 Version。

### 10.2 真实模型评测

真实模型评测使用独立脚本和显式预算，不进入默认 pytest。每轮生成 append-only 结果记录，
包括模型/Skill/Pack/dataset 身份、输入哈希、调用统计、Artifact 身份、评分和停止原因。

### 10.3 仓库回归

每次候选晋级前运行相关后端测试、Capability Pack 测试、Artifact 测试、Pi Gateway/Runtime
测试、类型检查和构建。真实模型评测与普通 pytest 不并行使用共享测试库。

## 11. 版本与回滚

版本按单 Skill 晋级：

1. 品牌通过：Pack `1.2.0`，品牌 Skill 及必要的 Root/Social Policy 同步升版。
2. 活动通过：Pack `1.3.0`，活动 Skill 升版。
3. 达人通过：Pack `1.4.0`，达人 Skill 升版；若评分阻塞则停在 `1.3.0`。

本阶段 Artifact contract、DTO、builder 和 exporter 版本不变。每次晋级更新相应 Skill digest、
Root Policy digest 与 manifest digest。旧 Run Snapshot 不改写。

上线后若发现回归，不改写失败版本或历史 Run；以新的 patch 版本恢复上一版 Skill 内容，保留
失败版本和评测记录用于审计。

## 12. 实施顺序与停止点

1. 建立通用评测语料格式、runner、scorer 与 1.1.0 基线。
2. 采集/整理品牌 10 类原始 MCP 场景。
3. 优化品牌 Skill，完成 A/B 评测和一次真实端到端确认；通过后晋级 1.2.0。
4. 复用基础设施优化活动 Skill；通过后晋级 1.3.0。
5. 建立达人评测并优化 Skill。
6. 达人达到 90% 才晋级 1.4.0；否则以 `KOL_SCORING_CONTRACT_BLOCKED` 停止并另写评分接入
   设计。

每个 Skill 晋级后停止并接受独立审核，不自动进入下一个 Skill。
