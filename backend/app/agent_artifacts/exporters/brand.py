"""brand_report_v3 Excel 导出渲染器（设计 §12.1 消费边界 / Task 18）。

渲染器只读已发布不可变 Version 的 payload（校验为强类型 ``BrandReportV3``），
按受控模板 ``templates/brand_report_v3.xlsx`` 填充各章节。空章节保留列头并写
「数据受限/未采集」说明，绝不因空数据画误导性图表；受限章节通过
``availability``/``limitations`` 披露。任何异常向上抛（Task 19 路由映射），
绝不输出半截文件。
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openpyxl import load_workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Font

from app.agent_artifacts.exporters._common import (
    MISSING,
    cell_value,
    clear_rows,
    empty_note,
    platform_label,
    write_value,
    write_table,
)
from app.agent_artifacts.payloads.brand import BrandReportV3

TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1] / "templates" / "brand_report_v3.xlsx"
)
SHEET_ORDER = (
    "综合概览",
    "情感分析",
    "日趋势",
    "内容与达人",
    "地域与话题",
    "热门帖子TOP",
    "洞察与建议",
    "方法论",
)


def render_brand_workbook(payload: dict) -> bytes:
    """把已发布 brand_report_v3 payload 渲染为 .xlsx bytes（同步 CPU 密集）。"""
    report = BrandReportV3.model_validate(payload)
    workbook = load_workbook(TEMPLATE_PATH)
    _render_overview(workbook["综合概览"], report)
    _render_sentiment(workbook["情感分析"], report)
    _render_daily_trend(workbook["日趋势"], report)
    _render_content_creators(workbook["内容与达人"], report)
    _render_regions_topics(workbook["地域与话题"], report)
    _render_top_posts(workbook["热门帖子TOP"], report)
    _render_insights(workbook["洞察与建议"], report)
    _render_methodology(workbook["方法论"], report)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


# ---------- 各 Sheet 渲染 ----------


def _render_overview(sheet, report) -> None:
    scope = report.scope
    sheet["A1"] = cell_value(f"{scope.brand} 品牌社交媒体表现分析报告")
    sheet["A2"] = (
        f"{scope.period.start} 至 {scope.period.end}（数据截至 {report.methodology.data_as_of:%Y-%m-%d}）"
    )
    platforms = "、".join(platform_label(name) for name in scope.platforms) or MISSING
    sheet["A3"] = f"平台：{platforms}；关键词：{'、'.join(scope.keywords) or MISSING}"

    overview = report.data.overview
    metrics = (
        ("声量", overview.total_volume),
        ("互动", overview.total_engagement),
        ("发帖", overview.total_posts),
        ("情感分", overview.sentiment_score),
    )
    for offset, (label, value) in enumerate(metrics):
        sheet.cell(6 + offset, 1).value = label
        write_value(sheet.cell(6 + offset, 2), value)

    clear_rows(sheet, 11, 60, 6)
    row = write_table(
        sheet,
        11,
        "平台表现",
        ["平台", "声量", "互动", "发帖", "声量占比", "情感分"],
        [
            [
                platform_label(item.platform),
                item.volume,
                item.engagement,
                item.posts,
                item.share_of_voice,
                item.sentiment_score,
            ]
            for item in overview.platforms
        ],
        columns=6,
        pct_columns=(5,),
    )
    # 对比分析：mom/yoy 指标按名称合并，同指标并列环比/同比变化率。
    metrics_by_name: dict[str, dict[str, Any]] = {}
    for comparison, label in (
        (report.data.comparisons.mom, "mom"),
        (report.data.comparisons.yoy, "yoy"),
    ):
        for metric in comparison.metrics:
            entry = metrics_by_name.setdefault(metric.metric, {"current": metric.current})
            entry[f"{label}_rate"] = metric.rate
    comparison_rows = [
        [name, entry["current"], entry.get("mom_rate"), entry.get("yoy_rate")]
        for name, entry in metrics_by_name.items()
    ]
    write_table(
        sheet,
        row + 1,
        "对比分析",
        ["指标", "当前值", "环比变化率", "同比变化率"],
        comparison_rows,
        columns=6,
        pct_columns=(3, 4),
    )


def _render_sentiment(sheet, report) -> None:
    clear_rows(sheet, 3, 60, 7)
    summary = report.data.sentiment.summary
    buckets = (
        ("正面", summary.positive),
        ("中性", summary.neutral),
        ("负面", summary.negative),
    )
    row = write_table(
        sheet,
        3,
        None,
        ["情感", "计数", "占比"],
        [[label, bucket.count, bucket.share] for label, bucket in buckets],
        columns=7,
        pct_columns=(3,),
    )
    by_platform_rows = []
    for item in report.data.sentiment.by_platform:
        by_platform_rows.append(
            [
                platform_label(item.platform),
                item.positive.count,
                item.positive.share,
                item.neutral.count,
                item.neutral.share,
                item.negative.count,
                item.negative.share,
            ]
        )
    write_table(
        sheet,
        row + 1,
        "平台情感分布",
        ["平台", "正面计数", "正面占比", "中性计数", "中性占比", "负面计数", "负面占比"],
        by_platform_rows,
        columns=7,
        pct_columns=(3, 5, 7),
    )


def _render_daily_trend(sheet, report) -> None:
    points = report.data.daily_trend
    clear_rows(sheet, 4, 100, 7)
    sheet._charts = []
    if not points:
        # 空章节：保留列头，写受限/未采集说明，不建误导性图表。
        sheet.merge_cells("A4:G4")
        sheet["A4"] = empty_note(report, "daily_trend")
        return
    for index, point in enumerate(points):
        row = 4 + index
        sheet.cell(row, 1).value = point.date
        sheet.cell(row, 2).value = platform_label(point.platform)
        sheet.cell(row, 3).value = point.volume
        sheet.cell(row, 4).value = point.engagement
        sheet.cell(row, 5).value = point.positive
        sheet.cell(row, 6).value = point.neutral
        sheet.cell(row, 7).value = point.negative
    last_data_row = 3 + len(points)
    chart = LineChart()
    chart.title = "每日声量与互动趋势"
    chart.add_data(
        Reference(sheet, min_col=3, min_row=3, max_row=last_data_row), titles_from_data=True
    )
    chart.add_data(
        Reference(sheet, min_col=4, min_row=3, max_row=last_data_row), titles_from_data=True
    )
    chart.set_categories(Reference(sheet, min_col=1, min_row=4, max_row=last_data_row))
    chart.height = 8
    chart.width = 16
    sheet.add_chart(chart, f"A{last_data_row + 2}")


def _render_content_creators(sheet, report) -> None:
    clear_rows(sheet, 3, 80, 6)
    data = report.data
    row = write_table(
        sheet,
        3,
        "内容类型",
        ["平台", "类型", "发帖", "声量", "互动"],
        [
            [platform_label(item.platform), item.type, item.posts, item.volume, item.engagement]
            for item in data.content_types
        ],
        columns=6,
        note=empty_note(report, "content_types"),
    )
    row = write_table(
        sheet,
        row + 1,
        "达人层级分布",
        ["平台", "层级", "达人数量", "发帖", "声量", "互动"],
        [
            [
                platform_label(item.platform),
                item.tier,
                item.creator_count,
                item.posts,
                item.volume,
                item.engagement,
            ]
            for item in data.creator_tiers
        ],
        columns=6,
        note=empty_note(report, "creator_tiers"),
    )
    write_table(
        sheet,
        row + 1,
        "商单 vs 自然内容",
        ["平台", "类型", "发帖", "声量", "互动"],
        [
            [platform_label(item.platform), item.kind, item.posts, item.volume, item.engagement]
            for item in data.organic_vs_paid
        ],
        columns=6,
        note=empty_note(report, "organic_vs_paid"),
    )


def _render_regions_topics(sheet, report) -> None:
    clear_rows(sheet, 3, 80, 4)
    sheet._charts = []
    regions = report.data.regions
    row = write_table(
        sheet,
        3,
        "地域分布",
        ["地域", "声量", "占比", "情感分"],
        [
            [item.region, item.volume, item.share, item.sentiment_score]
            for item in regions
        ],
        columns=4,
        pct_columns=(3,),
        note=empty_note(report, "regions"),
    )
    if regions:
        chart = BarChart()
        chart.type = "col"
        chart.title = "发帖用户地域声量分布"
        chart.add_data(
            Reference(sheet, min_col=2, min_row=3, max_row=3 + len(regions)),
            titles_from_data=True,
        )
        chart.set_categories(Reference(sheet, min_col=1, min_row=4, max_row=3 + len(regions)))
        chart.height = 8
        chart.width = 16
        sheet.add_chart(chart, f"A{3 + len(regions) + 2}")
    write_table(
        sheet,
        row + 1,
        "话题分布",
        ["话题", "声量", "互动", "情感分"],
        [
            [item.topic, item.volume, item.engagement, item.sentiment_score]
            for item in report.data.topics
        ],
        columns=4,
        note=empty_note(report, "topics"),
    )


def _render_top_posts(sheet, report) -> None:
    clear_rows(sheet, 4, 60, 10)
    posts = report.data.top_posts
    if not posts:
        sheet.merge_cells("A4:J4")
        sheet["A4"] = empty_note(report, "top_posts")
        return
    headers = ("排名", "平台", "标题", "作者", "发布时间", "点赞", "评论", "转发", "互动", "链接")
    for column, header in enumerate(headers, start=1):
        sheet.cell(3, column).value = header
    for index, post in enumerate(posts):
        row = 4 + index
        values = (
            index + 1,
            platform_label(post.platform),
            post.title,
            post.author,
            _naive_datetime(post.published_at),
            post.likes,
            post.comments,
            post.shares,
            post.engagement,
        )
        for column, value in enumerate(values, start=1):
            write_value(sheet.cell(row, column), value)
        url_cell = sheet.cell(row, 10)
        if post.url and _is_valid_url(post.url):
            url_cell.value = post.url
            url_cell.hyperlink = post.url
            url_cell.font = Font(color="0563C1", underline="single")
        else:
            url_cell.value = MISSING


def _render_insights(sheet, report) -> None:
    clear_rows(sheet, 3, 80, 4)
    narrative = report.narrative
    row = _write_paragraph(sheet, 3, "执行摘要", narrative.executive_summary)
    row = write_table(
        sheet,
        row + 1,
        "关键发现",
        ["标题", "详情", "支撑路径"],
        [
            [finding.title, finding.detail, _paths_text(finding.supporting_paths)]
            for finding in narrative.findings
        ],
        columns=4,
        note=MISSING,
    )
    write_table(
        sheet,
        row + 1,
        "行动建议",
        ["标题", "行动", "理由", "支撑路径"],
        [
            [
                recommendation.title,
                recommendation.action,
                recommendation.rationale,
                _paths_text(recommendation.supporting_paths),
            ]
            for recommendation in narrative.recommendations
        ],
        columns=4,
        note=MISSING,
    )


def _render_methodology(sheet, report) -> None:
    clear_rows(sheet, 4, 60, 2)
    scope = report.scope
    methodology = report.methodology
    limited = [
        f"{label}：{entry.status}"
        for label, entry in sorted(report.availability.items())
        if entry.status != "complete"
    ]
    rows = (
        ("分析品牌", scope.brand or MISSING),
        ("时间范围", f"{scope.period.start} 至 {scope.period.end}"),
        ("平台", "、".join(platform_label(name) for name in scope.platforms) or MISSING),
        ("关键词", "、".join(scope.keywords) or MISSING),
        ("对比口径", scope.comparison_mode),
        ("数据来源", "、".join(methodology.source_names) or MISSING),
        ("数据截至", f"{methodology.data_as_of:%Y-%m-%d %H:%M}"),
        ("章节可用性", "；".join(limited) if limited else "全部章节数据完整"),
        ("局限性", _limitations_text(report)),
    )
    for index, (label, text) in enumerate(rows):
        sheet.cell(4 + index, 1).value = label
        write_value(sheet.cell(4 + index, 2), text)


# ---------- 取值辅助 ----------


def _naive_datetime(value):
    """Excel 不支持带时区的 datetime：去掉 tzinfo 再写单元格。"""
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _paths_text(paths) -> str:
    return "、".join(paths) or MISSING


def _limitations_text(report) -> str:
    if not report.limitations:
        return "无"
    return "；".join(f"{item.code}：{item.message}" for item in report.limitations)


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


def _is_valid_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


__all__ = [
    "SHEET_ORDER",
    "TEMPLATE_PATH",
    "render_brand_workbook",
]
