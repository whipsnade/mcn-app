---
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
