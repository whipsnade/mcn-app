# 模型思考流式展示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让结构化模型调用忽略 `<think>` 包装并严格解析其中的 JSON，同时把允许范围内的思考内容脱敏后通过会话 SSE 实时展示，完成或失败后折叠并持久化到 assistant 消息。

**Architecture:** `TencentPlanAdapter.complete_json` 在传入 `ThinkingSink` 时使用流式模型响应，由独立解析器分离思考与 JSON；没有 Sink 的内部调用继续走非流式路径，但共用同一个 JSON 提取器。新增会话级 `SessionThinkingBroker`/SSE 与消息 metadata 持久化层，前端通过独立 hook 合并运行时事件和历史消息中的 `thinking.blocks`，现有任务 SSE 保持不变。

**Tech Stack:** Python 3.11、FastAPI、Pydantic v2、SQLAlchemy Async、OpenAI-compatible `AsyncOpenAI`、React 19、TypeScript、Fetch ReadableStream/SSE、Vitest、pytest。

## Global Constraints

- 仅 `brainstorm`、`goal_planner`、`agent_loop`、`brand_analysis`、`campaign_analysis`、任务内自动 `kol_analysis` 可以传入 `ThinkingSink`。
- `goal_summary`、followup、quick、手动 BI 按钮调用和其他后台辅助模型调用不得向会话发布思考事件。
- JSON 提取只容忍 `<think>`、`reasoning_content`、Markdown fence 和单个 JSON 对象外的包装文本；Pydantic 字段、类型及 `extra="forbid"` 仍严格执行。
- 单个思考 block 最多 12,000 字符，同一 turn 最多 30,000 字符；用户展示副本必须脱敏，`model_prompt_logs` 继续保留既有原始响应。
- 思考 Sink、Broker、SSE、脱敏和 metadata 持久化异常只记 warning，不得改变任务、Goal、Artifact、积分或正式模型结果。
- 现有任务 SSE `/api/v1/tasks/{task_id}/events` 及其事件契约不修改。
- 不新增第三方运行时依赖，不新增数据库表；最终内容写入现有 `messages.metadata_json`。
- 工作区已有用户修改：`backend/app/tasks/repository.py`、`backend/scripts/smoke_multi_intent.py`、`backend/tests/tasks/test_release_expired_unknown.py`。实现时必须保留并基于现状增量修改，禁止覆盖或回退。

---

## File Structure

### 新建文件

- `backend/app/model/structured_output.py`：跨 chunk 的 `<think>`/JSON 解析和单 JSON 对象提取。
- `backend/app/thinking/__init__.py`：thinking 包出口。
- `backend/app/thinking/contracts.py`：思考 operation、block、事件和状态类型。
- `backend/app/thinking/sanitizer.py`：用户展示副本脱敏与长度限制。
- `backend/app/thinking/service.py`：Broker、运行快照、`SessionThinkingSink` 和 turn 级缓存。
- `backend/app/thinking/persistence.py`：`messages.metadata_json` 的暂存、归并和失败消息持久化。
- `backend/app/thinking/router.py`：会话 SSE 端点。
- `backend/tests/model/test_structured_output.py`：解析器单元测试。
- `backend/tests/thinking/__init__.py`：后端 thinking 测试包。
- `backend/tests/thinking/test_sanitizer.py`：脱敏与限长测试。
- `backend/tests/thinking/test_service.py`：Broker、快照、慢消费者和 Sink 测试。
- `backend/tests/thinking/test_router.py`：SSE 鉴权和会话隔离测试。
- `backend/tests/thinking/test_persistence.py`：消息 metadata 暂存和归并测试。
- `src/api/sessionThinking.ts`：会话思考 SSE 客户端。
- `src/api/sessionThinking.test.ts`：会话思考 SSE 解析测试。
- `src/state/sessionThinking.ts`：前端思考事件 reducer。
- `src/state/sessionThinking.test.ts`：reducer 测试。
- `src/hooks/useSessionThinkingStream.ts`：连接、重连和会话切换。
- `src/hooks/useSessionThinkingStream.test.tsx`：hook 测试。
- `src/components/ThinkingPanel.tsx`：展开、折叠和阶段分组 UI。
- `src/components/ThinkingPanel.test.tsx`：UI 行为测试。

### 重点修改文件

- `backend/app/model/contracts.py`、`backend/app/model/tencent_plan.py`
- `backend/app/brainstorm/schemas.py`、`service.py`、`router.py`
- `backend/app/goals/planner.py`
- `backend/app/tasks/schemas.py`、`service.py`、`router.py`、`dependencies.py`、`repository.py`
- `backend/app/reporting/builders.py`、`backend/app/selection/analysis.py`
- `backend/app/workspace/serializers.py`
- `backend/app/api/router.py`
- `src/api/contracts.ts`、`brainstorm.ts`、`tasks.ts`、`sessions.ts`
- `src/types.ts`、`src/hooks/useWorkspace.ts`
- `src/components/ChatArea.tsx`、`ChatArea.test.tsx`
- `changelog/2026-07-26.md`

---

### Task 1: 结构化输出解析器

**Files:**
- Create: `backend/app/model/structured_output.py`
- Create: `backend/tests/model/test_structured_output.py`

**Interfaces:**
- Produces: `ThinkJsonStreamParser.feed_content(text: str) -> tuple[str, ...]`
- Produces: `ThinkJsonStreamParser.feed_reasoning(text: str) -> tuple[str, ...]`
- Produces: `ThinkJsonStreamParser.finish() -> ParsedStructuredOutput`
- Produces: `extract_single_json_object(text: str) -> str`
- Produces: `parse_non_stream_output(text: str) -> ParsedStructuredOutput`
- `ParsedStructuredOutput` fields: `raw_text: str`, `json_text: str`, `thinking_text: str`

- [ ] **Step 1: 写 JSON 提取失败测试**

```python
import pytest

from app.model.structured_output import extract_single_json_object


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('{"value":1}', '{"value":1}'),
        ('```json\n{"value":1}\n```', '{"value":1}'),
        ('说明文字 {"value":{"items":[1,2]}} 结束', '{"value":{"items":[1,2]}}'),
        ('前缀 {"text":"包含 } 和 \\\\" 引号"} 后缀', '{"text":"包含 } 和 \\\\" 引号"}'),
    ],
)
def test_extract_single_json_object(source: str, expected: str) -> None:
    assert extract_single_json_object(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "没有 JSON",
        '{"value":1',
        '{"value":1} {"value":2}',
    ],
)
def test_extract_single_json_object_rejects_missing_truncated_or_multiple(source: str) -> None:
    with pytest.raises(ValueError, match="structured_json_invalid"):
        extract_single_json_object(source)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && .venv/bin/pytest tests/model/test_structured_output.py -q`

Expected: FAIL，提示 `app.model.structured_output` 不存在。

- [ ] **Step 3: 实现感知字符串转义和嵌套深度的单对象提取**

```python
@dataclass(frozen=True)
class ParsedStructuredOutput:
    raw_text: str
    json_text: str
    thinking_text: str


def extract_single_json_object(text: str) -> str:
    source = _strip_json_fence(text).strip()
    start = source.find("{")
    if start < 0:
        raise ValueError("structured_json_invalid")
    depth = 0
    in_string = False
    escaped = False
    end: int | None = None
    for index, char in enumerate(source[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end is None:
        raise ValueError("structured_json_invalid")
    trailing = source[end:].strip()
    if "{" in trailing:
        raise ValueError("structured_json_invalid")
    return source[start:end]
```

- [ ] **Step 4: 写跨 chunk think 解析失败测试**

```python
from app.model.structured_output import ThinkJsonStreamParser, parse_non_stream_output


def test_parser_handles_tags_split_across_chunks() -> None:
    parser = ThinkJsonStreamParser()
    deltas: list[str] = []
    for chunk in ("<th", "ink>正在", "分析</thi", "nk>{\"value\":1}"):
        deltas.extend(parser.feed_content(chunk))
    result = parser.finish()
    assert "".join(deltas) == "正在分析"
    assert result.thinking_text == "正在分析"
    assert result.json_text == '{"value":1}'


def test_parser_merges_reasoning_content_and_tagged_think() -> None:
    parser = ThinkJsonStreamParser()
    assert parser.feed_reasoning("先确认品牌") == ("先确认品牌",)
    assert parser.feed_content("<think>再确认平台</think>{\"value\":1}") == ("再确认平台",)
    result = parser.finish()
    assert result.thinking_text == "先确认品牌再确认平台"
    assert result.json_text == '{"value":1}'


def test_non_stream_parser_ignores_think_and_markdown() -> None:
    result = parse_non_stream_output(
        "<think>内部思考</think>\n```json\n{\"value\":1}\n```"
    )
    assert result.thinking_text == "内部思考"
    assert result.json_text == '{"value":1}'
```

- [ ] **Step 5: 实现增量状态机并通过全部解析器测试**

状态机必须缓存最多 `len("</think>") - 1` 个潜在标签字符；只有确认字符不属于标签时才写入
JSON 缓冲或思考缓冲。`finish()` 将未闭合 think 视为思考，把 think 外文本交给
`extract_single_json_object`。

Run: `cd backend && .venv/bin/pytest tests/model/test_structured_output.py -q`

Expected: PASS。

- [ ] **Step 6: 提交解析器**

```bash
git add backend/app/model/structured_output.py backend/tests/model/test_structured_output.py
git commit -m "fix: 解析模型 think 包装中的结构化 JSON"
```

---

### Task 2: `complete_json` 流式适配与 Sink 协议

**Files:**
- Modify: `backend/app/model/contracts.py`
- Modify: `backend/app/model/tencent_plan.py`
- Modify: `backend/tests/model/test_prompt_logs.py`
- Modify: `backend/tests/model/test_reasoning_effort.py`
- Create: `backend/tests/model/test_structured_stream.py`

**Interfaces:**
- Consumes: Task 1 的 `ThinkJsonStreamParser`、`parse_non_stream_output`
- Produces: `ThinkingSink` Protocol
- Produces: `StructuredModelRequest.thinking_sink: ThinkingSink | None`
- Sink methods:
  - `started(*, attempt: int) -> Awaitable[None]`
  - `delta(text: str, *, attempt: int) -> Awaitable[None]`
  - `completed(*, attempt: int, duration_ms: int) -> Awaitable[None]`
  - `failed(*, attempt: int, error_code: str) -> Awaitable[None]`

- [ ] **Step 1: 写非流式 think 容错失败测试**

在 `backend/tests/model/test_prompt_logs.py` 增加：

```python
@pytest.mark.asyncio
async def test_complete_json_non_stream_ignores_think_wrapper() -> None:
    writer = _CaptureWriter()
    adapter = TencentPlanAdapter(
        client=_FakeCompletions([
            _json_response('<think>检查字段</think>{"value": 3}')
        ]),
        log_writer=writer,
    )

    result = await adapter.complete_json(_request())

    assert result.value.value == 3
    assert writer.entries[0].response == '<think>检查字段</think>{"value": 3}'
```

- [ ] **Step 2: 运行单测确认当前严格整串解析失败**

Run: `cd backend && .venv/bin/pytest tests/model/test_prompt_logs.py::test_complete_json_non_stream_ignores_think_wrapper -q`

Expected: FAIL，抛出 `MODEL_PLAN_INVALID`。

- [ ] **Step 3: 给非流式路径接入 Task 1 提取器**

把：

```python
value = request.output_model.model_validate_json(content, strict=True)
```

改为：

```python
parsed = parse_non_stream_output(content)
value = request.output_model.model_validate_json(parsed.json_text, strict=True)
```

原始 `content` 继续写入 `log.parts`，不把清洗后的 JSON 替换进管理员日志。

- [ ] **Step 4: 写真实流式 Sink 失败测试**

`backend/tests/model/test_structured_stream.py` 使用脚本化 async iterator：

```python
@pytest.mark.asyncio
async def test_complete_json_streams_thinking_and_validates_only_json() -> None:
    sink = CaptureThinkingSink()
    client = FakeCompletions([
        stream_chunks(
            content_chunks=["<th", "ink>分析", "品牌</think>", '{"value":4}'],
            reasoning_chunks=[None, None, None, None],
        )
    ])
    adapter = TencentPlanAdapter(client=client, log_writer=CaptureWriter())

    result = await adapter.complete_json(_request(thinking_sink=sink))

    assert result.value.value == 4
    assert sink.deltas == [(1, "分析"), (1, "品牌")]
    assert sink.terminal == ("completed", 1)
    assert client.calls[0]["stream"] is True
```

再增加：

- `reasoning_content` 独立字段可以发布 delta。
- 第一次 JSON 无效、第二次修复成功时 attempt 为 1、2。
- Sink 的任意方法抛异常，模型结果仍成功。
- 供应商返回明确“不支持 stream”400 时退回 `stream=False`，并一次性发布完整 think。
- 流已经输出部分内容后中断时不得重放供应商请求，Sink 收到 `failed`。

- [ ] **Step 5: 运行流式测试确认失败**

Run: `cd backend && .venv/bin/pytest tests/model/test_structured_stream.py -q`

Expected: FAIL，`StructuredModelRequest` 不接受 `thinking_sink`。

- [ ] **Step 6: 实现 Sink 协议和流式分支**

在 `contracts.py` 增加：

```python
class ThinkingSink(Protocol):
    async def started(self, *, attempt: int) -> None:
        raise NotImplementedError

    async def delta(self, text: str, *, attempt: int) -> None:
        raise NotImplementedError

    async def completed(self, *, attempt: int, duration_ms: int) -> None:
        raise NotImplementedError

    async def failed(self, *, attempt: int, error_code: str) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class StructuredModelRequest(Generic[T]):
    purpose: ModelPurpose
    template_name: str
    messages: tuple[ChatMessage, ...]
    output_model: type[T]
    max_tokens: int = 4096
    log_context: dict[str, Any] | None = field(default=None, compare=False)
    thinking_sink: ThinkingSink | None = field(default=None, compare=False)
```

在 `TencentPlanAdapter` 中：

- `thinking_sink is None`：保留非流式供应商请求，使用 Task 1 提取器。
- `thinking_sink is not None`：调用新增 `_create_json_stream_with_retry`。
- 每次 regeneration 调 `sink.started(attempt=regeneration_count + 1)`。
- 读取 `delta.reasoning_content` 与 `delta.content`。
- 供应商流不支持缓存键为 `(base_url, model)`。
- `_safe_sink_call` 捕获异常并记录 warning。
- 第二次校验失败和供应商异常都必须发送 `failed`。
- `stream_options={"include_usage": True}`，usage 和 request ID 沿用 `_stream_text` 的提取逻辑。

- [ ] **Step 7: 更新既有测试对 stream 参数的断言**

没有 Sink 的 `test_reasoning_effort_sent_when_configured` 仍断言 `stream=False`；新增有 Sink 的测试断言
`reasoning_effort` 同样出现在 `stream=True` 请求。

Run:

```bash
cd backend
.venv/bin/pytest tests/model/test_structured_output.py tests/model/test_structured_stream.py tests/model/test_prompt_logs.py tests/model/test_reasoning_effort.py -q
```

Expected: PASS。

- [ ] **Step 8: 提交模型适配层**

```bash
git add backend/app/model/contracts.py backend/app/model/tencent_plan.py backend/tests/model
git commit -m "feat: 流式分离结构化模型思考内容"
```

---

### Task 3: 会话思考 Broker、脱敏和 SSE

**Files:**
- Create: `backend/app/thinking/__init__.py`
- Create: `backend/app/thinking/contracts.py`
- Create: `backend/app/thinking/sanitizer.py`
- Create: `backend/app/thinking/service.py`
- Create: `backend/app/thinking/router.py`
- Create: `backend/tests/thinking/__init__.py`
- Create: `backend/tests/thinking/test_sanitizer.py`
- Create: `backend/tests/thinking/test_service.py`
- Create: `backend/tests/thinking/test_router.py`
- Modify: `backend/app/api/router.py`

**Interfaces:**
- Consumes: Task 2 的 `ThinkingSink`
- Produces: `ThinkingOperationSpec`
- Produces: `ThinkingBlock`
- Produces: `SessionThinkingService.create_sink(spec) -> SessionThinkingSink`
- Produces: `SessionThinkingService.bind_turn(*, turn_id: str, user_id: str, session_id: str, task_id: str | None, trigger_message_id: str | None) -> Awaitable[None]`
- Produces: `SessionThinkingService.completed_blocks(*, turn_id: str, user_id: str, session_id: str) -> tuple[ThinkingBlock, ...]`
- Produces: `get_session_thinking_service() -> SessionThinkingService`
- Produces: `GET /api/v1/sessions/{session_id}/events`

- [ ] **Step 1: 写脱敏失败测试**

```python
from app.thinking.sanitizer import sanitize_thinking


def test_sanitize_thinking_hides_secrets_and_large_schema() -> None:
    source = (
        "Authorization: Bearer abc.def.ghi\n"
        "api_key=sk-live-secret\n"
        "JSON Schema:\n{\"properties\":{\"token\":{\"type\":\"string\"}}}\n"
        "继续分析品牌"
    )
    result = sanitize_thinking(source, max_chars=12_000)
    assert "abc.def.ghi" not in result.text
    assert "sk-live-secret" not in result.text
    assert "[已隐藏]" in result.text
    assert "[输出结构说明已隐藏]" in result.text
    assert result.truncated is False


def test_sanitize_thinking_truncates_at_exact_limit() -> None:
    result = sanitize_thinking("分析" * 7000, max_chars=12_000)
    assert len(result.text) <= 12_000
    assert result.truncated is True
    assert result.text.endswith("思考内容过长，已截断")
```

- [ ] **Step 2: 实现纯函数脱敏并通过测试**

脱敏顺序必须为：Bearer/JWT/API key → 系统提示词段 → JSON Schema 段 → 长度限制。

Run: `cd backend && .venv/bin/pytest tests/thinking/test_sanitizer.py -q`

Expected: PASS。

- [ ] **Step 3: 写 Broker、snapshot 和慢消费者失败测试**

```python
@pytest.mark.asyncio
async def test_reconnect_receives_snapshot_before_future_delta() -> None:
    service = SessionThinkingService(queue_size=4)
    sink = service.create_sink(operation("op-1", "turn-1", "session-1"))
    await sink.started(attempt=1)
    await sink.delta("分析品牌", attempt=1)

    queue = await service.subscribe("session-1")
    snapshot = await asyncio.wait_for(queue.get(), timeout=0.1)
    assert snapshot.type == "thinking.snapshot"
    assert snapshot.payload["text"] == "分析品牌"

    await sink.delta("和平台", attempt=1)
    delta = await asyncio.wait_for(queue.get(), timeout=0.1)
    assert delta.type == "thinking.delta"
    assert delta.payload["text"] == "和平台"


@pytest.mark.asyncio
async def test_slow_consumer_is_compacted_to_latest_snapshot() -> None:
    service = SessionThinkingService(queue_size=2)
    queue = await service.subscribe("session-1")
    sink = service.create_sink(operation("op-1", "turn-1", "session-1"))
    await sink.started(attempt=1)
    for text in ("一", "二", "三", "四"):
        await sink.delta(text, attempt=1)
    events = drain(queue)
    assert events[-1].type == "thinking.snapshot"
    assert events[-1].payload["text"] == "一二三四"
```

- [ ] **Step 4: 实现 contracts、Broker 和 Sink**

`ThinkingOperationSpec` 固定字段：

```python
@dataclass(frozen=True)
class ThinkingOperationSpec:
    operation_id: str
    turn_id: str
    session_id: str
    user_id: str
    purpose: str
    label: str
    task_id: str | None = None
    goal_id: str | None = None
```

`ThinkingBlock` 固定字段：

```python
@dataclass(frozen=True)
class ThinkingBlock:
    operation_id: str
    turn_id: str
    purpose: str
    attempt: int
    label: str
    content: str
    status: Literal["completed", "interrupted"]
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    task_id: str | None = None
    goal_id: str | None = None
    truncated: bool = False
```

Sink 内部累计原始思考只存在内存；每次 delta 对“累计原文”重新执行脱敏：

- 新脱敏文本以旧脱敏文本开头：发布差值 `thinking.delta`。
- 脱敏替换改变旧前缀：发布完整 `thinking.snapshot`，避免跨 chunk 密钥泄漏。
- `completed/failed` 生成 `ThinkingBlock` 并移除运行快照。
- 同一 turn 的所有 block 总展示文本超过 30,000 字符时截断后续 block。

- [ ] **Step 5: 写 SSE 鉴权与隔离失败测试**

```python
@pytest.mark.asyncio
async def test_session_thinking_events_require_session_owner(auth_client_factory) -> None:
    owner = await auth_client_factory("13500000101")
    stranger = await auth_client_factory("13500000102")
    session_id = (await owner.post("/api/v1/sessions", json={})).json()["id"]

    assert (await stranger.get(
        f"/api/v1/sessions/{session_id}/events",
        headers={"Accept": "text/event-stream"},
    )).status_code == 404
```

路由测试通过依赖覆盖注入一个会立即发送 snapshot 并关闭的测试 service，断言 SSE 的
`event: thinking.snapshot`、`data:` 和 `: keepalive` 格式。

- [ ] **Step 6: 实现 SSE 路由并注册**

`router.py`：

- 使用 `CurrentUser`。
- 建流前查询 `WorkspaceSession.id/user_id/deleted_at`，非归属返回 404。
- 复用 FastAPI `StreamingResponse(media_type="text/event-stream")`。
- 每 15 秒发送 `: keepalive\n\n`。
- 取消请求时 unsubscribe。
- `Last-Event-ID` 只作兼容接收；重连权威数据由 snapshot 提供。

`api/router.py`：

```python
from app.thinking.router import router as thinking_router
api_router.include_router(thinking_router, prefix="/sessions", tags=["sessions"])
```

Run:

```bash
cd backend
.venv/bin/pytest tests/thinking/test_sanitizer.py tests/thinking/test_service.py tests/thinking/test_router.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交会话思考基础设施**

```bash
git add backend/app/thinking backend/app/api/router.py backend/tests/thinking
git commit -m "feat: 增加会话级思考事件流"
```

---

### Task 4: Turn ID 与消息 metadata 持久化

**Files:**
- Create: `backend/app/thinking/persistence.py`
- Create: `backend/tests/thinking/test_persistence.py`
- Modify: `backend/app/brainstorm/schemas.py`
- Modify: `backend/app/tasks/schemas.py`
- Modify: `backend/app/tasks/service.py`
- Modify: `backend/app/workspace/serializers.py`
- Modify: `backend/tests/brainstorm/test_brainstorm.py`
- Modify: `backend/tests/tasks/test_enforce_create_task.py`
- Modify: `backend/tests/workspace/test_sessions.py`

**Interfaces:**
- Consumes: Task 3 的 `ThinkingBlock`
- Produces: `BrainstormRequest.turn_id: UUID`
- Produces: `TaskCreate.turn_id: UUID`
- Produces: `ThinkingMessageStore(db: AsyncSession)`
- Produces: `ThinkingMessageStore.persist_block(block: ThinkingBlock, *, user_id: str, session_id: str) -> Awaitable[None]`
- Produces: `ThinkingMessageStore.attach_turn_to_assistant(message: Message, *, user_id: str, session_id: str, turn_id: str) -> Awaitable[None]`
- Produces: `record_brainstorm_failure(session_factory, *, user_id: str, session_id: str, turn_id: str, user_content: str, blocks: tuple[ThinkingBlock, ...], error_code: str) -> Awaitable[Message]`
- Public message metadata keys: `turn_id`、`thinking`
- Internal-only metadata key: `thinking_pending`

- [ ] **Step 1: 写请求契约和公开 metadata 失败测试**

```python
def test_task_create_accepts_turn_id_and_rejects_invalid_uuid() -> None:
    turn_id = "8a9fda07-77c5-44ea-967e-a17e795266ef"
    assert str(TaskCreate(content="分析品牌", turn_id=turn_id).turn_id) == turn_id
    with pytest.raises(ValidationError):
        TaskCreate(content="分析品牌", turn_id="not-a-uuid")


def test_public_message_metadata_exposes_thinking_but_hides_pending() -> None:
    metadata = public_message_metadata({
        "turn_id": "turn-1",
        "thinking": {"version": 1, "status": "completed", "blocks": []},
        "thinking_pending": {"blocks": [{"content": "internal staging"}]},
    })
    assert metadata["turn_id"] == "turn-1"
    assert "thinking" in metadata
    assert "thinking_pending" not in metadata
```

- [ ] **Step 2: 增加 UUID 字段并让 TaskService 写入用户消息**

`BrainstormRequest`、`TaskCreate`：

```python
turn_id: UUID = Field(default_factory=uuid4)
```

`TaskService.create` 在创建或复用 trigger message 后执行：

```python
metadata = dict(message.metadata_json or {})
metadata.setdefault("turn_id", str(payload.turn_id))
message.metadata_json = metadata
```

重试任务沿用源 trigger message 的 `turn_id`，不得以新的 `TaskCreate` 默认 UUID 覆盖。

- [ ] **Step 3: 写 block 暂存和归并失败测试**

测试数据库行为：

1. 只有 user 消息时，`persist_block` 写入隐藏的 `thinking_pending.blocks`。
2. 已有同 turn assistant 结论时，直接写入公开 `thinking.blocks`。
3. `operation_id + attempt` 重放不产生重复 block。
4. `attach_turn_to_assistant` 把 pending 复制到 assistant，删除 user 上的 pending。
5. 一个 failed block 使顶层状态为 `interrupted`。

核心断言：

```python
await store.persist_block(block, user_id=user.id, session_id=session.id)
await store.persist_block(block, user_id=user.id, session_id=session.id)
assert len(user_message.metadata_json["thinking_pending"]["blocks"]) == 1

await store.attach_turn_to_assistant(
    assistant_message,
    user_id=user.id,
    session_id=session.id,
    turn_id=block.turn_id,
)
assert assistant_message.metadata_json["thinking"]["blocks"][0]["content"] == "分析品牌"
assert "thinking_pending" not in user_message.metadata_json
```

- [ ] **Step 4: 实现 `ThinkingMessageStore`**

实现以下规则：

- 所有查询同时带 `user_id`、`session_id`。
- assistant 查找优先 `turn_id`，任务路径同时允许 `task_id`。
- block 主键为 `(operation_id, attempt)`。
- metadata 更新时整体复制 dict，确保 SQLAlchemy JSON 脏检查生效。
- 顶层 `thinking.status` 根据 blocks 重新计算。
- 模块级 `record_brainstorm_failure` 使用传入的 `SessionFactory` 开独立事务，写入同 turn 的 user 消息和
  `content="模型暂时无法完成需求理解，请稍后重试。"` 的 assistant 错误消息，并附 interrupted blocks。

- [ ] **Step 5: 运行消息与请求测试**

Run:

```bash
cd backend
.venv/bin/pytest tests/thinking/test_persistence.py tests/brainstorm/test_brainstorm.py tests/tasks/test_enforce_create_task.py tests/workspace/test_sessions.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交 Turn 与持久化**

```bash
git add backend/app/thinking/persistence.py backend/app/brainstorm/schemas.py backend/app/tasks/schemas.py backend/app/tasks/service.py backend/app/workspace/serializers.py backend/tests
git commit -m "feat: 持久化会话轮次思考内容"
```

---

### Task 5: 允许范围内的业务调用接线

**Files:**
- Modify: `backend/app/brainstorm/service.py`
- Modify: `backend/app/brainstorm/router.py`
- Modify: `backend/app/goals/planner.py`
- Modify: `backend/app/tasks/router.py`
- Modify: `backend/app/tasks/dependencies.py`
- Modify: `backend/app/tasks/repository.py`
- Modify: `backend/app/reporting/builders.py`
- Modify: `backend/app/selection/analysis.py`
- Modify: `backend/tests/brainstorm/test_brainstorm.py`
- Modify: `backend/tests/goals/test_planner.py`
- Modify: `backend/tests/tasks/test_agent_loop.py`
- Modify: `backend/tests/tasks/test_goal_lifecycle.py`
- Modify: `backend/tests/reporting/test_builders.py`
- Modify: `backend/tests/selection/test_analysis.py`

**Interfaces:**
- Consumes: `get_session_thinking_service()`、`ThinkingMessageStore`
- Consumes: `StructuredModelRequest.thinking_sink`
- Produces: `GoalPlannerService.plan_context(context: GoalPlannerContext, *, thinking_sink: ThinkingSink | None = None) -> Awaitable[GoalPlannerOutput]`
- Produces: report builder 可选参数 `thinking_sink: ThinkingSink | None = None`

- [ ] **Step 1: 写“只允许白名单用途”失败测试**

给各调用点的 FakeModel 记录 `request.thinking_sink`：

```python
assert brainstorm_model.requests[0].purpose == "brainstorm"
assert brainstorm_model.requests[0].thinking_sink is not None

assert planner_model.requests[0].purpose == "goal_planner"
assert planner_model.requests[0].thinking_sink is not None

assert agent_model.requests[0].purpose == "agent_loop"
assert agent_model.requests[0].thinking_sink is not None

assert summary_model.requests[0].purpose == "goal_summary"
assert summary_model.requests[0].thinking_sink is None
```

报告测试分别断言任务内 `brand_analysis`、`campaign_analysis`、自动 `kol_analysis` 有 Sink；
手动 `/kol-analysis` 没有 turn 上下文，因此 Sink 为 None。

- [ ] **Step 2: 给 Brainstorm 接入 Sink 与失败持久化**

`BrainstormService.respond`：

- 用户消息 metadata 写 `turn_id`。
- `_complete` 创建 label 为“正在理解需求”的 Sink。
- 成功创建 assistant 消息时调用 `attach_turn_to_assistant`。

`brainstorm.router` 捕获 `ModelAdapterError`：

```python
except ModelAdapterError as error:
    blocks = thinking_service.completed_blocks(
        turn_id=str(payload.turn_id),
        user_id=user.id,
        session_id=session_id,
    )
    await db.rollback()
    await record_brainstorm_failure(
        SessionFactory,
        user_id=user.id,
        session_id=session_id,
        turn_id=str(payload.turn_id),
        user_content=payload.content,
        blocks=blocks,
        error_code=error.code,
    )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=ErrorCode.BRAINSTORM_MODEL_ERROR,
    ) from error
```

- [ ] **Step 3: 给 GoalPlanner 与任务创建接入 turn**

`GoalPlannerService.plan_context` 增加可选 Sink 并透传给 `StructuredModelRequest`。

`tasks.router`：

- `_plan_goal_or_fallback` 接收 `turn_id` 并创建“正在规划分析目标”Sink。
- `clarify` 路径补落 user 消息，user/assistant 都写相同 `turn_id`。
- `execute` 创建任务后调用
  `thinking_service.bind_turn(turn_id=str(payload.turn_id), user_id=user.id, session_id=session_id, task_id=task.id, trigger_message_id=task.trigger_message_id)`，
  把 planner blocks 写入 trigger user 暂存。
- planner 异常回退 KOL 时仍保留 failed block，任务创建后绑定。
- 幂等命中不重新创建 operation，也不重复 block。

- [ ] **Step 4: 给 Agent Loop 接入 task/goal/turn**

构建 `AgentLoopContext.log_context` 时加入：

```python
{
    "user_id": user_id,
    "session_id": session_id,
    "task_id": task.id,
    "goal_id": goal.id if goal else None,
    "turn_id": trigger_message.metadata_json.get("turn_id"),
    "tags": agent_loop_tags(context),
}
```

`agent_decide` 仅在 turn_id 存在时创建“正在分析数据”Sink。每次模型调用使用新的
`operation_id`，同一次修复重试由 adapter 复用 operation。

Sink 完成后通过 `ThinkingMessageStore.persist_block`：

- conclusion 已存在：直接追加到 conclusion。
- conclusion 不存在：暂存在 trigger user 消息。

- [ ] **Step 5: 给三类任务内报告接入 Sink**

`reporting/builders.py` 和 `selection/analysis.py` 的模型函数增加：

```python
thinking_sink: ThinkingSink | None = None
```

`_TaskArtifacts` 在自动报告调用前从 task trigger message 读取 turn_id：

- brand：label“正在生成品牌报告”，带 task_id/goal_id。
- campaign：label“正在生成活动报告”，带 task_id/goal_id。
- auto KOL：label“正在生成KOL报告”，带 task_id。
- `build_goal_result_summary` 保持无 Sink。
- 手动 reporting/selection router 不传 Sink。

- [ ] **Step 6: 让任务结论和错误消息吸收 pending blocks**

`write_conclusion_message` 创建或命中现有结论时调用
`ThinkingMessageStore.attach_turn_to_assistant`。

`TaskRepository._append_error_message` 创建或命中错误消息时执行相同归并。该文件已有用户修改，
先查看当前 diff，只在 `_append_error_message` 及必要 import 上增量修改。

恢复重放要求：

- 现有 assistant 消息命中时仍执行归并。
- 相同 block 不重复。
- 归并失败记录 warning，不改变任务终态。

- [ ] **Step 7: 运行业务接线测试**

Run:

```bash
cd backend
.venv/bin/pytest \
  tests/brainstorm/test_brainstorm.py \
  tests/goals/test_planner.py \
  tests/tasks/test_enforce_create_task.py \
  tests/tasks/test_agent_loop.py \
  tests/tasks/test_goal_lifecycle.py \
  tests/tasks/test_multi_goal.py \
  tests/reporting/test_builders.py \
  tests/selection/test_analysis.py -q
```

Expected: PASS。

- [ ] **Step 8: 提交后端业务接线**

```bash
git add backend/app/brainstorm backend/app/goals/planner.py backend/app/tasks backend/app/reporting/builders.py backend/app/selection/analysis.py backend/tests
git commit -m "feat: 将用户可见模型调用接入思考流"
```

---

### Task 6: 前端会话思考流状态

**Files:**
- Create: `src/api/sessionThinking.ts`
- Create: `src/api/sessionThinking.test.ts`
- Create: `src/state/sessionThinking.ts`
- Create: `src/state/sessionThinking.test.ts`
- Create: `src/hooks/useSessionThinkingStream.ts`
- Create: `src/hooks/useSessionThinkingStream.test.tsx`
- Modify: `src/api/contracts.ts`
- Modify: `src/types.ts`

**Interfaces:**
- Produces: `ThinkingBlock`、`ThinkingMetadata`、`SessionThinkingEvent`
- Produces: `streamSessionThinking(sessionId, signal, onEvent)`
- Produces: `reduceSessionThinking(state, event)`
- Produces: `useSessionThinkingStream(sessionId) -> SessionThinkingRuntime`

- [ ] **Step 1: 增加前端契约和 reducer 失败测试**

在 `contracts.ts` 定义与后端 metadata 一致的 snake_case API 类型；在 `types.ts` 定义前端 camelCase
类型。

Reducer 测试：

```typescript
it('merges started, delta, snapshot and completed by operation plus attempt', () => {
  let state = initialSessionThinking('session-1');
  state = reduceSessionThinking(state, event('thinking.started', {
    operation_id: 'op-1', turn_id: 'turn-1', attempt: 1, label: '正在分析数据',
  }));
  state = reduceSessionThinking(state, event('thinking.delta', {
    operation_id: 'op-1', turn_id: 'turn-1', attempt: 1, text: '分析品牌',
  }));
  state = reduceSessionThinking(state, event('thinking.snapshot', {
    operation_id: 'op-1', turn_id: 'turn-1', attempt: 1, text: '分析品牌和平台',
  }));
  state = reduceSessionThinking(state, event('thinking.completed', {
    operation_id: 'op-1', turn_id: 'turn-1', attempt: 1, duration_ms: 21808,
  }));

  expect(state.byTurn['turn-1'][0]).toMatchObject({
    content: '分析品牌和平台',
    status: 'completed',
    durationMs: 21808,
  });
});
```

另测 failed、不同 session、重复 snapshot、乱序旧 sequence 被忽略。

- [ ] **Step 2: 实现前端 reducer 并通过测试**

Run: `npm run test -- src/state/sessionThinking.test.ts`

Expected: PASS。

- [ ] **Step 3: 写 SSE API 失败测试**

复用 `parseSseStream`，断言：

- `event: thinking.delta` 映射类型正确。
- 多行 data 正确 JSON 解析。
- 非 2xx、无 body、非法事件抛稳定错误。
- 请求使用 `authorizedFetch`，Accept 为 `text/event-stream`。

- [ ] **Step 4: 实现 `streamSessionThinking`**

```typescript
export async function streamSessionThinking(
  sessionId: string,
  signal: AbortSignal,
  onEvent: (event: SessionThinkingEvent) => void,
): Promise<void> {
  const response = await authorizedFetch(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/events`,
    { signal, headers: { Accept: 'text/event-stream' } },
  );
  if (!response.ok || !response.body) throw new Error(`SSE_${response.status}`);
  await parseSseStream(response.body, raw => onEvent(toSessionThinkingEvent(raw)));
}
```

- [ ] **Step 5: 写 hook 重连与切换失败测试**

使用 fake `streamSessionThinking`：

- 首次连接更新 `connection='connected'`。
- 异常后指数退避重连。
- sessionId 切换 abort 旧连接并清空运行态。
- completed blocks 在当前运行态保留，直到历史 metadata 替换。

- [ ] **Step 6: 实现 hook 并运行前端流状态测试**

Run:

```bash
npm run test -- \
  src/api/sessionThinking.test.ts \
  src/state/sessionThinking.test.ts \
  src/hooks/useSessionThinkingStream.test.tsx
```

Expected: PASS。

- [ ] **Step 7: 提交前端流状态**

```bash
git add src/api/sessionThinking.ts src/api/sessionThinking.test.ts src/state/sessionThinking.ts src/state/sessionThinking.test.ts src/hooks/useSessionThinkingStream.ts src/hooks/useSessionThinkingStream.test.tsx src/api/contracts.ts src/types.ts
git commit -m "feat: 接收会话级模型思考事件"
```

---

### Task 7: 折叠思考面板与 turn 交互

**Files:**
- Create: `src/components/ThinkingPanel.tsx`
- Create: `src/components/ThinkingPanel.test.tsx`
- Modify: `src/api/brainstorm.ts`
- Modify: `src/api/tasks.ts`
- Modify: `src/api/sessions.ts`
- Modify: `src/hooks/useWorkspace.ts`
- Modify: `src/hooks/useWorkspace.test.tsx`
- Modify: `src/components/ChatArea.tsx`
- Modify: `src/components/ChatArea.test.tsx`

**Interfaces:**
- Consumes: Task 6 的 `useSessionThinkingStream`
- Produces: `createTurnId() -> string`
- Produces: `ThinkingPanelProps`
- `Message` 增加 `turnId?: string`、`thinking?: ThinkingMetadata`

- [ ] **Step 1: 写请求 turn_id 失败测试**

更新 API 单测，断言：

```typescript
await postBrainstorm('session-1', '分析品牌', 'turn-1');
expect(fetchBody()).toEqual({ content: '分析品牌', turn_id: 'turn-1' });

await createTask('session-1', { content: '分析品牌', turn_id: 'turn-1' }, 'idem-1');
expect(fetchBody()).toEqual({ content: '分析品牌', turn_id: 'turn-1' });
```

`toMessage` 测试断言 `metadata.turn_id`、`metadata.thinking` 映射到 Message。

- [ ] **Step 2: 实现 turn API 和 DTO 映射**

`createTurnId` 优先使用 `crypto.randomUUID()`，无该 API 时使用与现有幂等键相同的安全回退格式。

`useWorkspace.appendMessage` 在调用开始生成一个 turnId：

- Brainstorm 和 Task 都先插入带 turnId 的乐观 user message。
- Brainstorm/Task 请求发送相同 turnId。
- Task 成功后用 `trigger_message_id` 替换乐观消息 ID，并补 taskId。
- Planner clarify 保留乐观 user 消息并追加 assistant，不再生成第二条临时 user 消息。
- 请求失败后调用 `getSession(sessionId)`；若后端已持久化错误 turn，则以服务端会话替换本地状态，否则移除乐观消息。

- [ ] **Step 3: 写 `ThinkingPanel` 失败测试**

```tsx
it('is expanded while running and collapses after completion', async () => {
  const { rerender } = render(
    <ThinkingPanel blocks={[runningBlock('分析品牌')]} />,
  );
  expect(screen.getByText('分析品牌')).toBeVisible();

  rerender(<ThinkingPanel blocks={[completedBlock('分析品牌', 21808)]} />);
  expect(screen.getByRole('button', { name: '已思考 21.8 秒' })).toBeVisible();
  expect(screen.queryByText('分析品牌')).not.toBeVisible();

  await userEvent.click(screen.getByRole('button', { name: '已思考 21.8 秒' }));
  expect(screen.getByText('分析品牌')).toBeVisible();
});
```

另测：

- failed 标题“思考中断”。
- attempt=2 显示“正在修正输出格式”。
- 多阶段按 label 分组。
- `<img src=x onerror=alert(1)>` 只作为文本显示，不产生 DOM 图片。
- 空 blocks 返回 null。

- [ ] **Step 4: 实现 `ThinkingPanel`**

要求：

- 运行中默认展开。
- 从 running 进入全部终态时自动折叠一次；用户随后手动展开不再被 effect 反复折叠。
- 内容使用 `<pre className="whitespace-pre-wrap">` 或等价纯文本节点。
- button 包含 `aria-expanded`、`aria-controls`。
- interrupted 使用警示色但不覆盖正式错误气泡。

- [ ] **Step 5: 写 ChatArea turn 合并和滚动失败测试**

场景：

1. 运行时事件和 assistant metadata 含相同 `(operationId, attempt)` 时只显示一个 panel。
2. panel 位于对应 user 消息后、assistant 消息前。
3. 没有 think 的 started/completed 空 block 不显示。
4. 用户滚动离开底部后 delta 不调用 `scrollIntoView`。
5. 用户仍在底部时 delta 自动跟随。
6. 切换 session 后旧 operation 不显示。

- [ ] **Step 6: 在 ChatArea 接入 hook 和历史 metadata**

`ChatArea` 内：

```typescript
const thinkingRuntime = useSessionThinkingStream(session.id);
const thinkingByTurn = mergeHistoricalAndRuntimeThinking(
  session.messages,
  thinkingRuntime.byTurn,
);
```

渲染消息时，user 消息气泡后根据 `msg.turnId` 插入一个 `ThinkingPanel`。历史 assistant metadata
通过同 turnId 回填；运行时 block 优先，终态 metadata 到达后按 operation+attempt 替换。

滚动容器增加 ref 和 `isNearBottomRef`，阈值 48px；只有 near-bottom 时响应 message 或 thinking 文本变化。

- [ ] **Step 7: 运行前端交互测试**

Run:

```bash
npm run test -- \
  src/api/brainstorm.test.ts \
  src/api/tasks.test.ts \
  src/api/sessions.test.ts \
  src/hooks/useWorkspace.test.tsx \
  src/components/ThinkingPanel.test.tsx \
  src/components/ChatArea.test.tsx
```

Expected: PASS。

- [ ] **Step 8: 提交前端交互**

```bash
git add src/api src/types.ts src/hooks/useWorkspace.ts src/hooks/useWorkspace.test.tsx src/components/ThinkingPanel.tsx src/components/ThinkingPanel.test.tsx src/components/ChatArea.tsx src/components/ChatArea.test.tsx
git commit -m "feat: 在会话中折叠展示模型思考过程"
```

---

### Task 8: 集成回归、真实响应验证和变更日志

**Files:**
- Modify: `backend/tests/model/test_prompt_logs.py`
- Modify: `backend/tests/brainstorm/test_brainstorm.py`
- Modify: `backend/tests/tasks/test_enforce_create_task.py`
- Modify: `src/hooks/useWorkspace.test.tsx`
- Create: `changelog/2026-07-26.md`

**Interfaces:**
- Consumes: Tasks 1-7 全部接口
- Produces: 可重复的 MiniMax `<think>` 回归测试与项目变更记录

- [ ] **Step 1: 增加真实故障形态回归 fixture**

把日志中已确认的响应形态改写为不含业务数据的 fixture：

```python
MINIMAX_THINK_RESPONSE = (
    "<think>检查当前画像，确认品牌、品类和平台是否齐全。</think>\n"
    '{"assistant_message":"请确认品类","extracted":{"audience":null,'
    '"brand":"Manner","category":null,"goal":"声量和情感趋势",'
    '"kol_filters":null,"period":null,"platforms":[],"region":null},'
    '"question":{"options":["咖啡/现制饮品"],"text":"请选择品类"},'
    '"ready":false,"title_suggestion":"Manner品牌分析"}'
)
```

测试必须证明：

- Brainstorm 返回 200。
- assistant 正文为 JSON 中的 `assistant_message`。
- 思考 block 被持久化且不混入正文。
- prompt log 仍保留 `<think>` 原始响应。

- [ ] **Step 2: 增加 Sink 故障隔离回归**

使用每个方法都抛 `RuntimeError("sink down")` 的 Sink：

- Brainstorm 仍成功。
- GoalPlanner 仍创建正确 Goal。
- Agent Loop 仍执行既有 decision。
- 报告仍生成。

- [ ] **Step 3: 运行后端定向检查**

Run:

```bash
cd backend
.venv/bin/ruff check app tests
.venv/bin/pytest \
  tests/model \
  tests/thinking \
  tests/brainstorm \
  tests/goals \
  tests/tasks \
  tests/reporting \
  tests/selection -q
```

Expected: ruff 退出 0；所有定向 pytest 通过。

- [ ] **Step 4: 运行后端全量测试**

Run: `cd backend && .venv/bin/pytest -q`

Expected: 全部通过；如果 `tests/integration/test_real_providers.py` 仍因本地模型名硬断言失败，必须单独记录实际输出，且不得把该历史断言误归因于本功能。

- [ ] **Step 5: 运行前端全量检查**

Run:

```bash
npm run test
npm run lint
npm run build
```

Expected: Vitest、`tsc --noEmit`、Vite production build 全部退出 0。

- [ ] **Step 6: 使用本地真实供应商做单次受控验证**

使用后端当前 `.env`，不打印 base URL、密钥或完整 prompt：

1. 重启后端。
2. 登录开发账号并创建空白会话。
3. 在前端发送“分析 Manner 最近3个月在各平台的声量变化和用户情感趋势”。
4. 浏览器网络面板确认 `/sessions/{id}/events` 为 200 且收到 `thinking.delta`。
5. 确认 Brainstorm 请求不再因 `<think>` 返回 502。
6. 完成后确认面板自动折叠，刷新后仍能展开。
7. 查询 `model_prompt_logs` 只核对 model/status/error_code，不输出 response 或凭证。

- [ ] **Step 7: 写变更日志**

`changelog/2026-07-26.md` 必须记录：

- MiniMax-M3 `<think>` 导致 `MODEL_PLAN_INVALID` 的根因。
- JSON 提取器和流式/非流式兼容策略。
- Session SSE、turn_id、metadata.thinking 契约。
- 允许与禁止展示的 purpose。
- 脱敏、限长、断线和失败隔离行为。
- 后端/前端验证命令与实际通过数量。
- 尚存限制：后端进程在未完成 operation 中途重启时，未持久化 delta 无法恢复。

- [ ] **Step 8: 检查 diff 和提交最终回归**

Run:

```bash
git diff --check
git status --short
```

只暂存本功能文件；确认没有覆盖任务开始前的用户修改。

```bash
git add changelog/2026-07-26.md backend/tests/model/test_prompt_logs.py backend/tests/brainstorm/test_brainstorm.py backend/tests/tasks/test_enforce_create_task.py src/hooks/useWorkspace.test.tsx
git commit -m "test: 覆盖模型思考流端到端行为"
```

---

## Final Verification Checklist

- [ ] MiniMax 风格 `<think>...</think>` + 合法 JSON 不再触发 `MODEL_PLAN_INVALID`。
- [ ] `reasoning_content` 与正文 think 标签都能实时展示。
- [ ] 无 Sink 的后台调用保持非流式且不发布会话事件。
- [ ] Brainstorm、GoalPlanner、Agent Loop、三类任务内报告均使用正确 label。
- [ ] goal summary、followup、quick 和手动报告不创建 thinking operation。
- [ ] 完成和失败 block 都持久化到 assistant `metadata.thinking`。
- [ ] `thinking_pending` 不通过 API 返回。
- [ ] 跨用户、跨会话无法订阅思考事件。
- [ ] Sink/SSE/持久化故障不影响任务、报告和积分。
- [ ] 前端运行中展开，终态自动折叠，刷新后恢复。
- [ ] 后端 ruff、pytest 与前端 Vitest、tsc、build 全部完成。
