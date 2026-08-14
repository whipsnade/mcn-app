---
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
