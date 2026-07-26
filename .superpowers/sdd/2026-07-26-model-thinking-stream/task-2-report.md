# Task 2：`complete_json` 流式适配与 Sink 协议报告

## 改动

- 在 `app.model.contracts` 增加 `ThinkingSink` Protocol，并为 `StructuredModelRequest` 增加可选 `thinking_sink`；无 Sink 时保持原有非流式供应商请求。
- 非流式结构化输出改用 Task 1 的 `parse_non_stream_output` 提取 JSON，日志仍保存供应商原始 content；提取失败继续沿用一次修复后 `MODEL_PLAN_INVALID` 的既有语义。
- 有 Sink 时走 `stream=True` 的结构化流：分离 `delta.reasoning_content` 与内容中的 `<think>`，只对提取出的 JSON 进行严格 Pydantic 验证。
- Sink 生命周期按 regeneration attempt 发送 `started`、`delta`、`completed` 或 `failed`；Sink 任一异常仅写 warning，不影响模型正式结果。
- 明确 400 stream 不支持会按 `(base_url, model)` 缓存能力并降级为非流式，同时一次发布完整 think；可见输出后的流中断不重放请求。
- 流路径传递 `stream_options={"include_usage": True}`、用量与 request ID；`reasoning_effort` 同时覆盖无 Sink 和有 Sink 的结构化请求。

## TDD RED/GREEN

1. RED：`test_complete_json_non_stream_ignores_think_wrapper` 在旧版严格整串验证下失败（首次内容带 `<think>` 后进入修复，测试 fake 输出耗尽）。
   GREEN：接入 `parse_non_stream_output` 后该测试通过。
2. RED：新增 `test_structured_stream.py` 的 7 个用例均因 `StructuredModelRequest` 不接受 `thinking_sink` 失败。
   GREEN：实现协议与流式分支后 7/7 通过。
3. 最终定向回归发现既有 `not-json` 修复用例失败：提取器抛出的 `ValueError` 未进入旧有修复分支。根因是提取发生在 Pydantic 验证前；将其与 `ValidationError` 合并处理后，该回归测试通过。

## 测试结果

```text
TENCENT_PLAN_API_KEY=test-model-token DATATAP_MCP_TOKEN=test-datatap-token \
  /Users/hanxiang/Works/Projects/codex/mcn-app/backend/.venv/bin/pytest \
  tests/model/test_structured_output.py tests/model/test_structured_stream.py \
  tests/model/test_prompt_logs.py tests/model/test_reasoning_effort.py -q

30 passed in 0.07s

ruff check app/model/contracts.py app/model/tencent_plan.py \
  tests/model/test_structured_stream.py tests/model/test_prompt_logs.py \
  tests/model/test_reasoning_effort.py

All checks passed!
```

另已执行 `git diff --check`，无输出（通过）。

## 变更文件

- `backend/app/model/contracts.py`
- `backend/app/model/tencent_plan.py`
- `backend/tests/model/test_prompt_logs.py`
- `backend/tests/model/test_reasoning_effort.py`
- `backend/tests/model/test_structured_stream.py`
- `.superpowers/sdd/2026-07-26-model-thinking-stream/task-2-report.md`

## 自审

- 无 Sink 请求仍显式发送 `stream=False`，且 prompt 日志保留原始响应，不写入清洗后的 JSON。
- 有 Sink 时只发布思考文本；content JSON 不会作为思考 delta 发布，且最终仍通过 `model_validate_json(..., strict=True)`。
- 每次 regeneration 使用 1 起始的 attempt；校验失败、上游异常和中断都会安全调用 `failed`，不会因 Sink 失败遮蔽模型结果。
- 非重试安全边界：任意 content/reasoning 可见后流中断都转换为 `MODEL_STREAM_INTERRUPTED`，不会再次创建供应商请求。
- 作用域仅限模型适配层与单测，未接入会话业务或 SSE。

## 顾虑

- 当前验证使用脚本化 OpenAI 兼容流；真实供应商的 stream 不支持错误体已按 `status_code=400`、`body.error.message/param` 识别，建议在具备真实供应商环境时补一条集成冒烟验证。
