# MCP 错误详情展示 + 会话任务暂停按钮实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 失败工具节点展示 MCP 上游错误详情（已脱敏落库的数据打通到 SSE 与 UI）；发送按钮图标化，任务运行中变暂停图标，点击协作式取消当前任务。

**Architecture:** 后端 `build_tool_event_payload` 增加 `upstream_message` 字段（取自 `mcp_calls.evidence_json["upstream_error_message"]`，已 `safe_upstream_text` 脱敏）；前端 reducer 存 `node.upstreamDetail` 并在节点 detail 下多渲染一行。取消复用已就绪的 `POST /tasks/{id}/cancel` + `cancelTask` client，新增 `cancelRequested` latch 状态驱动三态图标按钮。

**Tech Stack:** FastAPI + SQLAlchemy Async（后端 `backend/`），React + TypeScript + Vitest（前端 `src/`）。

**Spec:** `docs/superpowers/specs/2026-07-27-mcp-error-display-and-task-cancel-design.md`

**验证命令（每个 Task 的 Commit 前必跑）：**

```bash
# 后端（在 backend/ 目录）
.venv/bin/ruff check app tests
DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/tasks -q
# 前端（仓库根目录）
npm run test && npm run lint
```

---

### Task 1: 后端工具失败事件携带 upstream_message

**Files:**
- Modify: `backend/app/tasks/executor.py:180-200`（`build_tool_event_payload`）与 `:784-802`（调用点）
- Test: 先看 `backend/tests/tasks/` 哪个文件覆盖 `build_tool_event_payload`（可能 `test_agent_loop.py` 或专设 payload 测试），就近追加；无则新建 `backend/tests/tasks/test_tool_event_payload.py`

- [ ] **Step 1: 写失败测试**

```python
from app.tasks.executor import build_tool_event_payload


def test_failed_payload_includes_upstream_message_when_present() -> None:
    payload = build_tool_event_payload(
        "kol_search",
        status="failed",
        step_index=2,
        step_total=None,
        error_code="upstream_tool_error",
        upstream_message="达人不存在或已注销",
    )

    assert payload["upstream_message"] == "达人不存在或已注销"
    assert payload["message"]  # 白名单文案保留


def test_failed_payload_omits_upstream_message_when_absent_or_blank() -> None:
    for upstream in (None, "", "   "):
        payload = build_tool_event_payload(
            "kol_search",
            status="failed",
            step_index=2,
            step_total=None,
            error_code="connection_timeout",
            upstream_message=upstream,
        )
        assert "upstream_message" not in payload


def test_succeeded_payload_never_includes_upstream_message() -> None:
    payload = build_tool_event_payload(
        "kol_search",
        status="succeeded",
        step_index=1,
        step_total=None,
        upstream_message="不应出现",
    )
    assert "upstream_message" not in payload
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/tasks -q -k tool_event_payload`
Expected: FAIL（`TypeError: unexpected keyword argument 'upstream_message'`）

- [ ] **Step 3: 实现**

`build_tool_event_payload` 加参数与写入：

```python
def build_tool_event_payload(
    internal_tool_name: str,
    *,
    status: str,
    step_index: int,
    step_total: int | None,
    error_code: str | None = None,
    goal_id: str | None = None,
    upstream_message: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "platform": canonical_platform(internal_tool_name),
        "step_index": step_index,
        "step_total": step_total,
    }
    if goal_id is not None:
        payload["goal_id"] = goal_id
    if status in {"failed", "unknown"}:
        failure = safe_error(error_code)
        payload.update({"error_code": failure.code, "message": failure.message})
        # 上游错误原文（mcp_calls 落库前已 safe_upstream_text 脱敏）随事件透传，
        # 与白名单 message 并存；缺失/空白时省略该键。
        if upstream_message and upstream_message.strip():
            payload["upstream_message"] = upstream_message
    return payload
```

调用点（`executor.py:788-802`）从 `row` 取值传入：

```python
                build_tool_event_payload(
                    pending.internal_tool_name,
                    status=(...),
                    step_index=step_index,
                    step_total=None,
                    error_code=getattr(row, "error_type", None),
                    goal_id=goal_id,
                    upstream_message=(
                        (getattr(row, "evidence_json", None) or {}).get(
                            "upstream_error_message"
                        )
                        if row is not None
                        else None
                    ),
                ),
```

注意：`evidence_json["upstream_error_message"]` 可能不是 str（防御）——若非 str 传 None；`row` 为 None 时传 None。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/tasks -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/tasks/executor.py backend/tests/tasks/
git commit -m "feat: 工具失败事件透传脱敏后的上游错误详情"
```

---

### Task 2: 前端失败节点渲染上游错误详情

**Files:**
- Modify: `src/state/taskEvents.ts:21-27`（`TaskFlowNode`）与 `:127-142`（`withFlowNode` 的 tool 终态分支）
- Modify: `src/components/TaskFlowNodes.tsx:33-37`
- Test: 就近——reducer 测试（找 taskEvents 的既有测试文件）与 `src/components/` 下 TaskFlowNodes 相关测试（可能被 ChatArea.test.tsx 覆盖，没有则新建 `TaskFlowNodes.test.tsx`）

- [ ] **Step 1: 写失败测试**

reducer 用例（两条写入路径都要覆盖）：

```ts
// tool.failed 带 upstream_message：running 节点更新路径
// tool.failed 带 upstream_message 且没有 running 节点：late push 路径
// tool.failed 不带 upstream_message：节点无 upstreamDetail
```

组件用例：failed 节点同时有 detail 与 upstreamDetail 时两行都渲染；无 upstreamDetail 时只一行。

- [ ] **Step 2: 跑测试确认失败**

Run: `npm run test -- src/state src/components/TaskFlowNodes`
Expected: FAIL（upstreamDetail 不存在）

- [ ] **Step 3: 实现**

`src/state/taskEvents.ts`：

```ts
export interface TaskFlowNode {
  id: string;
  label: string;
  status: TaskFlowNodeStatus;
  /** 失败/未确认节点的用户可读原因（报错信息）。 */
  detail?: string;
  /** MCP 上游返回的错误详情（后端已脱敏），展示在 detail 下方。 */
  upstreamDetail?: string;
}
```

`withFlowNode` 的 `tool.succeeded/failed/unknown` 分支（:127-142）：

```ts
        const detail = status === 'succeeded' ? undefined : String(event.payload.message ?? '') || undefined;
        const upstreamDetail = status === 'succeeded'
          ? undefined
          : String(event.payload.upstream_message ?? '').trim() || undefined;
        const index = latestRunningToolNode(nodes, stepIndex);
        if (index === -1) {
          return { ...state, nodes: pushNode(nodes, { id: `tool-${stepIndex}-late-${event.id}`, label: `查询${platform}数据`, status, detail, upstreamDetail }) };
        }
        return { ...state, nodes: updateNode(nodes, index, { status, detail, upstreamDetail }) };
```

`src/components/TaskFlowNodes.tsx` 的 `FlowNodeRow`（detail 行之后）：

```tsx
        {node.detail && (
          <p className={`mt-0.5 text-[10px] leading-4 ${node.status === 'failed' ? 'text-rose-600' : 'text-amber-600'}`}>
            {node.detail}
          </p>
        )}
        {node.upstreamDetail && (
          <p className="mt-0.5 font-mono text-[10px] leading-4 text-slate-500">
            {node.upstreamDetail}
          </p>
        )}
```

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `npm run test && npm run lint`
Expected: PASS、tsc 退出 0

- [ ] **Step 5: Commit**

```bash
git add src/state/taskEvents.ts src/components/TaskFlowNodes.tsx src/state/ src/components/
git commit -m "feat: 执行流失败节点展示 MCP 上游错误详情"
```

---

### Task 3: 发送/暂停图标按钮与取消接线

**Files:**
- Modify: `src/hooks/useWorkspace.ts`（`cancelRequested` latch + `cancelActiveTask` + return 导出）
- Modify: `src/App.tsx`（约 :177-196，传 props）
- Modify: `src/components/ChatArea.tsx`（props + 按钮三态，:479-489）
- Test: `src/hooks/useWorkspace.test.tsx`、`src/components/ChatArea.test.tsx`

- [ ] **Step 1: 写失败测试**

`useWorkspace.test.tsx`（仿既有用例的 mock 风格；`cancelTask` 已在 `../api/tasks` mock 列表里则直接 mock，没有则加进 mock）：

```ts
// 1. cancelActiveTask 调用 cancelTask(activeTaskId)，cancelRequested 置 true
// 2. latch 窗口：API 返回后、task.cancelled 到达前，cancelRequested 仍为 true
// 3. taskRuntime.status 进入终态（mock useTaskStream 返回 cancelled）后 cancelRequested 复位
// 4. 无 activeTaskId 时 no-op（cancelTask 不被调用）
// 5. cancelTask reject 时 cancelRequested 立即复位
```

`ChatArea.test.tsx`：

```ts
// 1. 非 analyzing：Send 图标 submit 按钮，输入为空禁用
// 2. isAnalyzing=true：渲染「暂停」按钮（aria-label），点击调 onCancelTask，不提交表单
// 3. isCancelling=true：按钮禁用且 aria-label 为「正在取消」
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npm run test -- src/hooks/useWorkspace.test.tsx src/components/ChatArea.test.tsx`
Expected: FAIL（cancelActiveTask / props 不存在）

- [ ] **Step 3: 实现**

`src/hooks/useWorkspace.ts`：

- import 处把 `cancelTask` 加入 `../api/tasks` 的 import 列表。
- state：`const [cancelRequested, setCancelRequested] = useState(false);`
- latch 复位（终态或任务清理/切换；spec 评审建议：终态判断放宽为「终态或不再 analyzing」，避免 SSE 长断卡死）：

```ts
  useEffect(() => {
    if (!cancelRequested) return;
    if (!activeTaskId) {
      setCancelRequested(false);
      return;
    }
    const status = currentTaskRuntime?.status;
    if (status && isTerminalTaskStatus(status)) {
      setCancelRequested(false);
    }
  }, [cancelRequested, activeTaskId, currentTaskRuntime?.status]);
```

- 方法（放在 appendMessage 附近，return 块导出 `cancelRequested` 与 `cancelActiveTask`）：

```ts
  const cancelActiveTask = useCallback(async () => {
    const taskId = activeTaskId;
    if (!taskId || cancelRequested) return;
    setCancelRequested(true);
    try {
      await cancelTask(taskId);
      // latch：不在此处复位，等 SSE task.cancelled（终态）由 effect 复位。
    } catch (cancelError) {
      console.warn('cancel task failed', cancelError);
      setCancelRequested(false);
    }
  }, [activeTaskId, cancelRequested]);
```

`src/App.tsx`：给 ChatArea 传 `onCancelTask={workspace.cancelActiveTask}`、`isCancelling={workspace.cancelRequested}`。

`src/components/ChatArea.tsx`：

- props interface 加 `onCancelTask: () => Promise<unknown>;` 与 `isCancelling: boolean;`（App.tsx 唯一调用点同步传入；组件测试的 props 构造同步补）。
- lucide-react import 加 `Pause, Loader2`（`Send` 已 import）。
- 按钮（替换 :479-489）：

```tsx
          {isAnalyzing ? (
            <button
              type="button"
              aria-label={isCancelling ? '正在取消' : '暂停'}
              disabled={isCancelling}
              onClick={() => void onCancelTask()}
              className={`px-3 py-2 rounded-lg text-white transition active:scale-95 ${
                isCancelling
                  ? 'bg-slate-200 text-slate-400 cursor-not-allowed'
                  : 'bg-rose-500 hover:bg-rose-600'
              }`}
            >
              {isCancelling
                ? <Loader2 className="h-4 w-4 animate-spin" />
                : <Pause className="h-4 w-4" />}
            </button>
          ) : (
            <button
              type="submit"
              aria-label="发送"
              disabled={!inputText.trim()}
              className={`px-3 py-2 rounded-lg text-white transition active:scale-95 ${
                inputText.trim()
                  ? 'bg-indigo-600 hover:bg-indigo-700'
                  : 'bg-slate-200 text-slate-400 cursor-not-allowed'
              }`}
            >
              <Send className="h-4 w-4" />
            </button>
          )}
```

注意：ChatArea 既有测试若断言「发送」文字按钮会失败，同步更新这些用例（改按 aria-label/图标断言），不得删测试。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `npm run test && npm run lint`
Expected: PASS、tsc 退出 0

- [ ] **Step 5: Commit**

```bash
git add src/hooks/useWorkspace.ts src/hooks/useWorkspace.test.tsx src/App.tsx src/components/ChatArea.tsx src/components/ChatArea.test.tsx
git commit -m "feat: 发送按钮图标化并支持运行中暂停取消任务"
```

---

### Task 4: 全量验证 + changelog

**Files:**
- Modify: `changelog/2026-07-27.md`（追加；需 `git add -f`）

- [ ] **Step 1: 全量验证**

```bash
cd backend
.venv/bin/ruff check app tests
DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest -q
# 预期：仅 2 个既有本地环境失败（test_real_providers 两例）
cd ..
npm run test && npm run lint && npm run build
```

- [ ] **Step 2: 写 changelog 并提交**

`changelog/2026-07-27.md` 追加「MCP 错误详情展示 + 任务暂停按钮」一节：背景、改动（事件 payload upstream_message 链路、前端两行渲染、取消 latch 与三态按钮）、验证结果、遗留事项（协作式取消延迟、SSE 长断时 latch 依赖终态复位）。

```bash
git add -f changelog/2026-07-27.md
git commit -m "docs: 记录 MCP 错误详情展示与任务暂停按钮"
```

---

## 备注

- 取消是协作式：`POST /tasks/{id}/cancel` 只打标记，executor 在循环边界真正终止；期间按钮 latch 禁用由 `cancelRequested` 保证。
- `upstream_message` 的安全前提：`safe_upstream_text` 已脱敏（含 URL/凭证标记直接丢弃）；不要把 `mcp_calls.error_message` 以外的未脱敏异常文本带进 payload。
- 实现中如字段名/行号与本计划不一致，以源码为准修正，不得改无关逻辑。
