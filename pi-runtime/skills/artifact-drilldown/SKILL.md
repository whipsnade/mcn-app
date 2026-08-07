---
name: artifact-drilldown
description: 对既有 Artifact Version 进行受控钻取，并仅在证据不足时创建新产物。
---

# 产物钻取

优先读取用户引用的 Artifact Version、相关章节和历史 Evidence，确认问题能否由既有版本回答。
普通解释、定位来源或比较同一版本的章节不自动发布新报告。

仅当 freshness、用户范围或证据覆盖发生变化时，才按需查询 DataTap。所有新数值须有 Evidence
lineage；空结果和失败保持可见，禁止编造。需要新的可视化或正式钻取结论时使用
`build_insight_draft`，检查 Builder feedback 后才决定发布，并说明该结果是否为 partial。
