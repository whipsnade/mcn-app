# brainstorm 平台多选（multi-select chips）设计

日期：2026-07-27
状态：已确认（用户选择「多选 chips + 确认按钮」）

## 背景与目标

澄清阶段平台选择只能单选。调查结论：后端全链路（`BrainstormProfile.platforms` list、
prompt 渠道码规则、`sessions.platforms` 写回、agent loop 逐平台检索、Excel 导出）
**早已按多值设计**，限制只在两处前端/合并层：

1. ChatArea 选项 chips 点击 = `setInputText(text)` **替换**输入框内容（`ChatArea.tsx:123-127`），
   无法连点多个平台。
2. `merge_profile`（`brainstorm/schemas.py:72-81`）整字段覆盖：分两轮各确认一个平台时，
   后一轮数组覆盖前一轮，多值丢失。

目标：平台类问题支持多选（chips 切换选中 + 确认拼接填入），单选问题交互不变；
分轮确认平台时画像不丢值。

### 已确认决策

- 多选交互：chips 可切换选中态 + 「确认」按钮，确认后拼接（如「抖音、小红书」）填入
  输入框并聚焦，用户检查后发送（与现有「建议点击不自动提交」原则一致）。
- 单/多选由后端标记（模型按问题字段判定），不做前端启发式猜测。

## 设计

### 后端

1. **schema**（`brainstorm/schemas.py`）：`BrainstormQuestion` 加 `multi: bool = False`；
   `merge_profile` 的 `platforms` 改为**并集合并**（保序去重：先旧后新），其余字段覆盖
   语义不变。
2. **prompt**（`model/prompts.py` 的 `BRAINSTORM_SYSTEM_TEXT`）：补一条规则——
   platforms（渠道）问题的 `question.multi=true` 且提问文案引导「可多选」，其余问题
   `multi=false`；options 仍 2-4 个渠道候选。
3. **metadata**（`brainstorm/service.py`）：`brainstorm_metadata` 加 `"multi": bool`
   （`output.question.multi`），仅 ready=false 有问题时有意义；workspace metadata
   白名单已含 `brainstorm` 整个键，无需改白名单。

### 前端

4. **类型**（`src/api/contracts.ts` 的 `BrainstormMetadata`，`src/types.ts` 仅引用）：加
   `multi?: boolean`。
5. **ChatArea**：最新消息 `brainstorm.options` 非空且 `brainstorm.multi === true` 时：
   - chips 渲染为可切换选中态（选中高亮/打勾样式），本地 state 记录选中集合
    （消息 id 变化时重置）；
   -  chips 旁渲染「确认」按钮（选中 ≥1 个可用）：把选中项用「、」拼接后
     `setInputText(joined)` + 聚焦（复用 fillInput 语义），并清空选中态。
   - `multi !== true`（含 planner clarify.options）保持现有单选 fillInput 行为不变。

### 不做的事（YAGNI）

- planner clarify（GoalQuestion）不加 multi（当前 clarify 场景无多选需求）。
- 不自动提交（保持用户确认后发送）。
- 不改下游（agent loop/圈选/导出已支持多平台）。

## 测试策略

- 后端：`BrainstormQuestion.multi` 默认 false；`merge_profile` platforms 并集（旧+新
  保序去重、空值处理）；brainstorm 端点 metadata 带 multi。
- 前端：multi=true 时 chips 可切换选中、确认拼接填入（「抖音、小红书」）；multi=false
  时点击仍整体替换输入框。

## 遗留事项

- prompt 引导模型在平台问题输出 multi=true 的准确率需实测观察（误判为多选只是交互
  形态变化，不丢数据）。
- `merge_profile` 并集语义下平台只增不减：用户改主意（先确认抖音、后说「只要小红书」）
  无法移除旧值。当前澄清流程 platforms 必填且一次问完，触发概率低，接受该取舍；
  后续如高频出现再评估「重新设置」语义。
- options 上限 4 个（schema 现状）而渠道共 5 个，维持现状不扩展。
