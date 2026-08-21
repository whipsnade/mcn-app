---
name: artifact-drilldown
description: 对既有 Artifact Version 进行受控钻取，并仅在证据不足时创建新产物。
---

# 产物钻取

模型自主决策优先读取用户引用的 Artifact Version、相关章节和历史 Evidence，确认问题能否由
既有版本回答。普通解释、定位来源或比较同一版本的章节不自动创建新产物；只在 freshness、
用户范围或证据覆盖变化时按需查询 DataTap。

所有新数值须有 Evidence lineage；空结果、失败和 restricted/partial 状态保持可见，禁止编造。
需要新的可视化或正式钻取结论时，按当前 Run Snapshot 的 Tool Contract 创建合适的 Artifact
Draft，阅读结构化校验反馈后由模型决定复核、发布或完成。简单钻取可使用 `insight_board_v1`；
跨平台长尾结论可使用 `analysis_report_v1`，需要文件时由同一 Version 生成 `workbook_v1`。

生产 Native 路径只依赖 Snapshot 注入的 Skill、Tool Contract 和 Root Policy，不要求旧 POC
内部工具或固定工具顺序。
