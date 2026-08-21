---
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
