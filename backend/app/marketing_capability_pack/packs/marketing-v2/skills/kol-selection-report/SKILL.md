---
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
