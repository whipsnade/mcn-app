# 思考内容并入执行流程节点 设计

日期：2026-07-27
状态：已确认（用户要求：思考内容加入执行节点、默认折叠，效果见用户截图）

## 背景与目标

当前思考内容有两个独立展示区：消息下方的 `ThinkingPanel`（按 turn 折叠展示）与任务
「执行流程」面板（`TaskFlowNodes`，只有工具/阶段节点）。用户希望像参考截图一样：
**思考块作为节点穿插在执行流程列表中**（Agent → Thinking → Shell → …），
默认折叠、可展开查看内容，形成「决策 → 调用 → 决策 → 调用」的完整叙事。

## 现状

- `ChatArea.tsx:392-398`：`<TaskFlowNodes nodes={flowNodes} />`（执行流程，终态自动折叠
  为摘要行）；`:372-376`：每条有 thinking 的消息下方渲染 `<ThinkingPanel blocks=... />`。
- 思考块数据：`thinkingByTurn`（历史 metadata + 实时 SSE 合并，key 为 turnId），块含
  `purpose/label/content/status/durationMs/truncated/attempt`。
- 流节点数据：`taskRuntime.nodes`（`TaskFlowNode{id,label,status,detail,upstreamDetail}`），
  工具节点 id 为 `tool-{stepIndex}`，stepIndex 即 loop 迭代序号。
- 思考与工具事件是两条 SSE 流，**无共享时钟**——穿插顺序只能用启发式。

## 设计

### 1. 节点穿插规则（启发式，确定性）

按块的 `purpose` + 完成顺序映射到流节点位置：

- `goal_planner` / `brainstorm` 块 → 插在首个工具节点之前（规划先于执行）。
- `agent_loop` 块 → 第 i 块（按完成顺序）插在 `step_index = i` 的工具节点**之前**
  （第 i 轮迭代：先模型决策、后工具调用）；多出的块排在最后工具节点之后。
- `kol_analysis` / `brand_analysis` / `campaign_analysis` / `goal_summary` 等收尾类块 →
  插在最后一个工具节点之后、终态节点（分析完成/任务失败/报告已生成）之前。
- 无工具节点时按上述类别顺序依次排列。

### 2. ThinkingFlowNode 展示

- 节点形态：灯泡图标 + `block.label`（如「正在分析数据」）；running 时脉冲点 +
  「思考中」；completed 时附「x.x 秒」；interrupted 时琥珀色「思考中断」。
- **默认全部折叠**（用户明确要求）；逐节点独立展开：展开显示 `content`
  （`whitespace-pre-wrap`，与 ThinkingPanel 同规格）、attempt≥2 的「正在修正输出格式」
  提示、`truncated` 的「思考内容已截断」提示、折叠占位符原文。
- 内容实时更新：running 块的 snapshot/delta 经既有 `thinkingByTurn` 合并链路流入，
  展开时随事件刷新（reducer 语义不变，纯展示层组装）。

### 3. 与 ThinkingPanel 的关系（去重）

- 活跃 turn（有可见执行流程的任务所属 turn）：**消息下方的 ThinkingPanel 隐藏**，
  思考只在流程面板中出现，避免同一内容两处展示。
- 任务终态后：流程面板自动折叠为摘要行（现状），消息级 ThinkingPanel 恢复显示
  （completed 折叠态）——刷新/回看路径不变。
- 历史 turn 的 ThinkingPanel 完全不受影响。

### 4. 数据来源与接线

- `ChatArea` 计算活跃 turn 的思考块：`thinkingByTurn[activeTurnId]`（活跃 turn 取最新
  用户消息的 `turnId`），作为新 prop 传入 `TaskFlowNodes`。
- `TaskFlowNodes` 新增可选 prop `thinkingBlocks?: ThinkingBlock[]`，内部按 §1 规则
  把思考块转换为 `ThinkingFlowNode` 与流节点合并渲染；折叠态为本组件本地 state
  （Set<nodeKey>，默认空=全折叠）。

### 不做的事（YAGNI）

- 不改后端与两条 SSE 流、不改 reducer、不改 metadata 结构。
- 不做思考块与工具节点的严格时序对齐（两条流无共享时钟，§1 启发式即可）。
- 多 goal 编排下不做 goal 分组展示（goal 维度进度展示本来就是遗留增强项）。

## 测试策略

- 节点合并规则：三类 purpose 的插入位置（规划在前、agent_loop 按序穿插、收尾在后）、
  无工具节点时的顺序、超出工具数量的 agent_loop 块兜底。
- ThinkingFlowNode：默认折叠；点击展开显示 content；running 脉冲与「思考中」；
  completed 显示秒数；interrupted 琥珀态；attempt≥2 与 truncated 提示。
- ChatArea 集成：活跃 turn 的 ThinkingPanel 隐藏、终态后恢复；历史 turn 不受影响。

## 遗留事项

- agent_loop 块与工具节点的 i↔i 映射在「模型连续两轮未调工具」时会轻微错位
  （展示层启发式，可接受；真实时序以后端事件为准）。
