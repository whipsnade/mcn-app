---
name: campaign-evaluation-report
description: 用审计 Evidence 生成活动效果评估 Artifact，并保持 BI 和 Excel 同版本。
---

# 活动效果评估

适用于活动效果、内容表现、参与者、互动、话题或投放复盘。先理解活动身份、日期窗口、平台、
比较范围和用户要回答的业务问题；缺少决定性范围时请求澄清，不用默认归因规则替代用户口径。

模型自主决策选择当前 Run Snapshot 中已审核的 Tool Contract，按业务问题收集 Evidence，
核对活动归属、指标口径、样本局限和数据可用性。空结果、超时和供应商错误必须保留为限制；
满足核心章节时允许 partial，禁止编造活动效果、付费归属或未返回的因果结论，也不要求固定
工具顺序或固定调用数量。

创建或更新 Draft 后阅读结构化校验反馈、gaps、availability、coverage、limitations 和来源路径，
再由模型决定补查、澄清、复核或完成。完成条件是发布后的 Artifact Version 通过强类型与来源
校验，BI 与 Excel 均指向同一 Version；长尾需求可使用 `analysis_report_v1` 与同版 `workbook_v1`，
不能满足时说明哪些章节未完成。

生产 Native 路径只读取 Snapshot 注入的 Skill、Tool Contract 和 Root Policy；不得手写正式
payload、Excel 或 BI。
