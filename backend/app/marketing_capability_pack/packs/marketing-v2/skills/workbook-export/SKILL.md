---
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
