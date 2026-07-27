# brainstorm ready 接入 GoalPlanner 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** brainstorm ready 内联建任务经 GoalPlanner 规划（execute 按 goals 建任务；clarify/respond/失败回退默认 kol_selection）。

**Architecture:** `BrainstormService` ready 分支新增 `_plan_task_goals`（镜像 `_plan_goal_or_fallback` 容错语义），planner execute → `goal_specs` 传给 `TaskService.create`；仅 enforce 开启时生效。

**Tech Stack:** FastAPI + Pydantic + pytest。

**Spec:** `docs/superpowers/specs/2026-07-27-brainstorm-goal-planner-design.md`

**验证命令：**

```bash
cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/brainstorm tests/goals -q
```

---

### Task 1: brainstorm ready 接入 GoalPlanner

**Files:**
- Modify: `backend/app/brainstorm/service.py`（ready 分支 + 新 `_plan_task_goals`）
- Test: `backend/tests/brainstorm/test_brainstorm.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
# 1. ready + planner execute(brand_analysis) → 任务存在且 goal 为 brand_analysis（本 bug 回归）；
#    monkeypatch GoalPlannerService.plan_context 返回 execute 输出。
# 2. ready + planner execute 多 goal（brand_analysis → kol_selection 依赖）→ 两 goal 落库、
#    sequence 与 depends_on 正确。
# 3. ready + planner clarify → 任务按默认 kol_selection 创建（回退），200。
# 4. ready + planner respond(context_qa) → 同样回退 kol_selection，200。
# 5. ready + planner 抛 RuntimeError → 回退 kol_selection，200。
# 6. enforce 关闭（monkeypatch settings.goal_planner_enforce_enabled=False）→
#    plan_context 未被调用，默认 kol_selection。
```

ready 画像构造参考该文件既有 ready 用例（`_full_profile`/`FakeBrainstormModel` + `_share_session_factory`）；enforce 默认在测试环境是 false（conftest setdefault "false"），用例 1-5 需 `monkeypatch.setattr(get_settings(), "goal_planner_enforce_enabled", True)`。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/brainstorm -q -k "plan"`
Expected: FAIL（plan_context 未被调用 / goal 类型为 kol_selection）

- [ ] **Step 3: 实现**

`backend/app/brainstorm/service.py`：

- imports：`from app.core.config import get_settings`、`from app.goals.context import GoalPlannerContextBuilder`、`from app.goals.planner import GoalPlannerService`。
- ready 分支（`if ready:` 内、`TaskService.create` 之前）：

```python
        goal_specs = None
        if ready and get_settings().goal_planner_enforce_enabled:
            goal_specs = await self._plan_task_goals(
                user_id=user_id,
                session_id=session_id,
                content=payload.content,
                turn_id=turn_id,
            )
        task = await TaskService(self.db).create(
            user_id,
            session_id,
            TaskCreate(content=payload.content),
            trigger_message_id=user_message.id,
            goal_specs=goal_specs,
        )
```

（按现状代码结构调整——task 创建与 goal_specs 传递在 ready 块内。）

- 新方法（镜像 `_plan_goal_or_fallback` 容错语义）：

```python
    async def _plan_task_goals(
        self,
        *,
        user_id: str,
        session_id: str,
        content: str,
        turn_id: str,
    ) -> list[dict] | None:
        """enforce 规划；clarify/respond/失败一律回退 None（默认 kol_selection）。"""
        thinking_sink = None
        try:
            thinking_sink = self.thinking_service.create_sink(
                ThinkingOperationSpec(
                    operation_id=str(uuid4()),
                    turn_id=turn_id,
                    session_id=session_id,
                    user_id=user_id,
                    purpose="goal_planner",
                    label="正在规划分析目标",
                )
            )
        except Exception:
            logger.warning(
                "brainstorm_goal_planner_sink_failed session_id=%s", session_id, exc_info=True
            )
        try:
            context = await GoalPlannerContextBuilder().build_for_message(
                user_id, session_id, content, db=self.db
            )
            output = await GoalPlannerService(
                model=self.model, context_builder=None
            ).plan_context(context, thinking_sink=thinking_sink)
        except LookupError:
            raise
        except Exception:
            logger.warning(
                "brainstorm_goal_planner_fallback session_id=%s", session_id, exc_info=True
            )
            return None
        if output.action != "execute" or not output.goals:
            logger.info(
                "brainstorm_goal_planner_non_execute session_id=%s action=%s",
                session_id,
                output.action,
            )
            return None
        return [
            {
                "goal_type": goal.goal_type,
                "sequence": goal.sequence,
                "depends_on_sequence": goal.depends_on_sequence,
                "params": goal.params.model_dump(mode="json", exclude_none=True),
            }
            for goal in sorted(output.goals, key=lambda item: item.sequence)
        ]
```

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/brainstorm tests/goals tests/tasks -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/brainstorm/service.py backend/tests/brainstorm/test_brainstorm.py
git commit -m "fix: brainstorm ready 内联建任务经 GoalPlanner 规划"
```

---

### Task 2: 全量验证 + changelog

- [ ] **Step 1: 全量验证**

```bash
cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest -q
cd .. && npm run test && npm run lint && npm run build
```

- [ ] **Step 2: changelog 并提交**

`changelog/2026-07-27.md` 追加「brainstorm ready 接入 GoalPlanner」：事故（澄清后任务固定圈选，品牌分析需求被错误执行）、修复（ready 路径规划 + 回退语义）、验证、遗留（两次模型调用的时延、clarify/respond 回退分支发生率待观测）。

```bash
git add -f changelog/2026-07-27.md
git commit -m "docs: 记录 brainstorm ready 接入 GoalPlanner"
```

---

## 备注

- `TaskService.create` 的 `goal_specs` 形参以 `backend/app/tasks/service.py` 实际签名为准。
- 前端无改动。
