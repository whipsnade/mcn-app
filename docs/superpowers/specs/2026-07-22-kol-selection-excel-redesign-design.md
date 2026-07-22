# KOL 圈选 Excel 核心化重构 — 设计文档

日期：2026-07-22
状态：已与用户确认

## 背景与目标

用户沟通后明确：系统最重要的功能是 **KOL 圈选**，核心目标是**导出圈选 KOL 的 Excel**（模板参照旧 pipeline 产物 `_餐饮_KOL匹配度分析_20260717_0438.xlsx`）。因此做三件事：

1. 移除会话 agent 循环中的 BI 数据分析 prompt 注入与自动 BI 计算。
2. 前端右侧 BI 面板改为手动「分析」按钮触发，用户不点不算。
3. BI 的 KOL 分析数据完全来源于圈选名单数据，重新设计分析项。

已确认的决策（问答记录）：

- 圈选名单来源：通过会话 agent 自动沉淀；工具返回的 KOL **全部累积**（按 平台+UID 去重），匹配度靠评分排序体现。
- 沉淀方案：**方案 A 代码级沉淀**——后端解析工具返回自动 upsert，不依赖模型写入。
- 评分：恢复旧版代码确定性评分（8 维度权重），模型只负责采集字段。
- 导出：BI 面板「导出 Excel」按钮，手动随时导出。
- BI 面板分两区块：KOL 分析（本次做）、品牌/活动分析（暂不做，面板与 BI 报表模块保留留空）。
- KOL 分析方式：代码自动总结圈选数据 → 提交模型计算 → 前端展示。
- 会话 prompt 原则：只补充上下文 + 模板 Excel 关键字段 + MCP 参数信息，其他完全交给模型。

## 现状要点（探索结论）

- BI 注入链路：`orchestration/bi_requirements.py`（8 项 MetricDef）→ `tasks/dependencies.py:405` `build_agent_context` → `AgentLoopContext.required_metrics`（`orchestration/loop.py:184`）→ prompt user JSON；finish 覆盖门禁在 `tasks/executor.py:242-268`（streak 上限 3），工具空结果熔断在 `executor.py:291-317`（2 次空熔断）。
- 报告自动生成：任务结束 executor 调 `artifacts.build_analysis_report`（`tasks/dependencies.py:175-209`）→ `REPORT_WRITER_PROMPT` → `AnalysisReportService.build`（版本化 + `report.updated` 事件）。
- 圈选/导出现状：**无任何现存实体与端点**；旧 pipeline 代码在 git `7e44355^`：`reporting/exporter.py`（模板渲染 4 sheet）、`orchestration/export_contract.py`（字段契约 `kol_excel_v1`）、`ff3b652`（normalize + score）。历史表 `kols/task_candidate_pools` 等保留但不复用（新建独立表）。
- 前端右侧面板 = `src/components/UniversalReport.tsx`（`App.tsx:215-224`）；数据流：会话 DTO `latest_analysis_report` → `getAnalysisReport`，SSE `report.updated` → 重新拉取。
- Excel 模板 4 sheet：KOL匹配度筛选（19 列评分表）/ 达人详细画像 / 粉丝画像详情 / 评分方法论与数据来源。
- 模板评分维度：餐饮兴趣 20 / 目标地区粉丝 15 / 目标年龄段 15 / 互动率 15 / 活跃粉丝 10 / 内容标签 10 / 粉丝规模 10 / 互动沉淀与粉丝比 5。

## 设计

### 1. 移除 BI 注入与自动计算

- 删除 `backend/app/orchestration/bi_requirements.py`；`AgentLoopContext.required_metrics` 字段移除；`build_agent_context` 不再注入。
- `tasks/executor.py`：移除 finish 覆盖门禁（`missing_metrics` 回喂、`finish_reject_N`、streak 常量与放行逻辑）；**保留**工具级空结果熔断（`_MAX_EMPTY_CALLS_PER_TOOL=2`，与 BI 无关的省积分护栏）及其回喂。
- 任务结束不再自动 `build_analysis_report`、不再自动发 `report.updated`；任务 finish 时把模型 finish 的结论文本写为 assistant 消息（替代报告摘要流 `stream_analysis_summary`）。
- `reporting` 模块整体保留：收藏只读、`GET /api/v1/analysis-reports/{id}`、会话 DTO `latest_analysis_report` 均不动——品牌/活动 BI 区块只是留空。

### 2. 会话 prompt 改为圈选导向

重写 `AGENT_LOOP_PROMPT`（`model/prompts.py`）：目标是圈选匹配 KOL 并采齐导出字段。注入仅两块，其余交给模型：

- **Excel 字段契约**：从 `7e44355^` 恢复 `export_contract.py` 改造（新版本号 `kol_excel_v2`）：字段 平台/昵称/粉丝数/城市/行业兴趣占比/目标地区粉丝占比/目标年龄段占比/互动率/活跃粉丝率/内容标签/主页链接；标签随会话画像动态生成（如「餐饮兴趣」「杭州粉丝」）；规则：不得编造、缺失标「数据缺失」、每个选中平台必须检索。
- **MCP 参数信息**：size≤100、扁平参数、两族平台标签字段差异（小红书/抖音 vs B站/微博/微信）、kol_detail UID 批量上限等硬约束（现 prompt 已有的参数表保留）。

### 3. 圈选名单沉淀（方案 A：代码级）

- 迁移 0020 新建 `session_kol_selections`：
  - `id`、`user_id`、`session_id`、`platform`、`kol_uid`、`nickname`、`followers`、`city`、`profile_url`
  - `fields_json`（契约字段原始值）、`score_json`（维度分 + 综合分 + 评级）
  - `source_tool`、`first_task_id`、`last_task_id`、`created_at`、`updated_at`
  - 唯一约束 `(session_id, platform, kol_uid)`；普通索引 `user_id`。
- 新模块 `backend/app/selection/`：
  - `normalize.py`：两族平台响应解析（从 `ff3b652` 恢复改造），产出统一字段字典。
  - `scoring.py`：8 维度确定性评分（权重同模板），输出维度分、综合分、评级（重点推荐/推荐/可考虑/观察）；缺失字段按规则处理不编造。
  - `service.py`：upsert（去重合并，新数据覆盖空值不覆盖已有值）、列表查询。
- executor 挂钩：KOL 搜索/榜单类工具 settled 后调用沉淀；解析/写库失败只记 warning，不阻塞循环。
- 会话 DTO 增加 `kol_selection_count`。

### 4. Excel 导出（恢复旧管线）

- 从 `7e44355^` 恢复模板 `backend/app/reporting/templates/KOL匹配度分析报告.xlsx` 与 `exporter.py`，适配 `session_kol_selections` + `scoring.py` 输出；4 个 sheet 与模板一致。
- 文件名 `{品牌}_{品类}_KOL匹配度分析_{yyyymmdd_HHMM}.xlsx`。
- `GET /api/v1/sessions/{id}/kol-selection/export` → StreamingResponse 下载；名单为空返回 409 `NO_KOL_SELECTION`。
- `GET /api/v1/sessions/{id}/kol-selection`：名单列表（按综合分倒序，分页），供前端展示。

### 5. 手动 KOL 分析

`POST /api/v1/sessions/{id}/kol-analysis`（需认证、校验会话归属）：

1. 代码聚合名单数据为摘要（平台分布、评级分布、粉丝量级分桶、城市 TOP10、互动率分桶、TOP10 达人）+ 会话画像（brand/category/audience/period）。
2. 提交模型（新 `KOL_ANALYSIS_PROMPT`，走 `complete_json`，log_context purpose="kol_analysis"）输出标准 `ReportDocument`。
3. 复用 `AnalysisReportService.build` 版本化落库 + 发 `report.updated` 事件。
4. 零 MCP 调用、零积分（仅模型 tokens）；名单为空返回 409。

重新设计的 7 个分析项（块类型）：

1. 名单概览（metric_grid）：总数 / 覆盖平台数 / 平均综合分 / 重点推荐数
2. 平台分布（pie_chart）
3. 评级分布（bar_chart：重点推荐/推荐/可考虑/观察）
4. 粉丝量级分布（bar_chart：<10万 / 10-50万 / 50-100万 / 100-500万 / >500万）
5. 城市分布 TOP10（bar_chart）
6. TOP10 推荐达人（table：昵称/平台/粉丝/综合分/评级/评分理由）
7. 投放建议（markdown，模型结论）

### 6. 前端改造

- `UniversalReport.tsx`：右上角加「分析」「导出 Excel」按钮；空态显示「已圈 N 人，点击分析生成 KOL 分析报告」；品牌/活动区块留空占位。
- 分析按钮调 `POST /sessions/{id}/kol-analysis`，复用 SSE `report.updated` 链路刷新；导出按钮直接触发浏览器下载。
- `contracts.ts`：`ApiSession` 增加 `kol_selection_count`。

## 错误处理

- 沉淀解析失败：记 warning 跳过该条，不影响任务循环。
- 导出/分析时名单为空：409 带明确错误码，前端 toast 提示。
- 分析模型输出非法 JSON：按现有 `complete_json` invalid 路径处理，返回 502 并允许重试；prompt 日志照常记录。
- 评分缺字段：按模板规则「数据缺失字段按评分规则处理」，不编造。

## 测试

- 后端 pytest：
  - 删除/改造 `tests/orchestration/test_bi_requirements.py`、`test_loop.py` 与 `test_agent_loop.py` 中 BI 门禁相关用例；空结果熔断用例保留。
  - 新增 `tests/selection/`：normalize（两族平台样例）、scoring（权重/缺失字段/评级边界）、upsert 去重合并。
  - 恢复改造旧 `test_exporter.py`（模板 4 sheet、空名单 409）、export/列表/kol-analysis 端点测试。
- 前端 Vitest：`UniversalReport` 按钮渲染、空态、分析触发与 report.updated 刷新。
- 全量验证：ruff、pytest、`npm run test`、`npm run lint`、`npm run build`。

## 不做的事（YAGNI）

- 品牌/活动 BI 分析（面板留空，模块保留）。
- 用户手动勾选/剔除 KOL、名单编辑 UI。
- 分析定时/自动生成、prompt 日志清理策略。
