# 思考持久化死锁修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 思考持久化移出请求事务（独立短事务 + 死锁重试 + commit 后标记），消除幽灵 task_id；前端 events 404 停止重试并复位运行态。

**Architecture:** 新 helper `persist_turn_thinking`（`app/thinking/persistence.py`）统一承接 brainstorm/clarify/respond/execute 四条请求路径的思考持久化：先捕获 blocks 列表 → 独立事务 persist+attach → commit 成功后 `mark_blocks_persisted` → 1213 死锁用同一列表重试一次。前端 useTaskStream 识别 `SSE_404` 永久态停止重连并标记 `notFound`，useWorkspace 据此清空 activeTaskId。

**Tech Stack:** FastAPI + SQLAlchemy Async（后端 `backend/`），React + TypeScript + Vitest（前端 `src/`）。

**Spec:** `docs/superpowers/specs/2026-07-27-thinking-persist-deadlock-fix-design.md`

**验证命令（每个 Task 的 Commit 前必跑）：**

```bash
cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/thinking tests/brainstorm tests/tasks -q
# 前端（仓库根目录）
npm run test && npm run lint
```

---

### Task 1: persist_turn_thinking helper

**Files:**
- Modify: `backend/app/thinking/persistence.py`（文件尾部新增 helper）
- Test: `backend/tests/thinking/test_persistence.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
async def test_persist_turn_thinking_marks_only_after_commit(...) -> None:
    # 首轮事务在 attach 前抛 OperationalError(1213)，第二轮成功：
    # - persist_block 被调用两轮（重试复用同一 blocks 列表）
    # - mark_blocks_persisted 只在成功 commit 后调用一次
    # 用 monkeypatch 替换 ThinkingMessageStore / attach，配合内存 fake thinking service。

async def test_persist_turn_thinking_swallows_repeat_failure(...) -> None:
    # 两轮都抛 1213：helper 正常返回（尽力而为），mark 未调用。

async def test_persist_turn_thinking_without_assistant_skips_attach(...) -> None:
    # assistant_message_id=None：只 persist，不 attach（execute 分支用法）。
```

fixture 参考该文件既有用例（auth_client_factory / db_session / 真实 session+消息构造）。
`OperationalError` 构造：`sqlalchemy.exc.OperationalError("","", Exception())` 并把
`error.orig` 调整为 `args=(1213, "Deadlock found ...")` 的异常（helper 的 `_is_deadlock`
按 asyncmy 形态 `orig.args[:1] == (1213,)` 判定）。monkeypatch 注入点：
`app.thinking.persistence.ThinkingMessageStore`（helper 内模块级引用）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/thinking/test_persistence.py -q -k persist_turn`
Expected: FAIL（`ImportError: cannot import name 'persist_turn_thinking'`）

- [ ] **Step 3: 实现**

`backend/app/thinking/persistence.py` 尾部新增：

```python
async def persist_turn_thinking(
    session_factory: _SessionFactory,
    thinking_service,
    *,
    user_id: str,
    session_id: str,
    turn_id: str,
    assistant_message_id: str | None = None,
) -> None:
    """请求 commit 后的思考持久化：独立短事务 + 死锁重试一次。

    顺序契约（spec §1）：先捕获 blocks 列表 → 独立事务 persist+attach →
    commit 成功后才 mark_blocks_persisted（内存标记不得先于事务成功，
    否则死锁重试时 block 永久丢失）。assistant_message_id 为 None 时跳过
    attach（execute 分支无 assistant 消息）。全部异常只记 warning。
    """
    try:
        blocks = await thinking_service.completed_blocks(
            turn_id=turn_id,
            user_id=user_id,
            session_id=session_id,
            only_unpersisted=True,
        )
    except Exception:
        logger.warning("persist_turn_thinking_read_failed session_id=%s", session_id, exc_info=True)
        return
    if not blocks and assistant_message_id is None:
        return

    async def _persist_once() -> None:
        async with session_factory.begin() as db:
            store = ThinkingMessageStore(db)
            for block in blocks:
                await store.persist_block(block, user_id=user_id, session_id=session_id)
            if assistant_message_id is not None:
                assistant = await db.get(Message, assistant_message_id)
                if assistant is not None:
                    await store.attach_turn_to_assistant(
                        assistant,
                        user_id=user_id,
                        session_id=session_id,
                        turn_id=turn_id,
                    )

    attempts = 0
    while attempts < 2:
        attempts += 1
        try:
            await _persist_once()
            break
        except Exception as error:  # noqa: BLE001 — 尽力而为，死锁重试一次
            if attempts >= 2 or not _is_deadlock(error):
                logger.warning(
                    "persist_turn_thinking_failed session_id=%s", session_id, exc_info=True
                )
                return
    if blocks:
        try:
            await thinking_service.mark_blocks_persisted(
                turn_id=turn_id,
                user_id=user_id,
                session_id=session_id,
                keys=[(block.operation_id, block.attempt) for block in blocks],
            )
        except Exception:
            logger.warning(
                "persist_turn_thinking_mark_failed session_id=%s", session_id, exc_info=True
            )


def _is_deadlock(error: Exception) -> bool:
    orig = getattr(error, "orig", None)
    args = getattr(orig, "args", ())
    # asyncmy/PyMySQL 的 orig.args 形态为 (1213, "Deadlock found ...")。
    return args[:1] == (1213,) or "1213" in str(orig)
```

imports 补：`import logging`、`from sqlalchemy.exc import OperationalError`（如未导入）、`from app.workspace.models import Message`（已导入）。`logger = logging.getLogger(__name__)` 如文件没有则补。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/thinking -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/thinking/persistence.py backend/tests/thinking/test_persistence.py
git commit -m "feat: 思考持久化独立事务 helper（死锁重试 + commit 后标记）"
```

---

### Task 2: brainstorm 成功路径改接 commit 后持久化

**Files:**
- Modify: `backend/app/brainstorm/service.py`（移除 respond 内 persist/attach 与 `_persist_completed_blocks` 方法）
- Modify: `backend/app/brainstorm/router.py`（commit 后 persist → submit）
- Test: `backend/tests/brainstorm/test_brainstorm.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
async def test_brainstorm_ready_task_actually_exists(auth_client_factory, db_session, monkeypatch) -> None:
    # ready 画像 → 200 → 响应 task_id 在 analysis_tasks 真实存在（幽灵任务回归）。

async def test_brainstorm_ready_survives_thinking_persist_deadlock(auth_client_factory, db_session, monkeypatch) -> None:
    # monkeypatch persist_turn_thinking 抛 OperationalError(1213)（或直接抛 RuntimeError）：
    # 响应仍 200、任务真实存在、assistant 消息真实存在。
```

注意：ready 画像的构造参考该文件既有 ready 用例（`_full_profile` 之类）。

- [ ] **Step 2: 跑测试确认失败/现状确认**

死锁注入用例在现状代码下表现：任务不存在或响应异常（确认事故可复现）；幻影回归用例现状应已通过（没死锁时任务存在）。

- [ ] **Step 3: 实现**

`backend/app/brainstorm/service.py`：
- respond() 删除 `await self._persist_completed_blocks(...)`（:131-135）与
  `attach_turn_to_assistant` 整段 try（:165-177）；删除 `_persist_completed_blocks` 方法
  与不再使用的 import（ThinkingMessageStore 等，ruff 会指出）。
- `BrainstormOutcome.message` 的 metadata 保持现状（brainstorm+task_id，不含 turn_id/thinking）。

`backend/app/brainstorm/router.py`：
- imports 加 `from app.thinking.persistence import persist_turn_thinking`。
- `await db.commit()` 之后、`task_runner.submit` 之前插入：

```python
    await db.commit()
    try:
        await persist_turn_thinking(
            SessionFactory,
            thinking_service,
            user_id=user_id,
            session_id=session_id,
            turn_id=str(payload.turn_id),
            assistant_message_id=outcome.message.id,
        )
    except Exception:
        logger.warning(
            "brainstorm_thinking_persist_failed session_id=%s", session_id, exc_info=True
        )
    if outcome.task_id is not None:
        task_runner.submit(outcome.task_id)
```

（helper 内部已吞异常，外层 try 是双保险，与现有风格一致。）

**测试环境关键前置（plan 评审发现，必须照做）**：测试 fixture 用共享连接 + savepoint
隔离，请求事务的 commit 只释放 savepoint；helper 走真实 `SessionFactory`（新连接）
会看不到未提交数据而静默 no-op，导致 `test_brainstorm.py` 中经 GET 恢复验证
`metadata.thinking`/`turn_id` 的既有用例失败。**这些恢复态断言不是响应 DTO 断言，
不得删除或改弱**。正确做法（仓库已有先例 `test_brainstorm.py:464-476`）：相关用例
monkeypatch `app.brainstorm.router.SessionFactory` 为产出共享 `db_session` 的假
`_SessionFactory`，helper 即可在同连接内真实执行，既有断言无需改动。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/brainstorm tests/thinking -q`
Expected: PASS。两类既有用例区分处理：
- 断言**响应 DTO** 带 thinking metadata / turn_id 的：按 spec §1 的 DTO 变化声明更新；
- 经 GET 恢复验证**落库** `metadata.thinking`/`turn_id` 的（如 test_brainstorm.py:163、:263-264）：
  不得改动——按 Step 3 的「测试环境关键前置」给这些用例注入共享 db_session 的假
  SessionFactory 让它们真实通过。

- [ ] **Step 5: Commit**

```bash
git add backend/app/brainstorm/service.py backend/app/brainstorm/router.py backend/tests/brainstorm/test_brainstorm.py
git commit -m "fix: brainstorm 思考持久化移到 commit 后独立事务，消除幽灵 task_id"
```

---

### Task 3: tasks 路由三分支改接 commit 后持久化

**Files:**
- Modify: `backend/app/tasks/router.py`（clarify :360-397、respond :398-451、execute :503-528 三分支）
- Test: `backend/tests/tasks/test_enforce_create_task.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
async def test_enforce_execute_task_exists_when_thinking_persist_fails(auth_client_factory, db_session, monkeypatch) -> None:
    # enforce + execute 输出；monkeypatch persist_turn_thinking 抛 RuntimeError：
    # 202、响应 task_id 真实存在于 analysis_tasks。

async def test_enforce_clarify_survives_thinking_persist_failure(auth_client_factory, db_session, monkeypatch) -> None:
    # clarify 输出；monkeypatch persist_turn_thinking 抛错：202、clarify 消息真实落库。
```

- [ ] **Step 2: 跑测试确认失败**

预期现状下：monkeypatch 目标不存在（`app.tasks.router.persist_turn_thinking` 未导入）→ FAIL。

- [ ] **Step 3: 实现**

`backend/app/tasks/router.py`：
- imports：`from app.thinking.persistence import persist_turn_thinking`（`_persist_turn_blocks` 不再被三分支使用则删除该函数——已确认调用点只有 clarify/respond/execute 三处，删除安全，ruff 会兜底检查残留）。
- clarify 分支：`await db.commit()`（:396）之后插入 `persist_turn_thinking(SessionFactory, thinking_service, user_id=..., session_id=..., turn_id=turn_id, assistant_message_id=message.id)`（try/except warning 双保险），删除分支内原 `_persist_turn_blocks` + `attach_turn_to_assistant` 段；`bind_turn` 保留在原位置。
- respond 分支：同上（assistant_message_id=message.id）。
- execute 分支：`await db.commit()`（:525）之后、`task_runner.submit` 之前**无条件**插入 `persist_turn_thinking(..., assistant_message_id=None)`（enforce 关闭时 blocks 为空且 assistant_message_id=None → helper 提前 return，无害）；删除 `if planner_attempted:` 块内 `_persist_turn_blocks` 调用（保留 `bind_turn`）。
- **测试环境关键前置（同 Task 2）**：test_enforce_create_task.py 中经恢复验证落库
  `metadata.thinking`/`turn_id` 的既有用例不得改弱；给相关用例 monkeypatch
  `app.tasks.router.SessionFactory` 为共享 `db_session` 的假 `_SessionFactory`
  （先例 `test_brainstorm.py:464-476`），helper 才能看到请求事务的数据。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/tasks tests/thinking tests/brainstorm -q`
Expected: PASS（既有用例断言响应带 thinking metadata 的按 spec DTO 变化更新）

- [ ] **Step 5: Commit**

```bash
git add backend/app/tasks/router.py backend/tests/tasks/test_enforce_create_task.py
git commit -m "fix: tasks 路由三分支思考持久化移到 commit 后独立事务"
```

---

### Task 4: 前端 events 404 停止重试并复位

**Files:**
- Modify: `src/hooks/useTaskStream.ts`（404 永久态判断）
- Modify: `src/state/taskEvents.ts`（`TaskRuntimeState` 加 `notFound?: boolean`）
- Modify: `src/hooks/useWorkspace.ts`（notFound → 清 activeTaskId）
- Test: `src/hooks/useTaskStream.test.tsx`、`src/hooks/useWorkspace.test.tsx`

- [ ] **Step 1: 写失败测试**

```ts
// useTaskStream：streamTaskEvents  reject Error('SSE_404') → 不再重连（调用次数 1）、
//   connection='closed'、notFound=true；reject Error('SSE_500') → 仍重连（调用次数 >1）。
// useWorkspace：taskRuntime.notFound=true → activeTaskId 变 undefined。
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npm run test -- src/hooks/useTaskStream.test.tsx src/hooks/useWorkspace.test.tsx`
Expected: FAIL（notFound 不存在/404 仍重连）

- [ ] **Step 3: 实现**

`src/state/taskEvents.ts`：`TaskRuntimeState` 加 `notFound?: boolean;`。

`src/hooks/useTaskStream.ts` catch 块：

```ts
        } catch (error) {
          if (controller.signal.aborted || stopped) break;
          const currentState = latestState.current ?? initial;
          if (error instanceof Error && error.message === 'SSE_404') {
            // 任务不存在（或已不可见）：永久态，停止重连并标记，交由上层复位。
            update({ ...currentState, connection: 'closed', notFound: true });
            break;
          }
          update({ ...currentState, connection: 'error' });
        }
```

`src/hooks/useWorkspace.ts` 新 effect：

```ts
  useEffect(() => {
    if (currentTaskRuntime?.notFound) {
      setActiveTaskId(undefined);
    }
  }, [currentTaskRuntime?.notFound]);
```

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `npm run test && npm run lint`
Expected: PASS、tsc 退出 0

- [ ] **Step 5: Commit**

```bash
git add src/hooks/useTaskStream.ts src/state/taskEvents.ts src/hooks/useWorkspace.ts src/hooks/
git commit -m "fix: 任务 events 404 停止重试并复位运行态"
```

---

### Task 5: 全量验证 + changelog

- [ ] **Step 1: 全量验证**

```bash
cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest -q
# 预期仅 2 个既有环境失败（test_real_providers）
cd .. && npm run test && npm run lint && npm run build
```

- [ ] **Step 2: changelog 并提交**

`changelog/2026-07-27.md` 追加「思考持久化死锁修复（幽灵任务）」：事故现象（死锁 → 请求事务静默回滚 → 幽灵 task_id → 前端无限 404）、修复（四路径移 commit 后独立事务 + 1213 重试 + commit 后 mark、前端 404 止损）、DTO 变化声明（响应不再带 thinking metadata；brainstorm 响应 message 无 turn_id）、验证结果、遗留（整会话 FOR UPDATE 锁范围观察项）。

```bash
git add -f changelog/2026-07-27.md
git commit -m "docs: 记录思考持久化死锁修复"
```

---

## 备注

- `record_brainstorm_failure`（失败路径）已是独立事务（post-rollback），不在事故路径，不动。
- 实现中如行号/调用点与计划不一致，以源码为准；不得改无关逻辑。
