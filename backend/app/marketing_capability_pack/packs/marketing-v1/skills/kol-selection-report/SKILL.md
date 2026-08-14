---
name: kol-selection-report
description: 基于可追溯 Evidence 生成跨平台达人圈选与分析 Artifact。
---

# 达人圈选与分析

确认品牌或品类、目标受众、平台、日期窗口、预算和筛选条件；条件不足以决定候选范围时请求澄清。收集每位候选人的 Evidence 并保留来源与缺失字段。

评分由确定性 Builder 完成：严格缺失指标为零，不能把空对象变成候选，也不得在模型中手工计算或补写得分。调用 build_kol_selection_draft，阅读 availability、coverage、limitations 和 Evidence lineage；发布同一 Artifact Version 后才可渲染 BI 和 Excel。
