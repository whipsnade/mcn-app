---
name: brand-research-report
description: 用可追溯 Evidence 生成同版本品牌社媒研究 Artifact、BI 与 Excel。
---

# 品牌社媒研究报告

适用于品牌声量、互动、情感、趋势、内容、地域、话题、受众或热门帖子等正式研究需求。
先确认品牌实体、日期窗口、平台、关键词和对比口径；任一条件会显著影响结果时请求澄清。

按用户问题和可用工具收集足以支撑章节的 Evidence，并检查字段口径、空值、时间范围与
平台覆盖。DataTap 结果不足或失败时保留 availability 与 limitations；核心证据存在时可以
形成 partial 报告，禁止编造缺失数值或将替代口径伪装成原始口径。

调用 `build_brand_report_draft` 时只提交 scope、Evidence ID 与允许的叙事字段。阅读
Builder feedback 中的 availability、coverage、limitations 和 Evidence 引用，决定是否仍需
补查；不要手写报告 payload。完成条件是 Builder 校验成功、发布后的 Artifact Version 可读，
且 BI 与 Excel 绑定同一 Version；否则透明说明未完成的章节。

可参考 `references/chatgpt-datatap-success.md` 的参数化原则，而不能复制其来源实体、日期、
数值或工具顺序。
