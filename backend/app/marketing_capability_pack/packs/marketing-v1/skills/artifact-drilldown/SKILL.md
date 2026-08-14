---
name: artifact-drilldown
description: 对既有 Artifact Version 进行受控钻取，并仅在证据不足时创建新产物。
---

# 产物钻取

优先读取用户引用的 Artifact Version、相关章节和历史 Evidence。普通解释、定位来源或比较同一版本章节不自动发布新报告，也不调用 DataTap。钻取结论必须绑定用户指定的精确 Version，且引用该 Version 的 canonical data 与 Evidence。

仅当 freshness、用户范围或 Evidence 覆盖变化时，按需查询 DataTap。需要新的正式结论时调用 build_insight_draft，检查 Builder feedback 后才决定发布，并说明该结果是否 partial。
