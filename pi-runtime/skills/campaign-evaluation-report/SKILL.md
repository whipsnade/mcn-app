---
name: campaign-evaluation-report
description: 用审计 Evidence 生成活动效果评估 Artifact，并保持 BI 和 Excel 同版本。
---

# 活动效果评估

适用于活动效果、内容表现、参与者、互动、话题或投放复盘。确认活动身份、日期窗口、平台、
比较范围和用户想回答的业务问题；缺少决定性范围时先澄清。

围绕目标收集 Evidence，并核对活动归属、指标口径、样本局限和数据可用性。空结果、超时和
供应商错误必须保留为限制；满足核心章节时允许 partial，禁止编造活动效果、付费归属或未返回
的因果结论。

使用 `build_campaign_report_draft` 生成 Draft，依据 Builder feedback 的 gaps、availability、
coverage 和 limitations 决定补查。完成条件是发布后的 Artifact Version 通过校验，BI 与 Excel
均指向同一 Version；不能满足时说明哪些章节未完成。
