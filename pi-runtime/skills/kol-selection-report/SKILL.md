---
name: kol-selection-report
description: 基于可追溯 Evidence 生成跨平台达人圈选与分析 Artifact。
---

# 达人圈选与分析

适用于达人筛选、候选名单、匹配度、效果和价格效率比较。先理解品牌或品类、目标受众、平台、
日期窗口、预算和筛选条件；条件不足以决定候选范围时请求澄清，不擅自补齐预算或受众。

模型自主决策收集每位候选人的 Evidence，并保留来源、缺失字段和平台身份。使用当前 Run
Snapshot 允许的 Tool Contract 与确定性计算能力完成规范化、排序和比较；不得在自然语言中
手工计算或补写得分。行数、排序维度和权重由用户问题、真实返回数据及当前契约共同决定；不
预设输出规模或业务权重。数据有限时允许 partial，禁止编造报价、受众、商业合作或互动指标。

创建或更新 Draft 后检查结构化校验反馈、availability、coverage、limitations 和 Evidence lineage，
由模型决定补查、澄清、发布或完成。完成条件是 Artifact Version 通过强类型与来源校验，并由
同一 Version 渲染 BI 与 Excel；长尾圈选可使用 `analysis_report_v1` 和 `workbook_v1`，未完成时
明确限制。

生产 Native 路径只依赖 Run Snapshot 注入的 Skill、Tool Contract 和 Root Policy，不要求固定
工具顺序，不手写正式 payload 或 Excel。
