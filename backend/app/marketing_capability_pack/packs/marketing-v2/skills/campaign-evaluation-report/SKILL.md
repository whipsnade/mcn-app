---
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
