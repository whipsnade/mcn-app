# Task 3 实施报告：会话思考 Broker、脱敏和 SSE

## 状态

已完成任务 3。实现范围严格限制为 thinking contracts、脱敏纯函数、进程内
Broker/Sink、会话 SSE 路由及 API 注册；未接线 brainstorm、GoalPlanner 或 Task，
未新增数据库表和运行时依赖。

## 改动

1. 新增 `ThinkingOperationSpec`、`ThinkingBlock` 固定数据契约，以及内部
   `ThinkingEvent` SSE 事件契约。
2. 新增公开思考脱敏：
   - 严格按 Bearer/JWT/API key → 系统提示词段 → JSON Schema 段 → 长度限制执行；
   - 单 block 最多 12,000 字符，截断文本含固定中文后缀；
   - Schema 使用字符串感知的括号平衡扫描，保留 Schema 后面的公开思考。
3. 新增 `SessionThinkingService` / `SessionThinkingSink`：
   - 运行期原始思考只在进程内 `_RunningOperation.raw_text` 存在；
   - 每个 delta 对累计原文重新脱敏，安全前缀稳定时发差量，否则发完整 snapshot；
   - 重连订阅先获得当前运行 snapshot；
   - 慢消费者队列满时清理陈旧事件并压缩为最新 snapshot；
   - completed/failed 生成只含脱敏文本的 `ThinkingBlock`，移除运行快照；
   - 同 turn 的 completed block 公共文本总量限制为 30,000 字符；
   - turn owner 由 user/session 双键隔离，`bind_turn` 可补充 task/trigger message；
   - Sink 内部异常被隔离，不反向改变模型调用结果；取消异常保持向上传播；
   - 失败事件的 `error_code` 也经过公共脱敏。
4. 新增 `GET /api/v1/sessions/{session_id}/events`：
   - 使用 `CurrentUser`；
   - 流建立前按 `WorkspaceSession.id/user_id/deleted_at` 查询 owner，越权统一 404；
   - SSE 使用 `id/event/data` 格式，连接初始及每 15 秒发送 keepalive；
   - 接收但不依赖 `Last-Event-ID`，重连权威状态来自 snapshot；
   - 流结束或取消时在 `finally` 中 unsubscribe；
   - 设置 `Cache-Control: no-cache` 和 `X-Accel-Buffering: no`。
5. 在 `backend/app/api/router.py` 将 thinking 路由注册到 `/sessions`。

## TDD RED / GREEN

### 脱敏

- RED：
  `pytest tests/thinking/test_sanitizer.py -q`
  → `3 failed`，均因 `sanitize_thinking` 尚未实现。
- GREEN：
  同命令 → `3 passed in 0.01s`。

### Broker / Sink

- RED：
  `pytest tests/thinking/test_service.py -q`
  → `7 failed`，均因 contracts/service 尚未实现。
- GREEN：
  同命令 → `7 passed in 0.02s`。

### SSE 路由

- RED：
  `pytest tests/thinking/test_router.py -q`
  → `2 failed`：未注册路由返回框架 `Not Found`，且 thinking router 不存在。
- GREEN：
  同命令 → `2 passed in 0.10s`。

### 自审发现的错误码公共泄露

- RED：
  `pytest tests/thinking/test_service.py::test_failed_event_sanitizes_untrusted_error_code -q`
  → `1 failed`，失败 payload 可观察到 `secret-error-code`。
- GREEN：
  同命令 → `1 passed in 0.01s`。

## 最终验证

所有 pytest 命令均使用：

```bash
DATATAP_MCP_TOKEN=test-datatap-token \
  /Users/hanxiang/Works/Projects/codex/mcn-app/backend/.venv/bin/pytest ...
```

最终新鲜验证：

```text
ruff check app tests
All checks passed!

pytest tests/thinking/test_sanitizer.py tests/thinking/test_service.py \
  tests/thinking/test_router.py -q
13 passed in 0.12s

pytest -q --ignore=tests/integration/test_real_providers.py
674 passed, 4 warnings in 20.25s

git diff --check
exit 0
```

也执行了未排除文件的完整 `pytest -q`：

```text
675 passed, 2 failed, 4 warnings in 22.54s
```

两项失败都位于既有 `tests/integration/test_real_providers.py`，与本任务无关：

1. `test_real_datatap_lists_social_grow_tools` 使用
   `DATATAP_MCP_TOKEN=test-datatap-token` 调用真实 DataTap，返回 `unauthorized`。
2. `test_real_tencent_adapter_uses_confirmed_model` 硬编码期待
   `deepseek-v4-pro`，当前环境实际配置为 `MiniMax-M3`。

4 条 warning 均为既有 FastAPI/Starlette 422 常量弃用提示。

## 变更文件

- `backend/app/api/router.py`
- `backend/app/thinking/__init__.py`
- `backend/app/thinking/contracts.py`
- `backend/app/thinking/router.py`
- `backend/app/thinking/sanitizer.py`
- `backend/app/thinking/service.py`
- `backend/tests/thinking/__init__.py`
- `backend/tests/thinking/test_router.py`
- `backend/tests/thinking/test_sanitizer.py`
- `backend/tests/thinking/test_service.py`
- `.superpowers/sdd/2026-07-26-model-thinking-stream/task-3-report.md`

## 自审

- 范围：未修改 brainstorm、GoalPlanner、Task 执行或模型调用点；未新增迁移或依赖。
- 安全：SSE owner 查询含 `deleted_at IS NULL`；completed block 和所有公开文本均脱敏；
  原始累计文本不会进入 block、事件历史或数据库。
- 限额：12k block 与 30k turn 均有真实行为测试；截断后缀计入总长度。
- 重连与背压：重连 snapshot、未来 delta、队列满压缩均有测试。
- 终态：completed/interrupted block、运行快照移除和 owner 隔离均有测试。
- SSE：404 detail 防止“路由不存在也返回 404”的假通过；格式、keepalive、
  `Last-Event-ID` 和 unsubscribe 均有测试。
- 异常：Sink 捕获事件基础设施异常，Task 2 既有
  `test_complete_json_ignores_thinking_sink_exceptions` 同时覆盖模型结果不受影响。
- 代码质量：Ruff 和 `git diff --check` 通过。

## 顾虑与后续

1. Broker 按需求是进程内单例；多 worker 部署时不同进程不会共享实时事件。若未来启用
   多 worker，需要在部署约束中保持会话粘性，或另行设计跨进程 pub/sub。
2. 本任务没有 turn 生命周期清理接口，completed blocks 与 turn binding 会保留至进程
   重启。后续业务接线确定读取/持久化时机后，应在同一生命周期中增加显式释放策略，
   但本任务没有提前扩展该接口。
3. 真实供应商集成测试需要有效 DataTap token，并需消除对固定模型名的环境耦合；
   本任务未修改这些既有测试。
