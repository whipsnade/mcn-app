---
name: marketing-strategy
description: 基于真实 MCP Tool Result 与已发布 Artifact 提供受限、可说明依据的策略咨询。
---

# 营销策略咨询

先判断用户要的是既有结果解释、正式研究、钻取还是开放式策略建议。策略建议
优先引用已发布 Artifact Version 与真实 MCP Tool Result；范围、时效或覆盖不足时
调用 request_clarification 澄清关键条件，必要时再查询 DataTap。

- 建议必须区分事实、证据支持的推断和待验证假设，不把缺失数据包装成结论。
- 策略咨询可纯文字回复：不要自动创建报告；只有用户需要可发布正式产物时才
  加载对应专项 Skill，按其 model_input_contract 经 build_artifact_draft +
  publish_artifacts 发布。
- 数据受限时明确说明，禁止编造市场规模、预算、投放效果或用户偏好。
