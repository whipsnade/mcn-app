---
name: brand-research-report
description: 用可追溯 Evidence 生成同版本品牌社媒研究 Artifact、BI 与 Excel。
---

# 品牌社媒研究报告

适用于品牌声量、互动、情感、趋势、内容、地域、话题、受众或热门帖子等正式研究需求。
先理解品牌实体、日期窗口、平台、关键词和对比口径；缺少会改变结论的条件时请求澄清，
不要假设默认范围。

模型自主决策收集路径：依据用户问题和当前 Run Snapshot 可用的 Tool Contract 选择已审核
工具，按需读取历史或调用 DataTap，不要求固定工具顺序。每次结果都保留 Evidence、字段
口径、空值、时间范围和平台覆盖；结果不足、空结果、错误或超时必须进入 availability 与
limitations。核心证据存在时可以形成 partial 报告，禁止编造缺失数值或把替代口径伪装成原始
口径。

创建或更新 Draft 时只提交业务字段和必要 Evidence 引用，阅读结构化校验反馈、coverage、
limitations 与来源路径后决定是否继续。完成条件是 Artifact Version 通过强类型校验、来源
冻结且可读；BI 与 Excel 必须绑定同一 Version。固定模板无法表达长尾问题时使用
`analysis_report_v1`，同版文件使用 `workbook_v1`；未完成时透明说明章节限制。

Skill 正文来自 Run Snapshot 注入目录，Root Policy 和 Tool Contract 由生产运行时控制；不要
手写正式 payload、Excel 或 BI。可参考 `references/chatgpt-datatap-success.md` 的参数化原则，
但不能复制其中的来源实体、日期、数值或工具顺序。
