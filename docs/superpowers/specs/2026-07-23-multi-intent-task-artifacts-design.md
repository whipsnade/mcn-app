# 多意图任务与类型化产物设计

日期：2026-07-23
状态：设计已确认

## 1. 背景

KOL Insight AI 的会话是开放式、多轮的。同一会话内，用户每一轮的目标可能不同：

- 分析品牌阶段表现；
- 复盘某个品牌活动；
- 根据分析结论圈选达人；
- 在同一轮中先分析，再圈选达人。

当前系统把所有 ready 后的消息统一送入 KOL 圈选 Agent：

1. 所有任务都注入 `kol_excel_v2` 导出契约；
2. 所有 settled MCP 证据都尝试沉淀到会话级达人名单；
3. 所有任务收尾都尝试生成 KOL 分析；
4. 右侧 BI 主要围绕 KOL 分析和圈选达人设计。

这使品牌分析、活动效果分析与达人圈选混在同一条路径中。即使用户只要求分析品牌或活动，系统也可能形成达人名单并生成 KOL 报告。

本设计将“开放会话”与“本轮业务目标”解耦：会话保存上下文和历史产物，每条消息独立规划一个或多个业务目标；每种目标拥有独立的工具策略、证据处理和产物类型。

## 2. 设计目标

1. 同一会话允许连续执行不同类型的任务。
2. 一条消息允许包含多个有依赖关系的目标。
3. 品牌分析、活动分析和达人圈选使用明确、互不污染的产物契约。
4. 活动分析始终属于某个品牌，MCP 查询必须携带解析后的品牌参数。
5. 只有用户明确要求圈选、推荐或候选名单时，才允许写入圈选达人名单。
6. 品牌、活动、达人三类产物都在对应 Goal 收尾时自动生成。
7. 右侧 BI 固定为品牌、活动、达人三个模块，不因任务完成自动切换。
8. 每类报告和圈选名单都保留独立历史版本。
9. 复用现有 Agent Loop、MCP 校验、积分计费、租约恢复和 SSE 基础设施。
10. 支持分阶段上线和旧数据兼容，避免一次性重写当前 KOL 链路。

## 3. 非目标

本阶段不实现：

- 品牌 Agent、活动 Agent、达人 Agent 三套独立多 Agent 运行时；
- 任意 DAG、并发 Goal 或 Goal 间循环反馈；
- 跨会话的企业级品牌资产库和报告汇总；
- 真实充值后的自动续跑交互；
- 新的 ReportBlock 图表类型；
- 对旧历史表 `bi_reports`、`task_candidates`、`kols` 的全面清理。

## 4. 核心决策

### 4.1 会话不固定类型

会话只保存：

- 最近对话；
- 当前活跃品牌等上下文；
- 历史 Goal 与 Artifact；
- Brainstorm 已确认的信息。

`intent` 不属于会话。每条用户消息都重新规划本轮目标。

### 4.2 一轮请求是一个 AnalysisTask

`AnalysisTask` 继续表示“一条用户消息触发的一轮执行”，负责：

- 用户和会话归属；
- 总体租约；
- 总体 SSE；
- 总体终态；
- 多个 TaskGoal 的容器。

### 4.3 一个 AnalysisTask 包含一个或多个 TaskGoal

第一阶段 Goal 类型固定为：

- `brand_analysis`
- `campaign_analysis`
- `kol_selection`

一条消息最多生成三个 Goal，同类型 Goal 在同一任务内不得重复。

### 4.4 Goal 产生类型化 Artifact

任务与右侧 BI 不再通过报告正文或任务状态猜测业务类型。所有可展示产物都登记为 `TaskArtifact`。

第一阶段 Artifact 类型：

- `brand_report`
- `campaign_report`
- `kol_report`
- `kol_selection_set`

## 5. 总体架构

```text
用户本轮消息
  │
  ▼
GoalPlanner
  ├─ clarify：缺少关键参数，返回澄清问题，不创建任务
  └─ execute：创建 AnalysisTask + TaskGoal[]
                         │
                         ▼
                  GoalOrchestrator
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
 brand_analysis   campaign_analysis   kol_selection
        │                │                │
        └────────共享 Tool Loop───────────┘
                         │
                         ▼
                    TaskArtifact[]
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
      品牌分析          活动分析           达人
                                      ├─ KOL 分析
                                      └─ 圈选达人
```

共享 Tool Loop 继续提供：

- 模型工具决策；
- MCP 工具白名单与 Schema 校验；
- 每次 MCP 调用固定 10 积分的预留、结算和释放；
- 空结果熔断；
- 任务租约、恢复和幂等；
- SSE 任务进度。

GoalPolicy 只负责目标特有的 Prompt、工具范围、上下文、证据处理和收尾产物。

## 6. Goal 规划

### 6.1 GoalPlanner 输入

每条用户消息都进入 `GoalPlannerService`，输入包括：

- 当前用户消息；
- 最近会话消息；
- 当前会话活跃品牌；
- Brainstorm 画像；
- 当前账号默认品牌；
- 最近的品牌、活动和达人 Artifact 摘要；
- 当前允许的 Goal 类型。

GoalPlanner 是结构化模型调用，使用现有 prompt 日志和 exemplar 机制。该调用不产生 MCP 积分费用，但会增加一次模型调用延迟。

### 6.2 GoalPlanner 输出

输出只有两种动作：

```text
action = clarify
- question
- options
- extracted_context
```

```text
action = execute
- goals[]
  - goal_type
  - sequence
  - depends_on_sequence
  - params
  - request_evidence
```

`request_evidence` 是当前消息中支持该 Goal 的原文片段。对于 `kol_selection`，服务端要求该片段可以在当前消息的规范化文本中定位，防止模型仅因查到达人而自行扩展出圈选目标。

### 6.3 品牌解析优先级

GoalPlanner 按以下顺序解析品牌：

1. 当前消息明确提到的品牌；
2. 当前会话最近确认的活跃品牌；
3. 当前账号配置的默认品牌；
4. 都没有时返回 `clarify`。

显式指定品牌或竞品可以覆盖会话和账号默认品牌。

### 6.4 品牌与活动的关系

`campaign_analysis` 不是脱离品牌的独立分析。活动 Goal 必须包含：

- `brand`
- `campaign`
- 可选的 `period`
- 可选的 `platforms`
- 可选的分析侧重点

例如“分析喜茶 618 表现”直接解析为：

```json
{
  "goal_type": "campaign_analysis",
  "params": {
    "brand": "喜茶",
    "campaign": "618"
  }
}
```

无需追问“品牌分析还是活动分析”。MCP 查询必须携带品牌参数。

### 6.5 服务端规划校验

创建任务前执行确定性校验：

- Goal 数量为 1–3；
- 同一任务中 `goal_type` 不重复；
- `sequence` 连续且唯一；
- 依赖只能指向更早的 Goal；
- 不允许循环依赖；
- `campaign_analysis` 必须有非空 `brand` 和 `campaign`；
- `kol_selection` 必须有可验证的 `request_evidence`；
- Goal 参数通过各自 Pydantic Schema。

校验失败时，把错误反馈给模型重试一次；第二次仍失败返回 `goal_planning_failed`，不创建任务。

## 7. Goal 执行契约

### 7.1 GoalPolicy 接口

每种目标实现统一的 GoalPolicy 边界：

```text
goal_type
validate_params()
allowed_tools()
build_loop_context()
consume_evidence()
build_result_summary()
finalize_artifacts()
```

基础设施只依赖该接口，不在 `TaskExecutor` 中堆叠品牌、活动、达人条件分支。

### 7.2 BrandAnalysisGoalPolicy

目标：

- 品牌声量；
- 曝光和互动趋势；
- 用户情感；
- 热门内容主题；
- 平台分布；
- 竞品对比。

规则：

- 不注入 KOL Excel 契约；
- 不启用圈选沉淀器；
- 可以查询达人贡献数据，但达人仅作为品牌分析证据；
- 收尾自动生成 `brand_report`。

### 7.3 CampaignAnalysisGoalPolicy

目标：

- 指定品牌活动的曝光、互动和内容表现；
- 平台贡献；
- 达人贡献；
- 活动节奏；
- 正负反馈；
- 复盘和优化建议。

规则：

- 品牌参数必填；
- 不注入 KOL Excel 契约；
- 不启用圈选沉淀器；
- 达人贡献榜属于活动报告证据，不等于圈选名单；
- 收尾自动生成 `campaign_report`。

### 7.4 KolSelectionGoalPolicy

目标：

- 搜索候选达人；
- 补充达人画像；
- 按六维度评分；
- 形成版本化圈选名单；
- 生成 KOL 分析。

规则：

- 只有该 Policy 注入 `kol_excel_v2`；
- 只有该 Policy 启用 SelectionIngest；
- 每次执行创建独立 `selection_set`；
- MCP settled 证据只写入当前 selection set；
- 收尾生成 `kol_selection_set` 和 `kol_report` 两类 Artifact。

## 8. 复合任务

### 8.1 示例

用户请求：

> 分析喜茶 618 活动表现，并根据效果圈选下一轮达人。

规划结果：

```text
Goal 1: campaign_analysis
Goal 2: kol_selection
depends_on: Goal 1
```

Goal 1 完成后，将精简的 `result_summary_json` 注入 Goal 2，内容可以包括：

- 表现较好的平台；
- 高互动内容类型；
- 核心受众；
- 表现较好的达人特征；
- 活动风险和品牌安全要求。

### 8.2 执行顺序

第一阶段所有 Goal 顺序执行。即使两个 Goal 没有依赖关系，也不并发执行，以降低：

- 钱包并发预留复杂度；
- 任务租约恢复复杂度；
- 模型上下文不一致；
- SSE 聚合难度。

### 8.3 软依赖

Goal 依赖是软依赖：

- 上游成功时，下游获得 `result_summary_json`；
- 上游失败时，下游仍使用原始用户需求和已解析参数执行；
- 下游记录 `dependency_missing` warning；
- 缺少品牌等强制参数时，才停止下游 Goal。

第一阶段只支持：

```text
brand_analysis → kol_selection
campaign_analysis → kol_selection
```

## 9. 数据模型

### 9.1 task_goals

```text
id
task_id
sequence
goal_type
status
depends_on_goal_id
params_json
trajectory_json
result_summary_json
warning_code
error_code
started_at
completed_at
created_at
updated_at
```

约束：

- 唯一：`task_id + sequence`
- `goal_type` 限定为三种已批准类型
- `status` 使用明确状态机

建议状态：

```text
pending
running
completed
completed_with_warnings
insufficient_balance
failed
skipped
```

### 9.2 task_artifacts

```text
id
session_id
task_id
goal_id
artifact_key
artifact_type
title
version
status
report_id
selection_set_id
scope_json
error_code
created_at
updated_at
```

约束：

- 唯一：`artifact_key`
- `report_id` 与 `selection_set_id` 二选一
- Artifact 必须通过所属 Session 做用户鉴权

`task_id` 和 `goal_id` 对 Goal 自动产物非空；对兼容期内的手动生成和 legacy
回填允许为空。`artifact_key` 是所有来源统一使用的幂等键：

```text
goal:{goal_id}:{artifact_type}
manual:{idempotency_key}:{artifact_type}
legacy:{domain_object_id}:{artifact_type}
```

这样自动 Goal、手动重新生成和旧数据回填都能使用同一 Artifact 注册表，又不会因
`goal_id` 为空而失去幂等约束。

`scope_json` 保存产物生成时的业务范围快照：

```json
{
  "brand": "喜茶",
  "campaign": "618",
  "period": {
    "start": "2026-06-01",
    "end": "2026-06-18"
  },
  "platforms": ["xiaohongshu", "douyin"]
}
```

### 9.3 analysis_reports

新增：

```text
report_type
scope_json
```

`report_type`：

```text
brand_analysis
campaign_analysis
kol_analysis
```

版本唯一约束从：

```text
session_id + version
```

调整为：

```text
session_id + report_type + version
```

不同报告类型独立递增版本。

### 9.4 kol_selection_sets

```text
id
session_id
task_id
goal_id
version
title
scope_json
status
created_at
updated_at
```

唯一：

```text
session_id + version
```

### 9.5 kol_selection_items

字段继承当前 `SessionKolSelection` 的身份、评分和快照字段，并把归属改为 `selection_set_id`。

唯一：

```text
selection_set_id + platform + kol_uid
```

同一 selection set 内保留现有按昵称二次归并逻辑。不同 selection set 之间不合并。

### 9.6 artifact_read_states

```text
user_id
session_id
module_key
last_seen_artifact_id
seen_at
```

唯一：

```text
user_id + session_id + module_key
```

`module_key`：

```text
brand
campaign
kol_analysis
kol_selection
```

### 9.7 用户品牌配置

新增 `user_brand_profiles`：

```text
id
user_id
brand_name
is_default
metadata_json
created_at
updated_at
```

同一用户可以关联多个品牌，但最多一个默认品牌。

“最多一个默认品牌”由事务内锁定更新保证，并使用可空的默认标记唯一约束兜底；
非默认行的标记为 NULL，避免 MySQL 对普通布尔唯一约束导致只能保存一条非默认品牌。

`WorkspaceSession.brand` 表示当前活跃品牌上下文。Goal 的 `params_json` 和 Artifact 的 `scope_json` 保存当时实际使用的品牌快照，因此以后切换会话品牌不会改变历史报告归属。

## 10. 报告与名单版本

### 10.1 报告

品牌、活动和 KOL 报告分别维护独立版本。右侧默认展示该模块最新版本，并提供历史下拉。

历史项显示：

- 标题；
- 品牌；
- 活动名；
- 查询周期；
- 版本；
- 生成时间；
- 来源任务。

### 10.2 圈选名单

当前 `session_kol_selections` 是会话级累计 upsert。不同轮次的筛选条件会把旧达人和新达人混在一起，无法表示本轮名单。

新语义为：

- 每个 `kol_selection` Goal 创建一份 selection set；
- 达人页默认展示最新 selection set；
- 历史下拉可以切换旧名单；
- Excel 导出当前选中的 selection set；
- KOL 分析基于明确的 selection set 生成；
- 不同任务的名单不再无边界累加。

## 11. 自动产物生成

每个 Goal 完成证据采集后自动收尾：

1. 生成领域摘要；
2. 调用对应报告构建器或完成名单；
3. 写入领域对象；
4. 登记 TaskArtifact；
5. 发送 `artifact.updated`；
6. 标记 Goal 终态。

三类自动产物：

- `brand_analysis` → 品牌报告；
- `campaign_analysis` → 活动报告；
- `kol_selection` → 圈选名单 + KOL 分析。

品牌和活动报告继续使用现有 `ReportDocument` 与 ReportBlock 渲染协议，不新增图表组件体系。

## 12. 右侧 BI

### 12.1 一级 Tab

右侧固定为：

```text
品牌分析 | 活动分析 | 达人
```

任务完成后不自动切换当前 Tab。

### 12.2 品牌分析

默认展示最新品牌报告，顶部显示：

- 品牌；
- 查询周期；
- 生成时间；
- 来源任务；
- 历史版本。

### 12.3 活动分析

默认展示最新活动报告，标题区域必须同时显示品牌和活动：

```text
喜茶｜618 夏季推广｜2026-06-01 至 2026-06-18
```

同一会话允许保存多个品牌活动的历史报告。

### 12.4 达人

保留现有两个子 Tab：

```text
KOL 分析 | 圈选达人
```

两个子 Tab 分别维护：

- `kol_analysis` 报告版本；
- `selection_set` 名单版本。

Excel 导出当前选中的名单版本。

### 12.5 更新提示

收到 `artifact.updated` 后：

- 只刷新对应模块摘要；
- 一级或二级 Tab 显示更新圆点；
- 不切换用户当前视图；
- 用户点击对应 Tab 后写入已读状态；
- 刷新页面和换设备后仍能恢复未读状态。

## 13. API 与事件

### 13.1 新 API

```text
GET  /sessions/{id}/artifacts/summary
GET  /sessions/{id}/artifacts?type=&cursor=
GET  /artifacts/{artifact_id}
PUT  /sessions/{id}/artifact-read-state
GET  /selection-sets/{id}
GET  /selection-sets/{id}/export
```

### 13.2 兼容 API

兼容期内：

- `GET /sessions/{id}/kol-selection` 返回最新 selection set；
- 旧导出端点默认导出最新 selection set；
- `POST /sessions/{id}/kol-analysis` 基于最新 selection set 手动生成新版本；
- 旧报告读取接口继续工作。

### 13.3 SSE

新增统一事件：

```json
{
  "type": "artifact.updated",
  "payload": {
    "artifact_id": "artifact-id",
    "goal_id": "goal-id",
    "artifact_type": "campaign_report",
    "module_key": "campaign",
    "version": 2,
    "title": "喜茶 618 活动效果分析"
  }
}
```

兼容期内 `report.updated` 与 `artifact.updated` 双发，前端按 Artifact 或报告 ID 去重。

Goal 进度事件建议增加：

```text
goal.started
goal.completed
goal.failed
```

现有工具事件 payload 增加 `goal_id`。

## 14. 任务与 Goal 状态

### 14.1 Goal 终态

- `completed`
- `completed_with_warnings`
- `insufficient_balance`
- `failed`
- `skipped`

### 14.2 AnalysisTask 终态

- 所有 Goal 成功：`completed`
- 至少一个成功、至少一个 warning/failed：`completed_with_warnings`
- 全部失败：`failed`
- 因余额不足停止剩余 Goal：`insufficient_balance`

已完成 Goal 和 Artifact 在其他 Goal 失败时不回滚。

## 15. 错误处理与恢复

### 15.1 GoalPlanner

- Schema 失败自动重试一次；
- 第二次失败返回 `goal_planning_failed`；
- 缺少业务参数返回澄清问题；
- 规划失败不创建任务、不产生 MCP 积分费用。

### 15.2 Goal 轨迹

每个 `task_goal` 保存自己的 `trajectory_json`。

恢复时：

1. 已完成 Goal 不重跑；
2. 从第一个未完成 Goal 继续；
3. 已 settled MCP 调用不重复扣费；
4. 已存在 Artifact 不重复生成。

### 15.3 报告生成失败

MCP 证据已成功采集但报告撰写失败时：

- 不退回已经正确结算的 MCP 积分；
- Goal 标记 `completed_with_warnings`；
- Artifact 标记 `failed`；
- 保留证据；
- 用户可以只重试报告生成；
- 重试不重新调用 MCP，不重复扣费。

### 15.4 余额不足

- 停止尚未发出的 MCP 调用；
- 保留已完成 Goal 与 Artifact；
- 当前 Goal 标记 `insufficient_balance`；
- 后续 Goal 保持 `pending`；
- 未来开放充值后可从当前 Goal 继续。

## 16. 幂等与并发

关键唯一约束：

```text
task_goals:          task_id + sequence
task_artifacts:      artifact_key
analysis_reports:    session_id + report_type + version
kol_selection_sets:  session_id + version
kol_selection_items: selection_set_id + platform + kol_uid
artifact_read_states:user_id + session_id + module_key
```

报告和 selection set 版本创建使用：

- 锁定读计算下一版本；
- 唯一约束兜底；
- SAVEPOINT 冲突后重算并重试一次。

## 17. 权限与安全

所有 TaskGoal、Artifact、报告、selection set 和已读状态查询都必须通过 `WorkspaceSession.user_id` 校验归属。

禁止仅凭以下 ID 直接返回数据：

- `goal_id`
- `artifact_id`
- `report_id`
- `selection_set_id`

`scope_json`、Prompt 日志和 Artifact payload 继续使用现有敏感字段过滤策略，不暴露端点、Token 或上游凭据。

## 18. 测试策略

### 18.1 后端单元测试

- GoalPlanner 输出 Schema；
- 品牌四级解析优先级；
- 活动 Goal 品牌必填；
- KOL Goal 原文证据校验；
- Goal 去重和依赖校验；
- 三种 GoalPolicy 的工具范围和上下文；
- Artifact 到右侧模块的映射；
- 已读状态计算；
- 报告和名单版本计算。

### 18.2 后端集成测试

- 单独品牌分析；
- 单独活动分析；
- 单独达人圈选；
- 品牌分析后圈选；
- 活动分析后圈选；
- 非圈选 Goal 不写名单；
- 活动 MCP 参数始终带品牌；
- Goal 部分失败；
- 上游失败后的软依赖；
- 报告生成失败后无 MCP 重扣；
- 余额不足；
- 租约恢复；
- Artifact 幂等；
- 版本并发冲突；
- 跨用户数据隔离；
- 旧数据迁移和兼容端点。

### 18.3 前端测试

- 固定三个一级 Tab；
- 达人两个子 Tab 保持不变；
- `artifact.updated` 只显示更新提示；
- 任务完成不自动切换；
- 点击 Tab 标记已读；
- 最新版本和历史版本切换；
- 导出当前 selection set；
- 品牌、活动任务不误刷新达人名单；
- 旧 `report.updated` 双发期间去重。

### 18.4 E2E

至少覆盖：

1. 品牌分析只生成品牌报告；
2. 活动分析自动解析并携带品牌；
3. 活动分析后圈选达人，一轮产生多份 Artifact；
4. 同一会话连续切换品牌、活动和圈选目标，历史版本互不覆盖；
5. 任务完成后右侧不跳转，只显示更新提示；
6. 刷新页面后更新提示和历史版本仍正确。

## 19. 分阶段实施

### 阶段一：GoalPlanner 影子模式

- 实现 GoalPlanner；
- 不改变现有执行路径；
- 记录 Goal、品牌来源、参数和置信信息；
- 用真实消息评估误分类和误圈选率。

退出条件：

- 活动品牌解析稳定；
- 非圈选请求不会生成 `kol_selection`；
- 复合目标拆分符合产品预期；
- Schema 失败和澄清率可接受。

### 阶段二：Goal 与 Artifact 基础设施

- 新增 Goal、Artifact、ReadState 等表；
- 新增 `user_brand_profiles` 和默认品牌配置；
- 新增 selection set/items；
- 把现有 `session_kol_selections` 迁入“历史默认名单”；
- MCP 调用和事件增加 goal_id；
- 把现有 KOL 流程包装成 `kol_selection` Goal；
- 现有 KOL 报告和名单映射为 Artifact；
- 用户行为暂时保持不变。

### 阶段三：品牌和活动分析

- 实现品牌和活动 GoalPolicy；
- 实现两类报告构建器；
- 上线固定三个一级 Tab；
- 上线历史版本和更新提示；
- 将 KOL 契约、圈选沉淀和自动 KOL 分析下沉到 KolSelectionGoalPolicy。

### 阶段四：复合任务

- 启用多 Goal 顺序编排；
- 实现摘要传递；
- 实现软依赖；
- 实现 Goal 级恢复和部分成功。

### 阶段五：兼容收敛

- 新端点全面使用明确的 Artifact 和 selection set；
- Excel 导出必须指定或解析出当前 selection set；
- 观察期稳定后停止双发 `report.updated`；
- 删除前端旧报告状态分支；
- 停止向 `session_kol_selections` 写入；
- 旧表进入只读兼容期，确认无需回滚后再单独设计清理迁移。

## 20. 兼容策略

- `analysis_tasks.kind` 暂时继续固定为 `"agent"`；
- 旧任务没有 TaskGoal 时按 legacy 路径恢复；
- 新任务使用 Goal 级 trajectory；
- 旧 `report.updated` 与新事件双发一个兼容周期；
- 旧 KOL 列表和导出端点返回最新 selection set；
- 旧会话报告与名单在迁移时创建对应 legacy Artifact；
- 前端完成 Artifact 化后再移除旧报告状态分支。

## 21. 观测指标

- 各 Goal 类型数量和成功率；
- 单 Goal 平均 MCP 次数、积分和耗时；
- GoalPlanner Schema 失败率；
- GoalPlanner 澄清率；
- 活动品牌来源分布；
- 活动品牌缺失率；
- 非圈选 Goal 创建 selection set 的数量，目标必须为零；
- Artifact 生成失败率；
- Artifact 重试成功率；
- 复合任务部分成功率；
- Goal 恢复次数和重复结算拦截次数；
- 右侧 Artifact 未读和查看转化。

## 22. 风险与缓解

### 22.1 GoalPlanner 增加延迟

缓解：

- 使用精简上下文；
- 限制输出 Token；
- 复用 prompt exemplar；
- 先以影子模式观察耗时；
- 不在 Planner 中执行 MCP。

### 22.2 模型误生成圈选目标

缓解：

- `kol_selection` 必须携带当前消息原文证据；
- 服务端验证原文片段；
- 影子模式统计误圈选；
- 非圈选 Goal 永不启用 SelectionIngest。

### 22.3 旧名单迁移丢失来源

现有会话级累计名单无法还原每行来自哪一轮筛选。迁移时统一生成一个标题为“历史默认名单”的 selection set，并保留行内已有 `source_task_id` 和快照；不伪造不存在的版本历史。

### 22.4 双事件导致重复刷新

兼容期前端按 Artifact ID 或 Report ID 去重；旧事件只用于旧客户端，新客户端优先使用 `artifact.updated`。

## 23. 验收标准

设计实施完成后必须满足：

1. “分析品牌近 30 天表现”只更新品牌模块，不产生圈选名单。
2. “分析喜茶 618 表现”直接生成带品牌作用域的活动报告。
3. “分析活动中哪些达人表现最好”可以展示达人贡献，但不产生圈选名单。
4. “分析活动并圈选下一轮达人”在同一任务中生成活动报告、圈选名单和 KOL 分析。
5. 任务完成后右侧保持当前视图，仅对应 Tab 显示更新提示。
6. 品牌、活动、KOL 报告和名单都能查看独立历史版本。
7. Excel 导出内容与用户当前选中的 selection set 完全一致。
8. 任一 Goal 失败不删除其他已完成产物。
9. 崩溃恢复不会重复调用已 settled MCP 或重复扣费。
10. 所有 Artifact 和版本查询都保持当前用户数据隔离。
