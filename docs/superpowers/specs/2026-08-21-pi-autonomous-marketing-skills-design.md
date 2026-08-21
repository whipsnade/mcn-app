# Pi 自主营销 Skill、通用报告与生产热更新架构设计

状态：已确认，待实施计划
日期：2026-08-21

## 1. 背景

KOL Insight AI 面向品牌、活动、达人及混合营销问题。真实用户不会只提交预定义场景：
同一条请求可能同时要求品牌表现、爆文、达人主页、跨平台统一表头、BI 与自定义 Excel。

当前架构已经由 Pi 和模型主导业务分析，但标准 Artifact、固定 Excel 模板以及评测期门禁仍可能
成为产品能力上限。例如 `brand_report_v3` 与 `kol_selection_v3` 的 Top20 限制、固定 Sheet、
Corpus/候选/observation 门禁，均不应决定正式 Pi Run 能否响应长尾业务需求。

本设计不取消可信执行内核，而是重新划分职责：

> 开放业务决策，封闭高风险副作用。

Pi 决定做什么、如何研究和如何组织输出；可信内核只控制权限、外发、计费、数据真实性、
持久化与文件安全。

## 2. 目标

1. Pi 自主理解品牌、活动、达人及混合营销需求，不绑定固定业务流程。
2. 任务存在决定性歧义时，Pi 可以在付费调用前请求用户澄清。
3. 标准 BI 继续保留，同时提供可表达长尾需求的通用报告和 Excel 出口。
4. 生产 Skill 可随时热更新；更新立即影响新 Run，运行中 Run 保持启动快照。
5. Pi 原生加载受控 `SKILL.md`，不扫描用户级或未审核的本机 Skill。
6. Skill 内容变更不再触发反复全量回归、离线 Corpus Replay 或多轮稳定性测试。
7. MCP 标准 Tool Result 直接交给 Pi；不恢复 MCP Evidence Bridge。
8. 每个完成的分析 Run 至少发布一个主报告；澄清 Run 不要求发布报告。

## 3. 非目标

- 不允许 Skill 绕过租户隔离、工具审核、计费、License 或密钥边界。
- 不允许模型直接生成任意二进制 xlsx、公式、宏或操作服务器文件系统。
- 不删除现有标准 Artifact、BI 与 Exporter；它们继续作为默认模板和兼容出口。
- 不把开发评测中的 candidate、Corpus、Stage 2A/2B、observation 数量带入正式运行时。
- 不要求每次 Skill 文案更新都重新执行完整品牌 Gate 或真实 Web UAT。

## 4. 架构原则与职责边界

### 4.1 Pi 的业务自主权

正式 `session_analyst_v1` Run 中，Pi 自主决定：

- 是否需要澄清；
- 加载哪些品牌、活动、达人或输出 Skill；
- 调用哪些已审核 MCP 工具；
- 工具参数、顺序、分页、停止条件和替代策略；
- 如何组合品牌、活动与达人结果；
- 如何处理空结果、部分失败与数据不足；
- 选择标准 Artifact、通用报告、通用 Excel，或同时发布多个 Artifact；
- Sheet、表头、列顺序、平台标识、行数和排序。

Skill 提供领域知识、指标解释、输出契约和建议，不定义固定调用流水线。

### 4.2 可信内核的强制边界

可信内核继续强制：

- tenant/user/session/run 归属隔离；
- MCP 工具审核、实时签名校验与渠道权限；
- 外发前 durable ToolCall、积分预留与幂等；
- success/failed_confirmed/definitely_not_sent/result_unknown 的结算语义；
- unknown 不自动重放；
- License、余额和紧急资源上限；
- 数据缺失不能伪装成 0，报告必须披露 partial/unavailable；
- Artifact Version 不可变；
- URL、文件名、Excel 公式注入、宏与文件大小安全；
- 凭证、Bearer、API Key、DSN、主密钥不进入模型、报告或日志。

业务数据不做无差别脱敏：在当前租户权限范围内，MCP 返回的品牌、活动、达人业务数据可直接
交给 Pi。凭证和系统秘密仍必须严格剥离。

### 4.3 不得成为生产门禁的内容

以下内容只属于评测或默认模板，不得阻断正式 Pi Run：

- 固定业务工具顺序和调用次数；
- 固定一个请求只能生成一种 Artifact；
- Top20 等业务行数限制；
- 固定 Sheet 数量与固定列；
- Corpus hit/miss；
- candidate 编号；
- Stage 2A/2B 与固定 observation 数；
- 用户请求必须匹配预定义场景；
- 大型离线 replay 通过后才能发布 Skill 文案。

## 5. 原生 Pi Skill 与受控加载

### 5.1 加载方式

生产 Gateway 不直接设置 `noSkills: false`。该配置会允许 Pi SDK 自动发现用户级
`agentDir/skills` 和项目级 `.pi/skills`，可能把未审核 Skill 带入生产 Run。

生产采用：

```text
noSkills: true
additionalSkillPaths: [run_skill_snapshot_dir]
```

Pi SDK 在关闭默认发现时仍加载显式 `additionalSkillPaths`。因此每个 Run 只看到当前快照中
明确允许的原生 Skill。

### 5.2 Skill 分类

- Root Policy：可信内核政策，不由普通 Skill 编辑器修改。
- Domain Skills：品牌、活动、达人、营销策略等领域知识，可热更新。
- Output Skills：标准 BI、通用报告、Excel Workbook 等输出指导，可热更新。
- Tool Contracts：MCP/Internal Tool 的真实 Schema，由代码与审核目录管理，不能由 Skill
  自行扩展。

### 5.3 与 `load_marketing_skill` 的迁移

迁移期保留 `load_marketing_skill` 兼容旧 Run 和旧 Pack。新 Runtime Snapshot 同时物化为原生
Skill 目录；验证原生调用稳定后，新的 Skill 不再依赖该内部工具。旧不可变快照继续可回放，
不回写历史。

## 6. 生产 Skill 热更新生命周期

### 6.1 Revision

每次发布创建不可变 Skill Revision，至少记录：

- `skill_name`；
- `revision`；
- `content`；
- `content_digest`；
- `description`；
- `required_tools`；
- `created_by`；
- `created_at`；
- `change_note`。

Revision 作为小型文本对象持久化在新增的 `skill_revisions` 表；同一 Skill 的 Revision 不允许
原地更新或删除。新增的 `skill_activations` 表保存环境、租户范围、灰度比例与当前 Revision
指针，激活操作使用幂等键并写入管理审计日志。Skill 内容不依赖 Git 工作树或部署主机上的
可变源文件。

### 6.2 轻量发布校验

发布前只强制执行：

- frontmatter/Markdown 可解析；
- Skill 名称和描述合法且唯一；
- 引用的 Tool 存在于已审核目录；
- 不包含凭证、DSN、绝对临时路径或未批准扩展；
- 内容不能声称绕过权限、计费或未知调用规则；
- digest 可稳定重算。

校验通过即可激活，不要求真实模型、DataTap、Corpus Replay 或完整回归。

### 6.3 激活与快照

激活指针支持：

- 全局默认；
- 指定租户；
- 百分比灰度；
- 一键回滚上一 Revision。

百分比灰度以稳定 tenant identity 计算分桶，同一租户在灰度期间不会因不同 Run 随机切换版本。
一次激活事务只改变指针，不修改 Revision。

新 Run 创建时，后端解析所有激活 Revision，生成不可变 Skill Manifest 与 digest，并物化到
该 Run 的只读目录。Run Snapshot 持久化 Skill 名称、Revision 与 digest。运行中的 Run 永远
使用启动快照；激活变化只影响后续新 Run。

Skill 热更新不需要重启 FastAPI、Gateway，也不需要重新部署应用代码。

物化过程使用新建临时目录、完成 digest 复核后原子改名，并拒绝 symlink、路径穿越和未知文件。
若新 Revision 无法物化或校验，Run 在任何模型/MCP 调用前稳定失败，当前 active pointer 和已有
Run 均不受影响；系统不会自动回退到未记录的本机 Skill。

### 6.4 管理入口

第一阶段管理端提供：

- Skill 列表与当前激活 Revision；
- Markdown 编辑器；
- Revision Diff；
- 校验、发布、灰度、全量激活与回滚；
- 修改人、时间和说明审计。

只允许管理员修改生产 Skill；暂不引入复杂审批流。

## 7. 澄清策略

Pi 可以请求澄清，但澄清是模型业务判断，不是机械门禁。

适合澄清的情况：

- 品牌、活动或达人身份存在多个可能对象；
- 日期窗口无法确定；
- 指标口径会显著改变结果；
- 数量是合计还是分平台不明确；
- 用户要求互相冲突；
- 不同解释会显著改变调用成本。

通常不需要澄清：

- 可明确理解的笔误；
- 平台常用别名；
- 不影响业务结论的展示细节；
- 用户授权 Pi 按专业判断；
- 查询后发现的数据不足。

缺少决定性信息时，Pi 在付费 DataTap 调用前提出最小必要问题，一次优先询问最关键歧义。
用户授权自行判断时，Pi 采用合理默认值并在报告方法论中披露。澄清 Run 不生成报告；用户回复
后，新 Run 继承会话上下文继续分析。

## 8. 标准 Artifact 与通用报告

### 8.1 输出选择

- 请求匹配稳定标准模板时，Pi 可继续使用品牌、活动、达人标准 Artifact。
- 用户指定自定义字段、数量、表头、跨领域组合或特殊 Excel 布局时，Pi 自动选择
  `analysis_report_v1`。
- 用户要求 Excel 时，由同一 Report Version 生成 `workbook_v1` 投影。
- 一个 Run 可以发布多个 Artifact，但必须声明一个主报告。

### 8.2 `analysis_report_v1`

通用报告由类型化 Block 组成：

- metric cards；
- typed table；
- time series；
- link list；
- chart；
- narrative；
- methodology/limitations。

通用 Artifact 至少包含：

- `schema_version=analysis_report_v1`；
- `title`；
- `subject_type=brand|campaign|kol|mixed`；
- 用户请求范围 `scope`；
- `data_status=complete|restricted`；
- 唯一且稳定的 `blocks[].id`；
- `fulfillment[]` 数量要求完成度；
- `methodology` 与 `limitations`。

模型只提交业务字段；服务器补齐 artifact identity、Version、归属、发布时间和其他可信字段。
Block 引用必须指向同一 Artifact Version，不能读取其他租户或历史 Run 的可变数据。

表格列由 Pi 根据用户需求定义，每列声明稳定 key、显示名与类型。允许类型为：

```text
string | integer | number | percent | date | datetime | url | boolean
```

业务 Schema 不设置 Top20、Top40 等上限。系统只设置可配置技术上限，例如最大文件体积、
Sheet 数、列数、总行数和单元格长度；超过时分页或拆 Sheet，而不是删减业务数据后伪装成功。

数量要求显式记录：

```json
{
  "requested_min": 40,
  "actual_count": 37,
  "status": "partial",
  "reason": "真实数据仅返回 37 位达人"
}
```

数据不足时保留真实结果，并以 complete/partial/unavailable 和 limitation 披露。

### 8.3 `workbook_v1`

`workbook_v1` 是 `analysis_report_v1` 的布局投影，不复制业务数据。它引用 Report Version 与
Block ID，只描述：

- Sheet 名称与顺序；
- Block 放置位置；
- 列顺序、显示名、宽度与格式；
- 冻结表头、筛选、排序和超链接；
- 分页、拆 Sheet 与说明区。

模型不能提交公式、宏、脚本或二进制 xlsx。确定性 Exporter 只从不可变 Report Version 渲染，
因此 BI 与 Excel 始终同版。

`workbook_v1` 随 Report Version 一并冻结；相同 Version、模板版本与布局投影必须生成内容等价
的工作簿。缓存键包含 Version ID、Exporter 版本和布局 digest，避免 Skill 热更新改变历史导出。

## 9. 正式 Run 数据流

1. 用户提交任意营销需求。
2. 后端创建 Run，冻结 Skill Revision、Runtime Config、Tool Catalog 与用户权限。
3. Gateway 仅从 Run Snapshot 目录加载原生 Pi Skills。
4. Pi 判断是否需要澄清；任务明确则自主规划研究。
5. 每次 MCP 外发经过可信内核的权限、License、计费和幂等边界。
6. 标准 MCP Tool Result 直接进入 Pi 上下文，不经过 Evidence Bridge。
7. Pi 选择标准 Artifact 或 `analysis_report_v1`，调用受控 Draft/Publish 工具。
8. 用户要求 Excel 时，确定性 Exporter 从同一 Version 生成 `workbook_v1`。
9. UI 展示 BI 和下载入口；assistant message 在唯一 Run terminal 前完成。

## 10. 失败与降级语义

- empty：继续分析，报告按 empty/unavailable 披露。
- 平台或工具部分失败：使用已取得数据生成 restricted 报告。
- definitely_not_sent：Pi 可自主决定换参数、换工具或有界重试。
- failed_confirmed：释放积分，Pi 决定是否采用其他路径。
- result_unknown：保持预留并禁止重复外发；Pi 可继续其他分析。
- Artifact 校验失败：返回字段级结构化错误，由 Pi 修正。
- 数量不足：输出实际数量和 limitation，不补造记录。
- 达到模型、时间或外发紧急上限：暂停 Run，不伪装成功。
- 完成的分析 Run 必须发布主报告；澄清 Run 不适用。

## 11. 测试与发布边界

### 11.1 Skill 内容修改

每次 Revision 只运行一次轻量校验：

- Markdown/frontmatter；
- Tool 引用；
- digest；
- secret/DSN；
- Pi ResourceLoader 加载。

明确禁止把以下步骤作为普通 Skill 发布门禁：

- backend 全量 pytest；
- Gateway/Runtime/前端全量；
- 离线 Corpus Replay；
- 60-observation Gate；
- 连续三轮或十轮稳定性验证；
- 真实模型/DataTap UAT。

### 11.2 代码、Schema 或 Exporter 修改

按改动范围执行 TDD、定向测试和一次相关链路集成测试，不反复运行无关全量。

### 11.3 最终合并或生产发布

只在最终候选上执行一次：

- backend 全量；
- Gateway/Runtime/前端全量；
- 一次真实 Web 主链 UAT；
- 一次独立代码审查。

出现 flake 时隔离失败用例，不连续重跑整个套件。修复后运行受影响测试；只有最终发布候选可再
执行一次完整验证。

## 12. 生产观测

Skill 质量主要通过真实运行指标持续观察：

- 澄清率；
- 每 Run 模型请求、tokens、MCP attempt/dispatch 与积分；
- 首次 Draft 成功率和 Draft 修正次数；
- 报告发布成功率；
- restricted 比例；
- Excel 生成与下载成功率；
- 用户重新提问、重新生成和显式反馈比例；
- Skill Revision 激活前后的指标差异。

指标异常时优先回滚 Skill Revision，不要求先修改代码。

## 13. 示例：牛霸霸跨平台分析

用户请求分析最近两周“牛霸霸”在小红书和抖音的表现，要求爆文明细、达人链接、统一表头和
Excel。Pi 可以先澄清爆文率口径与数量是合计还是分平台；用户确认后，Pi 自主调用品牌、帖子、
达人相关 MCP 工具。

该请求不再被标准 BrandReport Top20 或 KOL Top20 限制。Pi 创建通用 Report Version，包含：

- 综合分析；
- 平台指标；
- 跨平台爆文明细，带 `platform` 与 URL 列；
- 跨平台达人明细，带 `platform` 与 homepage URL 列；
- 数据来源、口径、数量完成度与限制。

Excel 可投影为多个 Sheet，也可按用户明确要求把多个跨平台表放入同一 Sheet。若真实数据不足
指定数量，报告保留实际记录并标记 partial，不补造。

## 14. 兼容、迁移与回滚

- 现有标准 Artifact、Version、BI、Exporter 与历史 Run 全部保留可读。
- 新 Run 可按需求选择标准或通用 Artifact，旧 Run 不重新解释。
- 原生 Skill 与 `load_marketing_skill` 在迁移期并存，按 Runtime Snapshot 决定。
- Skill 激活失败或线上指标异常时回滚 active revision；无需回滚代码或历史数据。
- 新通用 Schema/Exporter 的数据库变更使用新增迁移，绝不修改既有迁移。

## 15. 验收标准

1. Pi 能处理品牌、活动、达人及混合请求，不要求匹配固定场景。
2. Pi 可在关键歧义下澄清，明确任务则不反复追问。
3. 新 Run 只加载显式 Run Snapshot Skill；用户级和项目级默认 Skill 不进入生产。
4. Skill 激活后无需重启，新 Run 使用新 Revision，运行中 Run 保持旧 Revision。
5. 管理员可查看 Diff、发布、灰度和回滚，所有操作可审计。
6. 标准 Artifact 继续工作；长尾需求可用 `analysis_report_v1` 表达。
7. Workbook 与 BI 读取同一不可变 Version。
8. 不存在固定 Top20 等业务行数上限；技术超限通过分页或拆 Sheet处理。
9. 直接 MCP Tool Result 架构保持，不恢复 Evidence Bridge。
10. Skill 内容更新只需轻量校验，不触发反复回归和离线测试。
