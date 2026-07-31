# 品牌分析报告导出与 BI 重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为品牌分析报告引入结构化快照 `brand_report_v2`（payload_json + template_version）、零积分 Excel 导出端点，并把右侧「品牌分析」BI 改为与 Excel 模板章节一一对应的章节式渲染；旧版通用 Block 报告保持兼容降级。

**Architecture:** 后端在 reporting 模块新增三层——`BrandReportDataAssembler`（从 task.plan_json 的 settled 证据确定性归一出 `data` + `availability`）、`BrandReportNarrativeBuilder`（以结构化数据为唯一模型输入生成叙事）、导出渲染器 `brand_exporter.py`（去样例 8 Sheet 模板 + openpyxl 填数 + 图表删除重建）。`run_brand_analysis` 原地演进为 v2 构建器，仍由 `_finalize_analysis_goal` 在 goal 收尾调用；`build_session_report` 增加可选 payload/template_version 落库。`GoalParams` 增加 `comparison_mode`，planner prompt 升 v3、brand loop prompt 升 v2（对比期阶段 + evidence_goal 期别标注）。前端 `TypedReportPanel` 按 payload 有无分流：有 → 章节式 `BrandReportView` + 导出按钮；无 → 旧 Block 渲染 + 不支持导出提示。

**Tech Stack:** FastAPI + SQLAlchemy Async + Alembic + pydantic（后端）；openpyxl（Excel）；React 19 + TypeScript + Vitest（前端）；Playwright（E2E，全部 mock）。

设计依据（必须严格遵循，不得改设计）：`docs/superpowers/specs/2026-07-30-brand-report-export-and-bi-design.md`。

## 关键事实（已核实，行号可能小幅漂移）

- 迁移：`backend/migrations/versions/0025_kol_selection_detail_views.py` 是当前 head（revision id `0025_kol_selection_detail_views`）；新迁移 revision id 用 `0026_brand_report_v2_payload`。迁移测试参照 `backend/tests/tasks/test_message_mediumtext_migration.py` 的 alembic 子进程模式。
- ORM：`backend/app/reporting/models.py:145` `AnalysisReport`（`blocks_json`/`conclusion_text`/`scope_json` 等）。注意 :172 注释把 report_type 写成 `brand_report / campaign_report`，实际代码用 `brand_analysis`/`campaign_analysis`（`brand_report` 是 artifact_type）——Task 1 顺手更正注释。
- DTO：`backend/app/reporting/schemas.py:19` `AnalysisReportRead`；`backend/app/reporting/router.py:60` `analysis_report_read()` 组装；列表端点 `GET /sessions/{session_id}/reports`（router.py:119）走 `SessionReportItem`，**不返回 payload**；详情端点 `GET /analysis-reports/{report_id}`（router.py:231）返回完整 `AnalysisReportRead`。
- 落库：`backend/app/reporting/analysis_reports.py:110` `AnalysisReportService.build_session_report`（会话级、version 按 (session_id, report_type) 编号、SAVEPOINT 重试）。
- 构建器：`backend/app/reporting/builders.py:30` `collect_goal_evidence`（只取 EvidenceNote 摘要）、`:129` `run_brand_analysis`（现为 `_run_goal_analysis` 包装）。调用方 `backend/app/tasks/dependencies.py:413` `_finalize_analysis_goal`，失败语义：异常 → failed `brand_report` Artifact + goal `completed_with_warnings`（:469-501），partial 不算失败。
- analysis-retry：`backend/app/reporting/router.py:155` `POST /sessions/{session_id}/analysis-retry`，`_ANALYSIS_RETRY_BUILDERS` 直接复用 `run_brand_analysis`——签名不变即自动兼容 v2。
- 轨迹：`backend/app/orchestration/loop.py:177` `EvidenceNote(step_id/tool/status/summary)`、`:274` `TrajectoryStep(id/internal_tool_name/arguments/evidence_goal)`、`:308` `restore_agent_trajectory(plan_json)`。组装器用 `EvidenceNote.step_id` 关联 `TrajectoryStep.arguments` 日期 + `evidence_goal` 前缀识别期别。
- GoalParams：`backend/app/goals/schemas.py:20`（`extra="forbid"`）；params 落库链路 `backend/app/tasks/service.py:177` `params_json={**goal_snapshot, **spec_params}` 为纯 dict 合并，GoalParams 加字段后随 planner 输出自动携带。语义校验 `backend/app/goals/validation.py`（comparison_mode 无新增校验需求）。
- prompt：`backend/app/model/prompts.py` `GOAL_PLANNER_SYSTEM_TEXT`（:79，`GOAL_PLANNER_PROMPT` name=`goal_planner_v1` 当前 version="2" → 升 "3"）；`BRAND_ANALYSIS_LOOP_SYSTEM_TEXT`（:183，`BRAND_ANALYSIS_LOOP_PROMPT` name=`brand_loop_v1` version="1" → 升 "2"）；新模板按 `PromptTemplate(name/version/system)` dataclass 定义并注册进 `PROMPTS` dict（:296）。prompt 契约测试在 `backend/tests/goals/test_prompt_contract.py`，版本号变更需同步期望。
- 导出参照：`backend/app/selection/exporter.py`（模板加载、`_write_styled_row`、`asyncio.to_thread`、`_cell_value` 防公式注入）；端点响应写法参照 `backend/app/selection/router.py:308`（`Response` + `Content-Disposition: attachment; filename*=UTF-8''{quote(filename)}`）。
- 模板探针结论：根目录 `brand_report.xlsx` 共 8 Sheet（综合概览/情感分析/日趋势/内容类型与达人/地域分布/热门帖子TOP/舆情洞察/方法论），含 3 个图表（日趋势 2 个 LineChart「每日声量趋势」「每日互动数趋势」、地域分布 1 个 BarChart「TOP 20 省份声量分布」）；样例数据含跨行公式（如 `=C4/SUM(C4:C6)`），行数可变时公式不可保留——导出直接写 payload 中已算好的占比数值。
- 前端：`src/components/UniversalReport.tsx:526` `TypedReportPanel`（brand/campaign 共用：版本列表 → 选中 report_id → `getAnalysisReport` 详情 → `ReportBlocks`）；三个一级 Tab 在 :1005-1073。导出下载参照 `src/api/kolSelection.ts:85` `downloadKolSelection`（authorizedFetch → blob → a[download]）；报告 API 在 `src/api/reports.ts` 与 `src/api/tasks.ts:67`。契约 `src/api/contracts.ts:289` `ApiAnalysisReport`。
- 测试惯例：后端 pytest 目录对齐 app/（`backend/tests/reporting/`、`backend/tests/goals/`），fixture 参照 `backend/tests/reporting/test_builders.py` 的 `FakeModel.complete_json` 返回 `StructuredResult`；前端 Vitest 与组件同目录（`Xxx.test.tsx`）；E2E 全部 `page.route` mock，参照 `e2e/analysis-report.spec.ts`。**任何步骤不得真实调 MCP/模型烧积分。**
- DataTap 工具输出字段名以 `docs/datatap-mcp-tools.md` 为准；归一化取值参照 `backend/app/selection/normalizers.py` 的防御式模式。

## brand_report_v2 数据契约骨架（Task 4 实现，全计划统一引用）

```python
# backend/app/reporting/brand_payload.py
class PeriodValue(BaseModel):  # 单期数值 + 取得状态
    value: float | None = None
    status: Literal["ok", "not_requested", "restricted"] = "ok"
    reason: str | None = None   # restricted 时必填，如 invalid_period / insufficient_points / no_data / tool_failed

class ChapterAvailability(BaseModel):
    status: Literal["complete", "partial", "unavailable"]
    missing_fields: list[str] = []
    reason: str | None = None
    source_tools: list[str] = []
    collected_at: str | None = None

class TopPostRow(BaseModel):
    platform: str
    post_id: str | None = None        # 平台原始帖子标识
    collected_at: str | None = None
    title: str | None = None          # 缺失保留 null，前端/Excel 显示「未提供」
    author: str | None = None
    interactions: int | None = None
    exposure_count: int | None = None # 小红书=阅读数 / 抖音=播放数
    like_count: int | None = None
    comment_count: int | None = None
    collect_count: int | None = None  # 仅源字段存在时展示
    share_count: int | None = None    # 小红书=转发 / 抖音=分享
    sentiment: str | None = None
    creator_tier: str | None = None
    url: str | None = None            # 仅 MCP 返回的合法 URL，禁止拼接猜测

class BrandReportPayload(BaseModel):
    template_version: Literal["brand_report_v2"] = "brand_report_v2"
    data_status: Literal["complete", "partial"]  # 7 个数据章节全 complete 才为 complete
    scope: ReportScope            # brand/period_start/period_end/platforms/comparison_mode/data_as_of
    query_spec: QuerySpec         # original_term/matched_tag/fallback_keyword/comparison_definition
    data: BrandReportData         # overview/sentiment/daily_trend/content_types/creator_tiers/organic_vs_paid/regions(≤20)/top_posts(每平台≤15)
    narrative: BrandReportNarrative | None = None  # Task 5 模型产出后回填
    availability: dict[str, ChapterAvailability]   # 8 章节键：overview/sentiment/daily_trend/content_creators/regions/top_posts/insights/methodology
    sources: list[SourceEntry]    # tool/collected_at/step_id
```

章节键与 Excel Sheet/BI 章节一一对应；`data` 是唯一数值事实来源，`narrative` 只能引用 `data`。

---

## Task 1：迁移 0026 + ORM + DTO 贯通

**Files:**
- Create: `backend/migrations/versions/0026_brand_report_v2_payload.py`
- Create: `backend/tests/reporting/test_brand_report_payload_migration.py`
- Modify: `backend/app/reporting/models.py`（AnalysisReport 加两列 + 更正 :172 注释）
- Modify: `backend/app/reporting/schemas.py`（AnalysisReportRead 加可选字段）
- Modify: `backend/app/reporting/router.py`（analysis_report_read 带出新字段）
- Modify: `src/api/contracts.ts`（ApiAnalysisReport 加可选字段，payload 暂为 `Record<string, unknown> | null`，Task 8 细化）

迁移骨架：

```python
revision: str = "0026_brand_report_v2_payload"
down_revision: str | None = "0025_kol_selection_detail_views"

def upgrade() -> None:
    op.add_column("analysis_reports", sa.Column("payload_json", sa.JSON(), nullable=True))
    op.add_column("analysis_reports", sa.Column("template_version", sa.String(32), nullable=True))

def downgrade() -> None:
    op.drop_column("analysis_reports", "template_version")
    op.drop_column("analysis_reports", "payload_json")
```

ORM 骨架（models.py AnalysisReport 内）：

```python
# brand_report_v2 结构化快照：仅新品牌报告写入，旧行保持 NULL。
payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
template_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
```

DTO 骨架（schemas.py AnalysisReportRead 内）：

```python
payload: dict[str, Any] | None = None
template_version: str | None = None
```

- [ ] 写失败测试 `test_brand_report_payload_migration.py`：① alembic head == `0026_brand_report_v2_payload`（ScriptDirectory.get_current_head，参照 test_message_mediumtext_migration.py）；② upgrade 后 information_schema 存在两列、downgrade 一步后两列消失、再 upgrade 恢复（子进程跑 alembic，测试库）；③ ORM 往返：插入 AnalysisReport(payload_json={"a": 1}, template_version="brand_report_v2") 读回一致；不带两列插入的旧式行读回为 None。
- [ ] 写失败测试（可放 `backend/tests/reporting/test_session_reports.py` 或新文件）：`GET /analysis-reports/{id}` 响应含 `payload`/`template_version`；`GET /sessions/{id}/reports` 列表项**不含** payload 字段。
- [ ] 跑 `cd backend && .venv/bin/pytest tests/reporting/test_brand_report_payload_migration.py -q` 确认失败（列不存在）。
- [ ] 实现迁移 + ORM + schemas + router.py 的 `analysis_report_read()`（`payload=report.payload_json, template_version=report.template_version`）。
- [ ] 迁移测试库：`cd backend && APP_ENV=test .venv/bin/alembic upgrade head`（命令以 README 为准），跑新增测试确认通过。
- [ ] 前端 contracts.ts：`ApiAnalysisReport` 加 `payload?: Record<string, unknown> | null; template_version?: string | null;`，跑 `npm run lint` 确认无类型错误。
- [ ] 跑 `cd backend && .venv/bin/pytest tests/reporting -q` 确认既有用例全绿。
- [ ] 提交：`git add backend/migrations/versions/0026_brand_report_v2_payload.py backend/app/reporting backend/tests/reporting src/api/contracts.ts && git commit -m "品牌报告：analysis_reports 增加 payload/template_version 两列"`

## Task 2：GoalParams.comparison_mode + planner prompt v3

**Files:**
- Modify: `backend/app/goals/schemas.py`（GoalParams 加字段）
- Modify: `backend/app/model/prompts.py`（GOAL_PLANNER_SYSTEM_TEXT 加落参规则；GOAL_PLANNER_PROMPT version "2"→"3"）
- Modify: `backend/tests/goals/test_schemas.py`
- Modify: `backend/tests/goals/test_prompt_contract.py`（版本期望同步）
- Modify: `backend/tests/tasks/test_enforce_create_task.py`（comparison_mode 落 params_json）

骨架（goals/schemas.py GoalParams 内）：

```python
# 品牌分析对比口径：mom=仅环比（默认）；mom_yoy=环比+同比（用户明确要求时）。
comparison_mode: Literal["mom", "mom_yoy"] = "mom"
```

prompt 追加要点（GOAL_PLANNER_SYSTEM_TEXT 尾部，保持原有约束风格）：

- brand_analysis Goal 必须落 `params.comparison_mode`：用户消息明确要求同比、或在澄清中选择「环比+同比」时为 `mom_yoy`；其余品牌分析一律 `mom`。
- 澄清可在时间窗确认后提供「环比」与「环比+同比」选项；用户未选择不阻塞，默认 `mom`。
- campaign_analysis / kol_selection 不输出该字段（extra=forbid 在 GoalParams 上允许字段存在，但语义上只对品牌有意义；planner 规则写明仅 brand_analysis 使用）。

- [ ] 写失败测试（tests/goals/test_schemas.py）：GoalParams 默认 `comparison_mode == "mom"`；`"mom_yoy"` 合法；`"yoy"` 等非法值 ValidationError；旧输入（无该字段）解析通过。
- [ ] 写失败测试（tests/tasks/test_enforce_create_task.py）：enforce 路径 planner 输出 brand_analysis goal 且 `comparison_mode="mom_yoy"` → 落库 `task_goals.params_json["comparison_mode"] == "mom_yoy"`；未输出时默认不落或落 `"mom"`（按实现选择断言，保持与 GoalParams 默认一致）。
- [ ] 跑 `cd backend && .venv/bin/pytest tests/goals/test_schemas.py tests/tasks/test_enforce_create_task.py -q` 确认失败。
- [ ] 实现 schemas 字段 + prompts.py 文本与 version="3"。
- [ ] 跑上述测试 + `tests/goals/test_prompt_contract.py`（更新版本期望）+ `tests/goals -q` 全绿。
- [ ] 提交：`git add backend/app/goals/schemas.py backend/app/model/prompts.py backend/tests/goals backend/tests/tasks/test_enforce_create_task.py && git commit -m "GoalParams 增加 comparison_mode，planner prompt 升 v3"`

## Task 3：brand loop prompt v2（对比期阶段 + 期别标注）

**Files:**
- Modify: `backend/app/model/prompts.py`（BRAND_ANALYSIS_LOOP_SYSTEM_TEXT 增补；BRAND_ANALYSIS_LOOP_PROMPT version "1"→"2"）
- Modify: `backend/tests/goals/test_prompt_contract.py`

prompt 增补要点（插入采集策略段之后，保持现有约束段不变）：

- 执行顺序：当期最小证据（标签匹配→概览）→ 对比期最小证据 → 其余模板维度（趋势/话题/受众/热帖/地域等）。
- 对比期由 `goal_params.comparison_mode` 与 `goal_params.period` 决定：`mom` 额外查询紧邻当前期的上一个等长周期；`mom_yoy` 在环比之外再查询上一自然年相同起止日期（2 月 29 日向前平移为 2 月 28 日）；无有效 period 时**不得猜测对比窗**，跳过对比期阶段。
- 对比期查询复用当期已获得的品牌标签/关键词、平台集合与统计口径。
- 每条 call_tool 的 `evidence_goal` 必须以 `current:` / `mom:` / `yoy:` 前缀标注该调用属于哪个期别（例：`current: 小红书当期声量概览`）。
- 每次 MCP 调用 10 积分；余额不足时保留已 settled 证据直接 finish，不得重试对比期调用。

- [ ] 写失败测试（tests/goals/test_prompt_contract.py 或新增断言）：`brand_loop_v1` version == "2"；system 文本包含锚点子串 `current:`、`mom:`、`yoy:`、`comparison_mode`、`2 月 28 日`、`不得猜测对比窗`（或等价表述，与最终文本一致）。
- [ ] 跑 `cd backend && .venv/bin/pytest tests/goals/test_prompt_contract.py -q` 确认失败。
- [ ] 实现 prompt 文本与版本升级。
- [ ] 跑 `tests/goals -q` 全绿。
- [ ] 提交：`git add backend/app/model/prompts.py backend/tests/goals/test_prompt_contract.py && git commit -m "brand loop prompt 升 v2：对比期阶段与期别标注"`

## Task 4：BrandReportDataAssembler + payload Schema

**Files:**
- Create: `backend/app/reporting/brand_payload.py`（契约模型，见文件头骨架）
- Create: `backend/app/reporting/brand_assembler.py`
- Create: `backend/tests/reporting/test_brand_assembler.py`

组装器骨架：

```python
# backend/app/reporting/brand_assembler.py
def comparison_windows(period: GoalPeriod, mode: Literal["mom", "mom_yoy"]) -> dict[str, tuple[date, date]]:
    """mom=紧邻上一等长周期；yoy=上一自然年同区间（2/29→2/28）。mode=mom 时不含 yoy。"""

def assemble_brand_report(
    task_plan_json: dict[str, Any] | None,
    goal_params: dict[str, Any],
    *,
    warning_code: str | None = None,
) -> BrandReportPayload:
    """settled 证据 → brand_report_v2（data+availability+query_spec+sources；narrative 留空）。

    - restore_agent_trajectory(plan_json) 取 steps/results；按 EvidenceNote.step_id
      关联 TrajectoryStep.arguments 的起止日期。
    - 期别判定：evidence_goal 前缀 current:/mom:/yoy: 优先；缺失时用 arguments
      日期与 comparison_windows 结果精确匹配兜底；都判不出按 current 处理。
    - 归一化按工具名子串映射（overview/trend/sentiment/hot_topic/user_profile/
      原帖工具），防御式取值：缺失字段为 null 并记入对应章节 missing_fields。
    - top_posts 按互动量降序、每平台 ≤15；regions 按声量降序 ≤20。
    - 趋势最大日期 < period.end 时写 scope.data_as_of。
    - 无有效 period 时对比章节 PeriodValue(status="restricted", reason="invalid_period")。
    - 对比证据缺失/工具失败 → 对应 PeriodValue restricted + reason；未请求同比 → not_requested。
    - 综合概览最小证据（任一平台当期 overview settled）缺失 → raise LookupError("no_evidence_collected")。
    - warning_code（如 brand_trend_data_unavailable）合并进对应章节 availability.reason。
    """
```

`data_status` 聚合：7 个数据章节（overview/sentiment/daily_trend/content_creators/regions/top_posts/insights）全 complete → `complete`，否则 `partial`；methodology 恒 complete（服务端生成，不参与降级）。insights 章节状态规则：sentiment 或 top_posts 任一有证据 → complete/partial 随其缺失字段，否则 `unavailable` + reason。

测试 fixture：手工构造 plan_json（`{"schema": "agent_trajectory_v1", "steps": [...], "results": [...]}`），工具输出形状以 `docs/datatap-mcp-tools.md` 为准简化构造。

- [ ] 写失败测试 test_brand_assembler.py，核心断言：
  - 完整证据 → `data_status == "complete"`，overview 各平台指标与合计正确，环比/同比百分比由 data 计算（非模型）。
  - `comparison_windows`：mom 窗为紧邻上一等长周期；yoy 窗跨年；period 含 2/29 → yoy 起点平移 2/28；`mode="mom"` 返回 dict 无 yoy 键。
  - 期别识别：evidence_goal `mom:` 前缀 + arguments 日期落在 mom 窗 → 计入环比；无前缀时日期匹配兜底。
  - top_posts：>15 条截断到 15、非品牌相关剔除、按互动量降序；regions >20 截断。
  - 热帖字段缺失 → null 保留（不填 0、不拼 URL）；`url` 非合法 URL → null。
  - 趋势缺尾日 → `scope.data_as_of` 为最大证据日期。
  - 无 period 的 goal_params → 对比 PeriodValue `restricted/invalid_period`；`comparison_mode="mom"` 时 yoy 恒 `not_requested`。
  - 无任何 overview 证据 → `LookupError("no_evidence_collected")`。
  - 某维度证据缺失 → 对应章节 `unavailable` + `missing_fields` + `source_tools` + `collected_at`，整体 `data_status == "partial"`。
  - payload `model_dump(mode="json")` 后可被 `BrandReportPayload.model_validate` 往返（导出端点校验复用）。
- [ ] 跑 `cd backend && .venv/bin/pytest tests/reporting/test_brand_assembler.py -q` 确认失败（模块不存在）。
- [ ] 实现 brand_payload.py + brand_assembler.py。
- [ ] 跑测试确认通过；`.venv/bin/ruff check app/reporting tests/reporting`。
- [ ] 提交：`git add backend/app/reporting/brand_payload.py backend/app/reporting/brand_assembler.py backend/tests/reporting/test_brand_assembler.py && git commit -m "品牌报告 v2 数据组装器：确定性归一与可用性追踪"`

## Task 5：BrandReportNarrativeBuilder + 叙事 prompt

**Files:**
- Create: `backend/app/reporting/brand_narrative.py`
- Create: `backend/tests/reporting/test_brand_narrative.py`
- Modify: `backend/app/model/prompts.py`（新增 BRAND_REPORT_NARRATIVE 模板并注册 PROMPTS）

骨架：

```python
# prompts.py
BRAND_REPORT_NARRATIVE_SYSTEM_TEXT = """你是受约束的品牌报告叙事撰写器。...
只能引用传入 data 中的数值与明细，禁止创造、换算或修改任何指标；
availability 非 complete 的章节不得输出该维度的数值结论；对比期 status 非 ok 时不得引用比较结论；
输出字段：praise_points/complaint_points/impact_level/expansion_signals/noise_notes/key_findings/conclusion/recommendations。..."""
BRAND_REPORT_NARRATIVE_PROMPT = PromptTemplate(
    name="brand_report_narrative_v1", version="1", system=BRAND_REPORT_NARRATIVE_SYSTEM_TEXT
)

# brand_narrative.py
class BrandReportNarrative(BaseModel):
    model_config = ConfigDict(extra="forbid")
    praise_points: list[str] = []        # 好评点
    complaint_points: list[str] = []     # 槽点
    impact_level: Literal["低", "中", "高"] = "低"  # 负面影响程度
    expansion_signals: list[str] = []    # 扩张信号
    noise_notes: str | None = None       # 噪音说明
    key_findings: list[str] = []         # 情感关键发现
    conclusion: str = ""                 # AI 结论
    recommendations: list[str] = []      # 结论与建议

async def build_brand_narrative(
    model: ModelAdapter, payload: BrandReportPayload, *, log_context: dict[str, Any]
) -> BrandReportNarrative:
    """模型输入只有 data + availability（JSON），purpose='brand_report_narrative'，
    tags=['brand_report_narrative']，经 complete_json 统一出口落 model_prompt_logs。"""
```

- [ ] 写失败测试 test_brand_narrative.py：① FakeModel 捕获请求，断言 user content JSON 只含 `data`/`availability` 两键（不含原始 evidence、不含 sources 内部 step_id）；② 正常输出解析为 BrandReportNarrative；③ 模型输出缺必填字段/多字段 → 校验异常上抛（由调用方走失败 Artifact 路径）；④ `request.purpose == "brand_report_narrative"` 且 log_context.tags 含同名。
- [ ] 写失败测试（test_prompt_contract.py）：`brand_report_narrative_v1` 已注册、version="1"、system 含「只能引用传入 data」锚点。
- [ ] 跑 `cd backend && .venv/bin/pytest tests/reporting/test_brand_narrative.py tests/goals/test_prompt_contract.py -q` 确认失败。
- [ ] 实现 brand_narrative.py + prompts.py 模板注册。
- [ ] 跑测试确认通过。
- [ ] 提交：`git add backend/app/reporting/brand_narrative.py backend/app/model/prompts.py backend/tests/reporting/test_brand_narrative.py backend/tests/goals/test_prompt_contract.py && git commit -m "品牌报告叙事层：结构化数据唯一输入的模型撰写"`

## Task 6：run_brand_analysis 演进 v2 + 落库 + analysis-retry 兼容

**Files:**
- Modify: `backend/app/reporting/builders.py`（run_brand_analysis 原地演进；campaign 路径不动）
- Modify: `backend/app/reporting/analysis_reports.py`（build_session_report 加可选参数）
- Modify: `backend/tests/reporting/test_builders.py`
- Modify: `backend/tests/reporting/test_analysis_reports.py`
- Modify: `backend/tests/reporting/test_analysis_retry.py`

骨架：

```python
# analysis_reports.py build_session_report 新增可选 kwargs（现有调用方零改动）：
async def build_session_report(self, *, user_id, session_id, document,
    report_type="kol_analysis", scope=None,
    payload: dict[str, Any] | None = None,
    template_version: str | None = None,
) -> AnalysisReport: ...
# report = AnalysisReport(..., payload_json=payload, template_version=template_version)

# builders.py
async def run_brand_analysis(db, model, *, user_id, session_id, task, goal,
                             thinking_sink=None, warning_code=None) -> AnalysisReport:
    params = goal.params_json if isinstance(getattr(goal, "params_json", None), dict) else {}
    payload = assemble_brand_report(getattr(task, "plan_json", None), params, warning_code=warning_code)
    narrative = await build_brand_narrative(model, payload, log_context={
        "user_id": user_id, "session_id": session_id, "task_id": task.id,
        "tags": ["brand_report_narrative"],
    })
    payload = payload.model_copy(update={"narrative": narrative})
    document = build_brand_compat_document(payload)  # 见下
    scope = _goal_scope(params, ("brand", "period", "platforms"))
    return await AnalysisReportService(db).build_session_report(
        user_id=user_id, session_id=session_id, document=document,
        report_type="brand_analysis", scope=scope,
        payload=payload.model_dump(mode="json"), template_version="brand_report_v2",
    )
```

`build_brand_compat_document(payload)`（纯代码，不调模型）：从 payload 生成兼容 `ReportDocument`——metric_grid（总声量/总互动/覆盖平台/时间窗）、pie（情感占比）、line（日趋势）、table（热帖前若干行）、markdown（conclusion + recommendations）、sources 块；缺数据的块整块省略。旧 BI/其他消费方行为不变。

- [ ] 写失败测试（test_analysis_reports.py）：build_session_report 传 payload/template_version → 落库两列正确；不传 → 两列 NULL（kol_analysis 旧行为）。
- [ ] 写失败测试（test_builders.py）：FakeModel 返回固定 narrative → run_brand_analysis 落库行 `template_version == "brand_report_v2"`、`payload_json` 可被 BrandReportPayload 校验、`blocks_json` 仍是非空兼容 Block、`conclusion_text` 为 narrative.conclusion；空证据 → `LookupError("no_evidence_collected")`；叙事模型抛 ModelAdapterError → 异常上抛（不落成 partial 报告——叙事失败即构建失败，由 finalize 降级）。
- [ ] 写失败测试（test_analysis_retry.py）：retry 品牌报告 → 新版本带 payload/template_version，旧版本行不被改写。
- [ ] 跑 `cd backend && .venv/bin/pytest tests/reporting -q` 确认新增失败、旧用例红绿符合预期。
- [ ] 实现 analysis_reports.py + builders.py。
- [ ] 跑 `tests/reporting -q` + `tests/tasks -q` 全绿（finalize 调用方签名未变）。
- [ ] 提交：`git add backend/app/reporting backend/tests/reporting && git commit -m "run_brand_analysis 演进 v2：快照+叙事+兼容 Block 一次落库"`

## Task 7：模板迁移脚本 + 导出渲染器 + 导出端点

**Files:**
- Create: `backend/scripts/build_brand_report_template.py`（一次性模板生成脚本，入库）
- Create: `backend/app/reporting/templates/brand_report_v2.xlsx`（脚本产物，提交）
- Create: `backend/app/reporting/brand_exporter.py`
- Create: `backend/tests/reporting/test_brand_exporter.py`
- Create: `backend/tests/reporting/test_brand_report_export_api.py`
- Modify: `backend/app/reporting/router.py`（新增导出端点）

模板脚本做法（脚本入库 + 运行一次提交产物）：读仓库根 `brand_report.xlsx` → 逐 Sheet 清除样例数据行（保留表头行、合并拓扑、列宽、数字格式、章节标题）→ 删除 3 个样例图表（`ws._charts = []`）→ 移除「Python openpyxl」等旧生成说明文字 → 写 `backend/app/reporting/templates/brand_report_v2.xlsx`。脚本带 `if __name__ == "__main__"`，路径用 `Path(__file__)` 相对定位。

渲染器骨架：

```python
# backend/app/reporting/brand_exporter.py
TEMPLATE_PATH = Path(__file__).with_name("templates") / "brand_report_v2.xlsx"
CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEET_ORDER = ("综合概览", "情感分析", "日趋势", "内容类型与达人", "地域分布", "热门帖子TOP", "舆情洞察", "方法论")

def sanitize_report_filename(brand: str, start: str, end: str, version: int) -> str:
    """剔除控制字符与 < > : " / \\ | ? *；合并连续空白/下划线；去尾部点和空格；
    品牌片段截 80 字符；清洗后为空用「未命名品牌」。
    返回 f"{品牌}_品牌社媒分析报告_{start}-{end}_v{version}.xlsx"。"""

def render_brand_workbook(payload: BrandReportPayload) -> bytes:
    """同步 openpyxl 渲染（调用方 asyncio.to_thread）。
    - 逐 Sheet 按固定表头行写数据；占比直接写 payload 已算数值（不写跨行公式）。
    - 缺失单元格写「未提供」；url 列写合法 URL 或「未提供」。
    - 日趋势两 LineChart + 地域 BarChart：数据存在才新建（openpyxl.chart.LineChart/BarChart，
      数据引用指向本工作簿已填数据区）；无数据不建图，Sheet 内写受限说明。
    - 空章节：保留 Sheet + 列头 + availability.reason 受限说明，不隐藏。
    - 方法论 Sheet 由 payload.scope/query_spec/sources + comparison_mode 口径生成。
    - 任何异常向上抛，由端点映射为明确错误，绝不输出半截文件。
    """

async def export_brand_report(db, user_id: str, session_id: str, report_id: str) -> ExportedWorkbook:
    """归属与类型校验（全部失败统一 LookupError('report_not_found')，不泄漏存在性）：
    session 归属 user → report.session_id == session_id → report_type == 'brand_analysis'
    → template_version == 'brand_report_v2' → BrandReportPayload.model_validate(report.payload_json)。
    不调用模型/MCP/积分系统。文件名取 payload.scope 的品牌与周期 + report.version。"""
```

端点骨架（reporting/router.py）：

```python
@router.get("/sessions/{session_id}/reports/{report_id}/export")
async def export_session_report(session_id: str, report_id: str, user: CurrentUser,
                                db: Annotated[AsyncSession, Depends(get_db)]) -> Response:
    try:
        workbook = await export_brand_report(db, user.id, session_id, report_id)
    except LookupError as error:
        raise not_found(str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail="EXPORT_RENDER_FAILED") from error
    return Response(content=workbook.content, media_type=workbook.content_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(workbook.filename)}"})
```

- [ ] 写失败测试 test_brand_exporter.py：① `sanitize_report_filename` 用例集（非法字符、连续空白、尾部点空格、超长截断、空品牌→未命名品牌）；② 完整 payload 渲染：8 Sheet 齐全、Sheet 顺序固定、表头行内容、关键数值与百分比格式、日趋势两图与地域图存在且数据范围指向已填区域；③ 趋势/地域为空 → 对应 Sheet 无图表（`ws._charts == []`）且有受限说明；④ 热帖 null 字段 → 「未提供」、无 URL 行不出现链接；⑤ 渲染结果可再被 openpyxl 读回（往返不损坏）。
- [ ] 写失败测试 test_brand_report_export_api.py：成功 200 + content-type + 文件名；跨用户/跨会话 report_id → 404；report_type=kol_analysis → 404；template_version 为 NULL 的旧报告 → 404；payload 损坏（非法 JSON 形状）→ 404；端点不注入模型依赖（router 签名断言或直接读源码层面无 get_model_adapter——用测试客户端调用即可，依赖注入失败会 500 暴露）。
- [ ] 跑 `cd backend && .venv/bin/pytest tests/reporting/test_brand_exporter.py tests/reporting/test_brand_report_export_api.py -q` 确认失败。
- [ ] 写模板脚本并运行一次：`cd backend && .venv/bin/python scripts/build_brand_report_template.py`，人工核对产物（8 Sheet、无样例品牌「昊来了」残留、无图表）。
- [ ] 实现 brand_exporter.py + router.py 端点。
- [ ] 跑两个测试文件 + `tests/reporting -q` 全绿。
- [ ] 提交：`git add backend/scripts/build_brand_report_template.py backend/app/reporting/templates/brand_report_v2.xlsx backend/app/reporting/brand_exporter.py backend/app/reporting/router.py backend/tests/reporting && git commit -m "品牌报告 Excel 导出：v2 模板渲染与导出端点"`

## Task 8：前端契约细化 + 章节式 BI（BrandReportView）

**Files:**
- Modify: `src/api/contracts.ts`（BrandReportPayload 类型细化，与后端 brand_payload.py 镜像）
- Modify: `src/api/reports.ts`（新增 downloadBrandReport）
- Modify: `src/api/reports.test.ts`
- Create: `src/components/BrandReportView.tsx`
- Create: `src/components/BrandReportView.test.tsx`
- Modify: `src/components/UniversalReport.tsx`（TypedReportPanel 分流）
- Modify: `src/components/UniversalReport.test.tsx`

契约骨架（contracts.ts）：

```ts
export interface BrandReportPeriodValue { value: number | null; status: 'ok' | 'not_requested' | 'restricted'; reason?: string | null }
export interface BrandReportChapterAvailability { status: 'complete' | 'partial' | 'unavailable'; missing_fields: string[]; reason?: string | null; source_tools: string[]; collected_at?: string | null }
export interface BrandReportTopPost { platform: string; post_id?: string | null; collected_at?: string | null; title: string | null; author: string | null; interactions: number | null; exposure_count: number | null; like_count: number | null; comment_count: number | null; collect_count: number | null; share_count: number | null; sentiment: string | null; creator_tier: string | null; url: string | null }
export interface BrandReportPayload {
  template_version: 'brand_report_v2';
  data_status: 'complete' | 'partial';
  scope: { brand: string; period_start: string | null; period_end: string | null; platforms: string[]; comparison_mode: 'mom' | 'mom_yoy'; data_as_of?: string | null };
  query_spec: { original_term: string; matched_tag?: string | null; fallback_keyword?: string | null; comparison_definition: string };
  data: { /* overview/sentiment/daily_trend/content_types/creator_tiers/organic_vs_paid/regions/top_posts，与后端镜像 */ };
  narrative?: { praise_points: string[]; complaint_points: string[]; impact_level: string; expansion_signals: string[]; noise_notes?: string | null; key_findings: string[]; conclusion: string; recommendations: string[] } | null;
  availability: Record<string, BrandReportChapterAvailability>;
  sources: Array<{ tool: string; collected_at?: string | null }>;
}
// ApiAnalysisReport.payload 类型由 Record<string, unknown> 收窄为 BrandReportPayload | null
```

API 骨架（reports.ts，照 downloadKolSelection 模式）：

```ts
export async function downloadBrandReport(sessionId: string, reportId: string): Promise<void> {
  const response = await authorizedFetch(`/api/v1/sessions/${sessionId}/reports/${reportId}/export`);
  // blob → Content-Disposition 解码文件名 → a[download] 点击 → revokeObjectURL
}
```

BrandReportView 要点：

- 章节导航（概览｜情感｜趋势｜内容与达人｜地域｜热帖｜舆情｜方法论）为同页锚点，点击 `scrollIntoView` 并高亮当前章节；不拆 Tab。
- 每章节标题旁按 `availability[章节键]` 渲染受限标记（受限 + 原因）；空章节不隐藏，显示受限说明。
- 趋势章节 `unavailable` 时只显示受限说明，禁止用 overview 变化字段伪造折线。
- 热帖：平台切换（小红书/抖音）、标题 `line-clamp-2` + 展开按钮（展开显示全文与原帖跳转）、`url` 为 null 不渲染跳转按钮、null 字段显示「未提供」、exposure 标签按平台「阅读数/播放数」、share 标签按平台「转发/分享」。
- 方法论为折叠卡片（默认收起）。
- 「AI 结论」（narrative.conclusion）与「结论与建议」（narrative.recommendations）沿用既有 Card 视觉，置于全部数据章节之后。
- 图表用 Recharts（既有依赖）；表格横向滚动 `overflow-x-auto`。

TypedReportPanel 分流（UniversalReport.tsx）：

```tsx
const isBrandV2 = reportType === 'brand_analysis'
  && report?.template_version === 'brand_report_v2' && report?.payload;
// 顶栏：isBrandV2 时显示数据状态徽标（完整/数据受限）与「导出报告」按钮
//   按钮调 downloadBrandReport(sessionId, selectedReportId)，随版本切换联动，带 loading/错误提示
// 主体：isBrandV2 ? <BrandReportView report={report} /> : <ReportBlocks report={report} />
//   非 v2 的品牌报告在 ReportBlocks 上方加提示「该历史版本不支持模板导出」
```

- [ ] 写失败测试 reports.test.ts：downloadBrandReport 调用正确 URL、解析 Content-Disposition 文件名、触发下载。
- [ ] 写失败测试 BrandReportView.test.tsx：完整 payload 渲染 8 章节 + AI 结论/建议置后；受限章节显示原因；热帖 null 字段「未提供」、无 url 无跳转按钮、标题展开交互；锚点导航点击滚动（jsdom 下 stub scrollIntoView）。
- [ ] 写失败测试 UniversalReport.test.tsx：brand + payload → 章节式渲染 + 导出按钮；brand 旧报告（无 payload）→ 旧 Block + 「不支持模板导出」提示；campaign 永走旧渲染；版本切换后导出按钮目标 report_id 联动。
- [ ] 跑 `npm run test -- src/api/reports.test.ts src/components/BrandReportView.test.tsx src/components/UniversalReport.test.tsx` 确认失败。
- [ ] 实现 contracts.ts / reports.ts / BrandReportView.tsx / UniversalReport.tsx 改动。
- [ ] 跑 `npm run test` 全绿 + `npm run lint`。
- [ ] 提交：`git add src/api/contracts.ts src/api/reports.ts src/api/reports.test.ts src/components/BrandReportView.tsx src/components/BrandReportView.test.tsx src/components/UniversalReport.tsx src/components/UniversalReport.test.tsx && git commit -m "品牌分析 BI 章节式重构与报告导出下载"`

## Task 9：E2E（全部 mock，不调真实后端 MCP）

**Files:**
- Create: `e2e/brand-report-export.spec.ts`

要点（参照 `e2e/analysis-report.spec.ts` 的 route mock 模式）：

- mock 会话（含最新 brand artifact 摘要）、`GET /sessions/{id}/reports?report_type=brand_analysis` 返回两版（v2 新版带 payload、v1 旧版无 payload）、`GET /analysis-reports/{id}` 按 id 分别返回带 payload 的完整报告与旧 Block 报告。
- mock 导出端点：`route.fulfill({ body: Buffer.from('xlsx'), headers: { 'Content-Disposition': "attachment; filename*=UTF-8''..." } })`。
- 断言：品牌分析 Tab 渲染章节导航与 8 章节；切到旧版 → 出现「不支持模板导出」提示且无导出按钮；切回新版 → 点击导出触发 `page.waitForEvent('download')` 且 suggestedFilename 符合清洗规则；partial 报告显示「数据受限」徽标与章节受限原因。
- [ ] 写 e2e/brand-report-export.spec.ts。
- [ ] 跑 `npm run test:e2e -- e2e/brand-report-export.spec.ts`（先确认 8000/5173 端口空闲）确认通过。
- [ ] 提交：`git add e2e/brand-report-export.spec.ts && git commit -m "E2E：品牌报告版本切换与导出下载"`

## Task 10：全量验证 + 文档同步

- [ ] 后端：`cd backend && .venv/bin/ruff check app tests && .venv/bin/pytest -q` 全绿。
- [ ] 前端：`npm run test && npm run lint && npm run build` 全绿。
- [ ] E2E：`npm run test:e2e` 全绿（确认端口空闲）。
- [ ] 追加当日 `changelog/2026-07-30.md`（结构：背景与目标 / 主要改动（含关键文件）/ 验证结果 / 遗留事项），要点：payload/template_version 两列、comparison_mode、planner v3 / brand loop v2、组装器+叙事层、导出端点、章节式 BI、模板去样例。
- [ ] 检查并同步 `AGENTS.md`：多意图阶段三段落的报告构建描述（brand_analysis 落 payload/template_version、导出端点路径、comparison_mode 参数、prompt 版本号）与代码现状一致。
- [ ] 提交：`git add changelog/2026-07-30.md AGENTS.md && git commit -m "文档：品牌报告导出与 BI 重构变更记录"`

## 实施注意事项（spec 边界内的实现选择）

- **期别判定**：`evidence_goal` 是自由文本（≤300 字符），spec 要求 loop 用它标注期别。采用「`current:`/`mom:`/`yoy:` 前缀优先、`TrajectoryStep.arguments` 日期精确匹配 `comparison_windows` 兜底」的双重判定；两者都判不出按 current 处理并记 warning，不阻塞构建。
- **Excel 公式**：样例模板的占比公式（`=C4/SUM(C4:C6)`）在可变行数下不可保留，导出直接写 payload 已算好的占比数值；图表数据引用导出工作簿内已填数据区。
- **热帖 Sheet 双平台**：模板样例为小红书 + 抖音两段顺序排列；导出按平台顺序重写两段（行数动态，参照 selection exporter 的 unmerge/clear/rewrite 模式）。
- **叙事失败语义**：叙事模型调用失败 = 报告构建失败（异常上抛走 failed Artifact 路径），不落「只有 data 没有 narrative」的报告，保证 payload 内 narrative 恒完整。
- **build_session_report 兼容性**：新增 kwargs 均为可选默认 None，kol_analysis 与 campaign 旧调用方零改动。
- **旧报告**：`payload_json`/`template_version` 恒为 NULL，BI 走旧 Block 渲染并提示不支持模板导出；导出端点对其返回 404。
