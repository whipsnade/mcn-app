"""0048 add the audited marketing-v2 baseline as immutable revisions.

0045 created the registry and its first placeholder rows.  This additive data
migration keeps those rows immutable, adds the reviewed revision=2 bodies and
tools, then moves the production pointers while retaining the old rows as the
explicit previous revision.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa


revision: str = "0048_marketing_skill_audited_baseline"
down_revision: str | None = "0047_marketing_skill_scope_uniqueness"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_GLOBAL_SCOPE_KEY = "__global__"

_BASELINE_SKILLS = (
    (
        "social-marketing-analyst",
        "处理社媒营销研究、报告、钻取与策略咨询，并按需加载专项 Skill。",
        "marketing_root_v1",
        ("load_marketing_skill", "request_clarification", "publish_artifacts"),
        """---
name: social-marketing-analyst
description: 处理社媒营销研究、报告、钻取与策略咨询，并按需加载专项 Skill。
---

# 社媒营销分析总则

仅处理品牌、活动、达人和社媒营销语境的问题。遇到非营销主题，按根策略固定
回复后结束。根据用户目标按需加载品牌报告、活动评估、达人圈选、产物钻取或
策略咨询专项 Skill。

完整报告需要明确对象、日期窗口、平台及问题；信息不足时调用
request_clarification 提出一个最关键的问题，此后不得继续查询或发布。

- 任何正式输出都必须经 build_artifact_draft 按 load_marketing_skill 返回的
  model_input_contract 构造输入，再经 publish_artifacts 发布；不能手写正式
  payload、Excel 或 BI。
- 每次正式产物调用前先加载对应 Skill 获取 model_input_schema 与
  concise_example；校验失败按结构化字段级错误修正。
- DataTap 错误、空结果和超时如实保留，不以编造数据或替代口径冒充原始结果。
""",
    ),
    (
        "brand-research-report",
        "按 load_marketing_skill 的 model_input_contract 生成同版本品牌社媒研究 Artifact、BI 与 Excel。",
        "brand_report_v3",
        ("build_artifact_draft", "publish_artifacts"),
        """---
name: brand-research-report
description: 按 load_marketing_skill 的 model_input_contract 生成同版本品牌社媒研究 Artifact、BI 与 Excel。
---

# 品牌社媒研究报告

确认品牌实体、日期窗口、平台、关键词和对比口径；决定性条件不足时调用
request_clarification。MCP 标准 Tool Result 直接由你消费分析，不需要写入任何
中间证据库，也不存在独立的 Evidence 检索步骤。

1. 先调用 load_marketing_skill 获取本 Skill 的 model_input_contract（含
   model_input_schema 与 concise_example）。
2. 按 model_input_schema 构造 build_artifact_draft 的 payload：只提交
   scope/data/narrative/availability/limitations/methodology_input；服务器负责
   补齐 schema_version/module/data_status/canonical_data/field_lineage，不要
   提交这些字段。
3. 校验失败时按返回的结构化字段级错误（path/type/reason/retryable）逐条修正
   后重试，不要猜测 Schema。
4. 经 publish_artifacts 发布后，Artifact Version 与 BI、Excel 绑定同一 Version。
5. 数据不足时把对应章节 availability 标为 partial/unavailable 并给出覆盖
   limitations，不得编造数据，也不得把缺失当零。
""",
    ),
    (
        "campaign-evaluation-report",
        "按 load_marketing_skill 的 model_input_contract 生成活动效果评估 Artifact，并保持 BI 和 Excel 同版本。",
        "campaign_report_v3",
        ("build_artifact_draft", "publish_artifacts"),
        """---
name: campaign-evaluation-report
description: 按 load_marketing_skill 的 model_input_contract 生成活动效果评估 Artifact，并保持 BI 和 Excel 同版本。
---

# 活动效果评估

确认活动身份、日期窗口、平台、比较范围和业务问题；缺少决定性范围时先澄清。
MCP 标准 Tool Result 直接由你分析，不需要写入任何中间证据库。

1. 先调用 load_marketing_skill 获取 model_input_contract（model_input_schema +
   concise_example）。
2. 按 schema 构造 build_artifact_draft 的 payload：只提交业务字段
   （scope/data/narrative/availability/limitations/methodology_input）；服务器
   补齐 schema_version/module/data_status/canonical_data/field_lineage。
3. 校验失败按结构化字段级错误（path/type/reason）修正后重试。
4. 经 publish_artifacts 发布后 BI 与 Excel 均指向该 Version。
5. 禁止编造活动效果、付费归属或未返回的因果结论；数据缺失以 partial/
   unavailable + limitation 表达。
""",
    ),
    (
        "kol-selection-report",
        "按 load_marketing_skill 的 model_input_contract 生成跨平台达人圈选与分析 Artifact。",
        "kol_selection_v3",
        ("build_artifact_draft", "publish_artifacts"),
        """---
name: kol-selection-report
description: 按 load_marketing_skill 的 model_input_contract 生成跨平台达人圈选与分析 Artifact。
---

# 达人圈选与分析

确认品牌或品类、目标受众、平台、日期窗口、预算和筛选条件；条件不足以决定
候选范围时请求澄清。MCP 标准 Tool Result（含平台/达人/报价/互动字段）直接由你
分析，不需要写入任何中间证据库。

1. 先调用 load_marketing_skill 获取 model_input_contract（model_input_schema +
   concise_example）。
2. 按 schema 构造 build_artifact_draft 的 payload：只提交业务字段
   （scope/data/narrative/availability/limitations/methodology_input）；服务器
   补齐 schema_version/module/data_status/canonical_data/field_lineage。
3. 校验失败按结构化字段级错误（path/type/reason）修正后重试。
4. 经 publish_artifacts 发布后 BI 与 Excel 指向同一 Artifact Version。
5. 不得把空对象变成候选、不得在模型侧手工补写评分或伪造报价；缺失指标如实以
   partial/unavailable + limitation 表达。
""",
    ),
    (
        "artifact-drilldown",
        "对同 Session 已发布父 Artifact Version 做受控钻取，并按模型输入契约产出看板。",
        "insight_board_v1",
        ("read_artifact", "build_artifact_draft", "publish_artifacts"),
        """---
name: artifact-drilldown
description: 对同 Session 已发布父 Artifact Version 做受控钻取，并按模型输入契约产出看板。
---

# 产物钻取

先读取用户引用的 Artifact Version（read_artifact）。普通解释、定位来源或比较
同一版本章节不自动发布新报告，也不调用 DataTap。钻取看板必须绑定同 Session
已发布的父 Artifact Version（parent_artifact_id / parent_artifact_version_id），
且只引用该 Version 的最终业务数据。

1. 先调用 load_marketing_skill 获取 model_input_contract（model_input_schema +
   concise_example）。
2. 按 schema 构造 build_artifact_draft 的 payload：只提交业务字段（title/scope/
   parent_artifact_id/parent_artifact_version_id/narrative/blocks/availability/
   limitations/methodology_input）；服务器补齐 schema_version/module/data_status，
   模型不得提交这些字段。
3. 校验失败按结构化字段级错误（path/type/reason）修正后重试。
4. 经 publish_artifacts 发布后 BI 与 Excel 指向该 Version。
5. 数据不足以 partial/unavailable + limitation 表达，不得编造数值。
""",
    ),
    (
        "marketing-strategy",
        "基于真实 MCP Tool Result 与已发布 Artifact 提供受限、可说明依据的策略咨询。",
        "strategy_advice_v1",
        ("read_artifact", "request_clarification"),
        """---
name: marketing-strategy
description: 基于真实 MCP Tool Result 与已发布 Artifact 提供受限、可说明依据的策略咨询。
---

# 营销策略咨询

先判断用户要的是既有结果解释、正式研究、钻取还是开放式策略建议。策略建议
优先引用已发布 Artifact Version 与真实 MCP Tool Result；范围、时效或覆盖不足时
调用 request_clarification 澄清关键条件，必要时再查询 DataTap。

- 建议必须区分事实、证据支持的推断和待验证假设，不把缺失数据包装成结论。
- 策略咨询可纯文字回复：不要自动创建报告；只有用户需要可发布正式产物时才
  加载对应专项 Skill，按其 model_input_contract 经 build_artifact_draft +
  publish_artifacts 发布。
- 数据受限时明确说明，禁止编造市场规模、预算、投放效果或用户偏好。
""",
    ),
    (
        "analysis-report",
        "为品牌、活动、达人或混合营销请求生成类型化通用分析报告。",
        "analysis_report_v1",
        ("load_marketing_skill", "build_artifact_draft", "publish_artifacts"),
        """---
name: analysis-report
description: 为品牌、活动、达人或混合营销请求生成类型化通用分析报告。
---

# 通用营销分析报告

当用户要求自定义字段、跨平台统一表头、长尾数量或混合业务组合时，使用
`analysis_report_v1`。先按用户目标自主决定需要的查询、分页、停止条件和输出
范围；不把固定业务流程或固定数量写入报告。

调用 `load_marketing_skill` 获取 `model_input_contract` 后，只向
`build_artifact_draft` 提交业务字段：`title`、`subject_type`、`scope`、`blocks`、
`fulfillment`、`availability`、`limitations`、`methodology_input` 和可选
`workbook`。服务器负责补齐 `schema_version`、`module`、`data_status` 与 Artifact
身份。Block 必须使用类型化列和安全的 http/https 链接，不提交公式、宏、脚本或
文件路径。

`fulfillment` 必须保留真实的 `requested_min`、`actual_count`、`status` 和
`reason`。数据不足时保留已取得结果，使用 partial/unavailable 与 limitation
披露，不把缺失值变成零，也不为了达到用户数量而编造记录。发布后再按需调用
`publish_artifacts`；BI 与 Excel 由同一不可变 Report Version 生成。
""",
    ),
    (
        "workbook-export",
        "根据同一通用 Report Version 选择安全、可复现的 Excel 布局投影。",
        "workbook_v1",
        ("load_marketing_skill", "read_artifact", "publish_artifacts"),
        """---
name: workbook-export
description: 根据同一通用 Report Version 选择安全、可复现的 Excel 布局投影。
---

# 通用 Excel 投影

只有用户明确需要 Excel 时才选择 `workbook_v1` 布局。它只引用当前 Run 已发布的
`analysis_report_v1` Version 与 Block ID，不复制业务事实，也不跨 Version 读取内容。
可以声明 Sheet 顺序、列顺序、显示名、冻结表头、筛选、排序、超链接和分页意图；
不要提交公式、宏、脚本、二进制文件或服务器路径。

数量超过单 Sheet 的技术能力时应分页或拆 Sheet，不能静默丢行。导出失败要披露
技术限制并保留 Report Version；同一 Version、Exporter 版本和布局应得到确定性
且可复现的工作簿。
""",
    ),
)


def _rows() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    now = datetime.now(UTC).replace(tzinfo=None)
    revisions: list[dict[str, object]] = []
    activations: list[dict[str, object]] = []
    for index, (name, description, artifact_contract, required_tools, body) in enumerate(
        _BASELINE_SKILLS, start=1
    ):
        content = body if body.endswith("\n") else f"{body}\n"
        revision_id = f"00000000-0048-4000-8000-{index:012d}"
        old_revision_id = f"00000000-0045-4000-8000-{index:012d}"
        revisions.append(
            {
                "id": revision_id,
                "tenant_id": None,
                "scope_key": _GLOBAL_SCOPE_KEY,
                "skill_name": name,
                "revision": 2,
                "content": content,
                "content_digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "description": description,
                "required_tools": list(required_tools),
                "artifact_contract": artifact_contract,
                "created_by": None,
                "created_at": now,
                "change_note": "marketing-v2 audited baseline; supersedes 0045 placeholder",
            }
        )
        activations.append(
            {
                "skill_name": name,
                "active_revision_id": revision_id,
                "previous_revision_id": old_revision_id,
                "updated_at": now,
            }
        )
    return revisions, activations


def upgrade() -> None:
    revision_rows, activation_rows = _rows()
    revision_table = sa.table(
        "skill_revisions",
        sa.column("id", sa.String(36)),
        sa.column("tenant_id", sa.String(36)),
        sa.column("scope_key", sa.String(36)),
        sa.column("skill_name", sa.String(96)),
        sa.column("revision", sa.Integer),
        sa.column("content", sa.Text),
        sa.column("content_digest", sa.String(64)),
        sa.column("description", sa.String(512)),
        sa.column("required_tools", sa.JSON),
        sa.column("artifact_contract", sa.String(96)),
        sa.column("created_by", sa.String(36)),
        sa.column("created_at", sa.DateTime),
        sa.column("change_note", sa.String(512)),
    )
    op.bulk_insert(revision_table, revision_rows)
    activation_table = sa.table(
        "skill_activations",
        sa.column("environment", sa.String(24)),
        sa.column("scope_key", sa.String(36)),
        sa.column("skill_name", sa.String(96)),
        sa.column("active_revision_id", sa.String(36)),
        sa.column("previous_revision_id", sa.String(36)),
        sa.column("updated_at", sa.DateTime),
    )
    for row in activation_rows:
        op.execute(
            activation_table.update()
            .where(
                (activation_table.c.environment == "production")
                & (activation_table.c.scope_key == _GLOBAL_SCOPE_KEY)
                & (activation_table.c.skill_name == row["skill_name"])
            )
            .values(
                active_revision_id=row["active_revision_id"],
                previous_revision_id=row["previous_revision_id"],
                updated_at=row["updated_at"],
            )
        )


def downgrade() -> None:
    # Keep immutable audited revisions and the pointer history.  Removing them
    # would destroy the only safe rollback target and could reintroduce the
    # unreviewed placeholder pack.
    pass
