# 模型决策可恢复 + 品牌分析最低可交付 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 缺工具名决策改可恢复回喂（不终止、不扣费、连续修正 2 次）；修正耗尽且有证据时降级受限报告；brand_loop prompt 强化；brainstorm/GoalPlanner 注入 current_date 与 period 覆写合理性校验。

**Architecture:** `resolve_agent_call` 拆 `AGENT_DECISION_MISSING_TOOL_NAME` / `TOOL_NOT_ALLOWED`；executor 新增独立修正计数与结构化回喂，耗尽时走 completed_with_warnings + warning_code 透传报告构建器；报告 prompt 注入受限声明；日期锚点注入两个 planner payload + override 校验。

**Spec:** `docs/superpowers/specs/2026-07-28-recoverable-decision-and-limited-report-design.md`

**验证命令（每个 Task 的 Commit 前必跑）：**

```bash
cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/orchestration tests/tasks tests/reporting tests/goals tests/brainstorm -q
```

---

### Task 1: resolve_agent_call 错误码拆分

**Files:**
- Modify: `backend/app/orchestration/loop.py:255-280`
- Test: `backend/tests/orchestration/`（找 resolve_agent_call 既有测试文件，就近追加）

- [ ] **Step 1: 写失败测试**

```python
# 1. action=call_tool 顶层无 internal_tool_name → PlanValidationError("AGENT_DECISION_MISSING_TOOL_NAME")
# 2. internal_tool_name 误嵌在 arguments 内（顶层 None）→ 同样 AGENT_DECISION_MISSING_TOOL_NAME
# 3. 顶层有 internal_tool_name 但不在 context.tools → PlanValidationError("TOOL_NOT_ALLOWED")
# 4. 合法调用不受影响（回归）
```

- [ ] **Step 2: 跑测试确认失败** → 现码是 AGENT_TOOL_MISSING。

- [ ] **Step 3: 实现**

`resolve_agent_call`：
```python
    if not decision.internal_tool_name:
        raise PlanValidationError("AGENT_DECISION_MISSING_TOOL_NAME")
    if decision.internal_tool_name not in context.tools:
        raise PlanValidationError("TOOL_NOT_ALLOWED")
```
（AGENT_TOOL_MISSING 全仓 grep 处理引用点；误嵌形态不需要特殊检测代码——顶层为空即新码，错位提示在 Task 2 回喂文案里按 `isinstance(decision.arguments, dict) and "internal_tool_name" in decision.arguments` 检测。）

- [ ] **Step 4: 回归** → PASS
- [ ] **Step 5: Commit** `git add backend/app/orchestration/loop.py backend/tests/orchestration/ && git commit -m "refactor: 拆分缺工具名与工具不允许决策错误码"`

---

### Task 2: executor 可恢复修正 + 修正耗尽受限交付

**Files:**
- Modify: `backend/app/tasks/executor.py`（`_run_goal_loop` 的回喂/熔断与终态分支）
- Modify: `backend/app/tasks/dependencies.py`（`finalize_goal` 把 warning_code 转发给 `_finalize_analysis_goal`）
- Test: `backend/tests/tasks/test_agent_loop.py` 或 test_goal_lifecycle.py（就近）

- [ ] **Step 1: 写失败测试**

```python
# 1. 连续 2 次「缺工具名」决策：两次都不调 MCP、不扣积分；每次回喂 EvidenceNote 含
#    错因 + 允许工具清单 + goal 目标提示；误嵌形态时文案点名「误嵌在 arguments 内」。
# 2. 第 3 次缺工具名：goal 不失败——有 settled 证据时终态 completed_with_warnings、
#    goal.warning_code == "brand_trend_data_unavailable"、调用报告构建、任务 reason 为
#    "decision_recovery_exhausted"、用户消息为「部分数据未能获取，已基于已采集数据生成报告。」
# 3. 零 settled 证据时第 3 次缺工具名：仍按现状失败。
# 4. TOOL_NOT_ALLOWED 等其他 PlanValidationError：第 2 次熔断行为不变（回归）。
# 5. 修正成功（第 3 次合法调用 trend）：继续执行、计数清零。
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**

`executor.py`：
- 新计数器 `missing_tool_name_streak`（与 invalid_streak 并列，校验成功时两计数器都清零）。
- `PlanValidationError("AGENT_DECISION_MISSING_TOOL_NAME")` 分支：streak += 1 → ≤2 时回喂结构化 EvidenceNote 继续循环（不调 MCP 不扣积分，与现有回喂同路径）；>2 时进入受限交付：
  - **接线（plan 评审修正）**：不得在 executor 直接调 `_finalize_analysis_goal`（会重复构建）。正确路径是走既有 `self._finalize_goal(task.id, goal, "completed_with_warnings", warning_code="brand_trend_data_unavailable")`（**不传 error_code**），并由 `finalize_goal` 把 warning_code 转发给内部的 `_finalize_analysis_goal`（`dependencies.py:360` 调用点同步透传，见 Task 3）。
  - 有 settled 证据 → 上述 finalize；任务 reason `"decision_recovery_exhausted"`（`mark_completed_with_warnings` 的 reason/消息从硬编码扩为按场景传入：「部分数据未能获取，已基于已采集数据生成报告。」）。
  - 无 settled 证据 → 维持现状 failed。
  - **多 goal 编排路径（`_orchestrate_goals`）本期不做**：该路径耗尽时按现状 has_failures 收尾（单 goal 路径才有受限交付），spec 遗留项记录。
- 回喂 EvidenceNote 构造：错因行（误嵌 arguments 内检测并点名）+ `允许的工具：{internal_name} ×N`（附一行用途）+ goal 目标提示（params.requirement 或 goal_type 描述）。

- [ ] **Step 4: 回归** → PASS
- [ ] **Step 5: Commit** `git add backend/app/tasks/executor.py backend/tests/tasks/ && git commit -m "feat: 缺工具名决策可恢复修正与修正耗尽受限交付"`

---

### Task 3: 报告构建注入受限声明

**Files:**
- Modify: `backend/app/reporting/builders.py`（`_run_goal_analysis` / `run_brand_analysis`）与 `backend/app/model/prompts.py`（`BRAND_ANALYSIS_PROMPT` 加受限声明占位）
- Modify: `backend/app/tasks/dependencies.py`（`_finalize_analysis_goal` 透传 warning_code）
- Test: `backend/tests/reporting/test_builders.py`

- [ ] **Step 1: 写失败测试**

```python
# run_brand_analysis 带 warning_code="brand_trend_data_unavailable" 时：
# 模型输入含受限声明（「趋势数据未成功获取」「不输出跨期趋势结论」与禁止伪造趋势的指令）；
# 不带 warning_code 时输入无声明（回归）。
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**

- 固定映射 `_LIMITATION_NOTES = {"brand_trend_data_unavailable": "趋势数据未成功获取"}`；
  builders 的报告输入加 `limitation` 键（None 时省略）；`BRAND_ANALYSIS_PROMPT` 加一段：
  「存在 limitation 时，报告必须明确标注：已完成品牌概览与情感快照；{limitation}，
  不输出跨期趋势结论；不得根据 overview 的环比字段伪造完整趋势分析。」
- `_finalize_analysis_goal(..., warning_code=None)` 透传；其余 goal 类型调用点不传（None）。

- [ ] **Step 4: 回归** → PASS
- [ ] **Step 5: Commit** `git add backend/app/reporting/builders.py backend/app/model/prompts.py backend/app/tasks/dependencies.py backend/tests/reporting/ && git commit -m "feat: 受限报告注入趋势缺口声明"`

---

### Task 4: brand_loop prompt 强化 + 循环状态注入

**Files:**
- Modify: `backend/app/model/prompts.py`（`BRAND_ANALYSIS_LOOP_SYSTEM_TEXT`）
- Modify: `backend/app/orchestration/`（AgentLoopContext/user payload 注入已调用工具 + 剩余缺口）——先读现状（context 结构、payload 组装点）再定最小改法
- Test: prompt contract 测试（`backend/tests/goals/test_prompt_contract.py` 或新建）

- [ ] **Step 1: 写失败测试**

```python
# prompt 含：internal_tool_name 顶层必填、禁止嵌进 arguments、不确定时 finish、
# 阶段工具顺序（标签匹配→概览→趋势→可选话题/受众）。
# 循环 payload 含 called_tools 与 evidence_gaps 键（有值时）。
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**：prompt 补规则段落；payload 注入 `called_tools`（轨迹中已 settled 的工具名去重）与 `evidence_gaps`（按 goal 类型静态清单减已得——最小实现：从 goal params/已有证据推导，保持简单）。

- [ ] **Step 4: 回归** → PASS
- [ ] **Step 5: Commit** `git add backend/app/model/prompts.py backend/app/orchestration/ backend/tests/ && git commit -m "feat: brand_loop 工具契约强化与循环状态注入"`

---

### Task 5: 日期锚点与 period 覆写校验

**Files:**
- Modify: `backend/app/brainstorm/service.py`（user content 加 current_date）、`backend/app/goals/planner.py`（payload 加 current_date）、`backend/app/model/prompts.py`（BRAINSTORM/GOAL_PLANNER 各加一行日期规则）
- Modify: `backend/app/tasks/dependencies.py:94-111`（`param_profile_period_override` 合理性校验）
- Test: 对应测试文件就近追加

- [ ] **Step 1: 写失败测试**

```python
# brainstorm/planner 的模型输入含 current_date == date.today().isoformat()。
# override：end > today → 拒绝覆写；end < today - 400 天 → 拒绝；合法近期窗口 → 覆写（回归）。
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**：payload 加 `"current_date": date.today().isoformat()`；prompt 各加一行
  「相对时间（最近 N 天/个月）一律以 current_date 为基准折算。」；override 校验
  （拒绝时记 warning 返回 None）。

- [ ] **Step 4: 回归** → PASS
- [ ] **Step 5: Commit** `git add backend/app/brainstorm/service.py backend/app/goals/planner.py backend/app/model/prompts.py backend/app/tasks/dependencies.py backend/tests/ && git commit -m "fix: 注入当前日期锚点并校验 period 覆写合理性"`

---

### Task 6: 全量验证 + changelog

- [ ] **Step 1: 全量验证**

```bash
cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest -q
cd .. && npm run test && npm run lint && npm run build
```

- [ ] **Step 2: changelog 并提交**

`changelog/2026-07-28.md` 新建：事故（误嵌工具名 → 笼统回喂 → 熔断 → 证据丢弃；
日期锚点缺失）、五项修复、验证、遗留（模型修正遵循度实测、受限声明文案抽样）。

```bash
git add -f changelog/2026-07-28.md
git commit -m "docs: 记录决策可恢复与受限交付"
```

---

## 备注

- 实现中如行号/结构不一致，以源码为准；`AGENT_TOOL_MISSING` 的其他引用点（前端
  文案/测试）一并核对。
- 前端无改动。
