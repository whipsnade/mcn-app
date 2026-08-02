"""Task 18 受控 Excel 模板生成：brand_report_v3.xlsx / kol_selection_v3.xlsx。

渲染器（``app/agent_artifacts/exporters/*.py``）按固定行号填充这些模板；模板只
承载标题/表头/列宽/合并/数字格式，不携带样例数据行。评分维度权重从
``app.selection.scoring_v2.WEIGHTS_V2``（评分唯一真源）派生。

运行：cd backend && .venv/bin/python scripts/build_agent_artifact_templates.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.agent_artifacts.exporters.kol_selection import DIMENSION_LABELS  # noqa: E402
from app.agent_artifacts.payloads.kol_selection import SCORE_DIMENSIONS  # noqa: E402

OUT_DIR = BACKEND / "app" / "agent_artifacts" / "templates"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TITLE_FONT = Font(name="微软雅黑", bold=True, size=14, color="1F4E79")
SECTION_FONT = Font(name="微软雅黑", bold=True, size=11, color="1F4E79")
HEADER_FILL = PatternFill("solid", fgColor="D9E2F3")
HEADER_FONT = Font(name="微软雅黑", bold=True, size=10)
THIN = Side(style="thin", color="B0B0B0")
HEADER_BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center")


def new_workbook(name: str, widths: dict[str, float]) -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = name
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    ws.sheet_view.showGridLines = False
    return wb


def add_sheet(wb: Workbook, name: str, widths: dict[str, float]):
    ws = wb.create_sheet(name)
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    ws.sheet_view.showGridLines = False
    return ws


def title_row(ws, row: int, text: str, columns: int) -> None:
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=columns)
    cell = ws.cell(row, 1)
    cell.value = text
    cell.font = TITLE_FONT


def section_row(ws, row: int, text: str, columns: int) -> None:
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=columns)
    cell = ws.cell(row, 1)
    cell.value = text
    cell.font = SECTION_FONT


def header_row(ws, row: int, headers: list[str]) -> None:
    for column, text in enumerate(headers, start=1):
        cell = ws.cell(row, column)
        cell.value = text
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.border = HEADER_BORDER
        cell.alignment = CENTER


def build_brand_report_v3() -> Path:
    wb = new_workbook(
        "综合概览",
        {"A": 18, "B": 14, "C": 14, "D": 14, "E": 14, "F": 14},
    )
    ws = wb.active
    title_row(ws, 1, "", 6)
    ws.merge_cells("A2:F2")
    ws.merge_cells("A3:F3")
    section_row(ws, 4, "核心指标", 6)
    header_row(ws, 5, ["指标", "数值"])
    for offset, label in enumerate(["声量", "互动", "发帖", "情感分"]):
        ws.cell(6 + offset, 1).value = label

    ws = add_sheet(wb, "情感分析", {"A": 12, "B": 12, "C": 12, "D": 12, "E": 12, "F": 12, "G": 12})
    title_row(ws, 1, "情感分析", 7)

    ws = add_sheet(wb, "日趋势", {"A": 13, "B": 10, "C": 12, "D": 12, "E": 10, "F": 10, "G": 10})
    title_row(ws, 1, "每日声量与互动趋势", 7)
    header_row(ws, 3, ["日期", "平台", "声量", "互动", "正面", "中性", "负面"])

    ws = add_sheet(wb, "内容与达人", {"A": 12, "B": 10, "C": 12, "D": 12, "E": 12, "F": 12})
    title_row(ws, 1, "内容类型与达人层级", 6)

    ws = add_sheet(wb, "地域与话题", {"A": 14, "B": 12, "C": 12, "D": 12})
    title_row(ws, 1, "地域与话题分布", 4)

    ws = add_sheet(
        wb,
        "热门帖子TOP",
        {"A": 7, "B": 10, "C": 36, "D": 14, "E": 18, "F": 10, "G": 10, "H": 10, "I": 10, "J": 40},
    )
    header_row(ws, 3, ["排名", "平台", "标题", "作者", "发布时间", "点赞", "评论", "转发", "互动", "链接"])

    ws = add_sheet(wb, "洞察与建议", {"A": 18, "B": 40, "C": 30, "D": 30})
    title_row(ws, 1, "洞察与建议", 4)

    ws = add_sheet(wb, "方法论", {"A": 16, "B": 80})
    header_row(ws, 3, ["字段", "内容"])

    target = OUT_DIR / "brand_report_v3.xlsx"
    wb.save(target)
    return target


def build_kol_selection_v3() -> Path:
    wb = new_workbook(
        "KOL匹配度筛选",
        {
            "A": 6,
            "B": 10,
            "C": 22,
            "D": 12,
            "E": 12,
            "F": 12,
            "G": 12,
            "H": 12,
            "I": 12,
            "J": 12,
            "K": 12,
            "L": 12,
            "M": 12,
            "N": 12,
            "O": 11,
            "P": 12,
            "Q": 11,
            "R": 12,
        },
    )
    ws = wb.active
    title_row(ws, 1, "", 18)
    ws.merge_cells("A2:R2")
    header_row(
        ws,
        4,
        [
            "序号",
            "平台",
            "昵称",
            "粉丝数",
            "互动总数",
            "平均互动",
            *[DIMENSION_LABELS[dim] for dim in SCORE_DIMENSIONS],
            "综合总分",
            "评级",
            "星级",
            "数据完整度",
        ],
    )

    ws = add_sheet(wb, "评分说明", {"A": 22, "B": 10, "C": 46, "D": 46})
    title_row(ws, 1, "评分方法论与数据来源", 4)
    section_row(ws, 3, "一、评分维度与权重", 4)
    header_row(ws, 4, ["维度", "权重", "缺失处理", "说明"])
    from app.selection.scoring_v2 import WEIGHTS_V2  # noqa: E402

    for offset, dimension in enumerate(SCORE_DIMENSIONS):
        row = 5 + offset
        ws.cell(row, 1).value = DIMENSION_LABELS[dimension]
        ws.cell(row, 2).value = WEIGHTS_V2[dimension]
        ws.cell(row, 3).value = "缺失或无法匹配记 0 分，不重分配权重"
    section_row(ws, 14, "二、评级映射", 4)
    header_row(ws, 15, ["评级", "星级", "分数区间", "说明"])
    ratings = [
        ("重点推荐", "★★★★★", "≥78", "优先合作，匹配度极高"),
        ("推荐", "★★★★", "62-77", "建议合作，匹配度良好"),
        ("可考虑", "★★★", "48-61", "可考虑合作，需关注短板"),
        ("观察", "★★", "<48", "匹配度偏低，保持观察"),
    ]
    for offset, values in enumerate(ratings):
        for column, value in enumerate(values, start=1):
            ws.cell(16 + offset, column).value = value
    section_row(ws, 21, "三、数据完整度", 4)
    cell = ws.cell(22, 1)
    cell.value = (
        "data_completeness 表示八个评分维度中实际有数据支撑的比例（0-100%）；"
        "缺失维度记 0 分，但完整度如实披露，不重分配权重。"
    )
    cell.alignment = Alignment(wrap_text=True, vertical="top")

    ws = add_sheet(wb, "圈选结论", {"A": 16, "B": 60, "C": 40, "D": 40})
    title_row(ws, 1, "圈选结论与建议", 4)

    target = OUT_DIR / "kol_selection_v3.xlsx"
    wb.save(target)
    return target


if __name__ == "__main__":
    print(build_brand_report_v3())
    print(build_kol_selection_v3())
