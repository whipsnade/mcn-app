"""kol_selection_v3 Excel 导出渲染器（设计 §12.1 消费边界 / Task 18）。

按 §12.1：Excel 每行展示八个 ``score_snapshot.dimensions.*.raw_score`` 列以及
总分/评级/星级/数据完整度，不展示 ``weighted_score`` 列（原始分单独列示，
加权分不参与展示）。渲染器只读已发布 Version 的 payload，不调用模型/MCP。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font

from app.agent_artifacts.exporters._common import (
    MISSING,
    cell_value,
    clear_rows,
    platform_label,
    present,
    write_table,
    write_value,
)
from app.agent_artifacts.payloads.kol_selection import (
    SCORE_DIMENSIONS,
    KolSelectionV3,
)

TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1] / "templates" / "kol_selection_v3.xlsx"
)
SUMMARY_SHEET = "KOL匹配度筛选"
NARRATIVE_SHEET = "圈选结论"

# 与模板表头顺序一一对应（§12.1：八个 raw_score 列 + 总分/评级/星级/完整度）。
DIMENSION_LABELS = {
    "industry_interest": "行业兴趣",
    "target_region": "目标地区",
    "target_age": "目标年龄",
    "engagement": "互动表现",
    "active_follower": "活跃粉丝",
    "content": "内容质量",
    "followers": "粉丝规模",
    "engagement_follower_ratio": "互动粉丝比",
}


def render_kol_selection_workbook(payload: dict) -> bytes:
    """把已发布 kol_selection_v3 payload 渲染为 .xlsx bytes（同步 CPU 密集）。"""
    selection = KolSelectionV3.model_validate(payload)
    workbook = load_workbook(TEMPLATE_PATH)
    _render_summary(workbook[SUMMARY_SHEET], selection)
    _render_narrative(workbook[NARRATIVE_SHEET], selection)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _render_summary(sheet, selection) -> None:
    scope = selection.scope
    brand = scope.brand or "KOL"
    sheet["A1"] = cell_value(f"{brand} KOL 圈选名单")
    platforms = "、".join(platform_label(name) for name in scope.platforms) or "未指定"
    summary = selection.data.summary
    sheet["A2"] = (
        f"平台: {platforms} | 候选: {present(summary.candidate_count)} | "
        f"圈选: {present(summary.selected_count)} | 评分: {selection.data.scoring.version}"
    )
    clear_rows(sheet, 5, 100, 18)
    for index, item in enumerate(selection.data.items):
        row = 5 + index
        snapshot = item.score_snapshot
        values = [
            item.rank,
            platform_label(item.platform),
            item.nickname,
            item.followers,
            item.engagement_total,
            item.avg_engagement,
        ]
        # §12.1：八个维度的原始分（raw_score），绝不使用 weighted_score 顶替。
        for dimension in SCORE_DIMENSIONS:
            values.append(snapshot.dimensions[dimension].raw_score)
        values.extend(
            [snapshot.total, snapshot.rating, snapshot.stars, snapshot.data_completeness]
        )
        for column, value in enumerate(values, start=1):
            write_value(sheet.cell(row, column), value)


def _render_narrative(sheet, selection) -> None:
    clear_rows(sheet, 3, 100, 4)
    narrative = selection.narrative
    row = _write_paragraph(sheet, 3, "圈选摘要", narrative.selection_summary)
    row = write_table(
        sheet,
        row + 1,
        "匹配结论",
        ["关联达人", "内容"],
        [[note.kol_uid or "-", note.text] for note in narrative.fit_findings],
        columns=4,
        note=MISSING,
    )
    row = write_table(
        sheet,
        row + 1,
        "风险提示",
        ["关联达人", "内容"],
        [[note.kol_uid or "-", note.text] for note in narrative.risk_notes],
        columns=4,
        note=MISSING,
    )
    write_table(
        sheet,
        row + 1,
        "使用建议",
        ["关联达人", "内容"],
        [[note.kol_uid or "-", note.text] for note in narrative.usage_advice],
        columns=4,
        note=MISSING,
    )


def _write_paragraph(sheet, start_row: int, title: str, text: str) -> int:
    sheet.merge_cells(start_row=start_row, start_column=1, end_row=start_row, end_column=4)
    title_cell = sheet.cell(start_row, 1)
    title_cell.value = title
    title_cell.font = Font(name="微软雅黑", bold=True, size=11, color="1F4E79")
    row = start_row + 1
    sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
    text_cell = sheet.cell(row, 1)
    text_cell.value = cell_value(text)
    text_cell.alignment = Alignment(wrap_text=True, vertical="top")
    return row + 1


__all__ = [
    "DIMENSION_LABELS",
    "NARRATIVE_SHEET",
    "SUMMARY_SHEET",
    "TEMPLATE_PATH",
    "render_kol_selection_workbook",
]
