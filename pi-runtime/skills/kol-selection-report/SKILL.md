---
name: kol-selection-report
description: 基于可追溯 Evidence 生成跨平台达人圈选与分析 Artifact。
---

# 达人圈选与分析

适用于达人筛选、候选名单、匹配度、效果和价格效率比较。确认品牌或品类、目标受众、平台、
日期窗口、预算和筛选条件；条件不足以决定候选范围时请求澄清。

收集每位候选人的 Evidence 并保留来源与缺失字段。评分由确定性 Builder 完成：严格缺失即
零，跨平台输出 Top20，效果与匹配度权重为 70，价格效率权重为 30；不得在模型中手工计算或
补写得分。数据有限时允许 partial，但禁止编造报价、受众、商业合作或互动指标。

调用 `build_kol_selection_draft`，需要组合解读时使用 `build_kol_analysis_draft`。检查 Builder feedback
的 availability、coverage、limitations 和 Evidence lineage 后决定是否补查。完成条件
是发布同一 Artifact Version，并由该 Version 渲染 BI 与 Excel；未完成时明确限制。
