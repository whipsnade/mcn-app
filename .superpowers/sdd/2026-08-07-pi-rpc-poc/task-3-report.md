# Task 3 报告：Python Pi RPC 子进程客户端

## 结论

已完成方案 A 的严格 Pi RPC 子进程客户端；范围仅限 Python 客户端与 fake subprocess 协议测试。
未接入 DataTap、数据库、积分、正式 Runtime、方案 B 或方案 C。

## 文件

- `backend/app/pi_runtime_poc/rpc.py`
- `backend/tests/pi_runtime_poc/test_rpc.py`
- `changelog/2026-08-07.md`

## 红灯

实现前运行聚焦测试，按预期在导入阶段失败：
`ModuleNotFoundError: No module named 'app.pi_runtime_poc.rpc'`。

## 绿灯精确结果

- `cd backend && .venv/bin/pytest --confcutdir=tests/pi_runtime_poc tests/pi_runtime_poc/test_rpc.py -q`：`7 passed in 0.02s`。
- `cd backend && TENCENT_PLAN_API_KEY=<占位> DATATAP_MCP_TOKEN=<占位> .venv/bin/pytest tests/pi_runtime_poc/test_rpc.py -q`：`7 passed in 0.02s`。
- `cd backend && .venv/bin/ruff check app/pi_runtime_poc/rpc.py tests/pi_runtime_poc/test_rpc.py`：`All checks passed!`。
- `git diff --check`：退出码 0。

第二条命令只向既有根 conftest 提供非敏感占位配置；本测试未使用数据库 fixture，未连接数据库。

## 安全隔离

- 进程通过 `asyncio.create_subprocess_exec` 的列表参数启动，未使用 shell。
- 固定关闭 session、内置工具、上下文文件、自动 Extension 与自动 Skill；只加载显式路径。
- 强制离线与跳过版本检查；每个 Run 使用新建的私有 agent 目录，并在 `close` 后删除。
- stdout 只接受严格 LF JSONL；stderr 独立保留有界尾部作诊断。代码、测试、日志与本报告均未写入密钥、令牌或业务完整提示词。

## 偏差

简报的原始 pytest 命令在当前 worktree 直接运行时，既有根 conftest 因缺少外部服务配置而未能初始化；
使用 Task 2 同样的 `--confcutdir` 得到无数据库红绿证据。最终另以仅进程级非敏感占位配置复跑原始命令，得到相同 7 个通过用例，未改变项目文件或连接数据库。

## Commit

`feat: add strict pi rpc subprocess client`（本报告随该独立 Task 3 提交一同入库）。

## 下一步

Task 4 可在本客户端上实现 DataTap MCP Extension；Task 7 必须先调用 `PiPocSettingsGuard.assert_safe`，再创建客户端并映射事件。

## 自审

- 逐项覆盖了简报列出的 correlation、分片、CRLF/U+2028、stderr、非法 stdout、墙钟、abort 与 close。
- Pi 0.84.1 的 `prompt`/`response` id 关联与 Task 1 实测的 `abort` JSONL 命令不存在冲突。
- 变更仅包含 Task 3 客户端、测试、当天日志与 Task 3 报告；没有触碰业务链路或 POC 配置门禁。

---

## Task 3 Fix Round 1（独立审查修复，3 项）

### 结论

独立审查未通过 Task 3 初版（commit 443bde2），列 3 项必修与建议同步修复项。本轮
严格 TDD 修复后重新聚焦验证，暂停在 Task 3，不进入 Task 4。

### 修改文件

- `backend/app/pi_runtime_poc/rpc.py`
- `backend/tests/pi_runtime_poc/test_rpc.py`
- `changelog/2026-08-07.md`

### 修复项

**1. Critical — 环境最小 allowlist（不再整体继承宿主环境）**
- 原 `start()` 用 `os.environ.copy()` 整体传给 Pi 子进程，宿主数据库密码、DataTap
  token、模型 key、内部签名 secret 等会因环境继承进入 Pi。
- 改为新增 `_pi_environment()`：仅从宿主挑选 `_ENV_ALLOWLIST`
  （PATH / LANG / LC_ALL / LC_CTYPE / LANGUAGE / TMPDIR / HOME）非敏感键，合并
  `PiRpcConfig.environment` 显式提供的值，最后注入 Pi 必需键
  （PI_CODING_AGENT_DIR / PI_OFFLINE / PI_SKIP_VERSION_CHECK）。
- 新增回归测试：预置宿主敏感变量（MYSQL_PASSWORD / DATATAP_MCP_TOKEN /
  TENCENT_PLAN_API_KEY / JWT_SECRET / PI_RUNTIME_POC_INTERNAL_SECRET），断言 spawn
  env 中这些键一律不存在；allowlist 内非敏感键仍可用；未显式提供且不在 allowlist
  的宿主变量不进入；Pi 必需键恒被注入。

**2. Important — abort() 不无限等待**
- 原 `abort()` 发送 abort RPC 后 `await self._wait_task` 无限等待，若进程不退出则
  永久挂起。
- 改为 `_await_exit_within()`：发送 abort 后给短暂宽限（`_ABORT_GRACE_SECONDS`），
  未退出时仅操作当前精确子进程 PID 依次 `terminate()` → 再短暂宽限
  （`_TERMINATE_GRACE_SECONDS`）→ 仍未退出才 `kill()`；全程不使用进程名通配或 shell。
- 新增 fake subprocess 测试：普通进程 abort 后不自主退出 → 断言 terminate 被调用且
  abort 有限时返回；stubborn 进程 terminate 后仍不退出 → 断言最终升级 kill 且 abort
  有限时返回。

**3. Minor（建议同步修复）— 永不换行 stdout 上限**
- 原 `_read_stdout()` 对永不换行的 stdout 无限累积 bytes buffer。
- 新增 `_MAX_RPC_RECORD_BYTES`（1 MiB）：单条未终结 record 超限即以
  `PiRpcProtocolError("rpc_record_too_large")` 失败收口，避免 buffer 无限增长。
- 新增测试：永不换行的 stdout 超过上限 → `PiRpcProtocolError`。

### 红灯证据

实现前运行聚焦测试，5 个新测试按预期失败（`AttributeError` 与断言失败）：

```
FAILED test_spawn_env_does_not_inherit_host_secrets
FAILED test_spawn_env_merges_only_explicit_config_environment
FAILED test_abort_terminates_process_that_does_not_exit
FAILED test_abort_escalates_to_kill_when_terminate_lingers
FAILED test_unterminated_record_over_max_bytes_fails_protocol
5 failed, 8 passed in 0.05s
```

### 绿灯命令与准确结果

- `cd backend && .venv/bin/pytest --confcutdir=tests/pi_runtime_poc tests/pi_runtime_poc/test_rpc.py -q`
  → **13 passed in 0.06s**。
- `cd backend && TENCENT_PLAN_API_KEY=<占位> DATATAP_MCP_TOKEN=<占位> .venv/bin/pytest tests/pi_runtime_poc/test_rpc.py -q`
  → **13 passed in 0.06s**（仅进程级非敏感占位配置，未连接数据库）。
- `cd backend && .venv/bin/pytest tests/pi_runtime_poc -q`（带占位配置）
  → **20 passed in 0.07s**（test_rpc / test_auth / test_config 全绿）。
- `cd backend && .venv/bin/ruff check app/pi_runtime_poc tests/pi_runtime_poc`
  → **All checks passed!**。
- `git diff --check`：退出码 0（clean）。

### 安全/数据隔离核对

- 宿主敏感环境变量不再因 `os.environ.copy()` 整体继承进入 Pi 子进程；仅 allowlist
  非敏感键 + 显式配置 + Pi 必需键被注入。
- abort 仅操作当前 `PiRpcClient` 持有的精确子进程 PID，不使用进程名匹配或 shell。
- 代码、测试、日志与本报告均未写入密钥、业务完整提示词；未连接任何数据库。
- 既有正确内容保持：bytes buffer 按 LF `b"\n"` framing（不用 readline）、仅剥离行尾
  CR（不损坏 JSON 字符串内 U+2028）、stdout 只接受严格 JSONL、stderr 保留 64 KiB
  有界 ring buffer、`create_subprocess_exec` 参数列表不经 shell、固定 flags
  （--mode rpc --no-session --no-builtin-tools --no-context-files --no-extensions -e /
  --no-skills --skill）、30 分钟默认墙钟。

### 发现的计划偏差

- 无。三处修复均落在 Task 3 既有范围内，未越界到 Task 4+ 或方案 B/C。

### Commit

- 本轮 commit：`fix: isolate pi rpc subprocess environment and bounded abort`（独立提交，
  仅含 rpc.py / test_rpc.py / changelog，工作树既有未提交文件未混入）。

### 是否允许进入下一 Task

- 否。本报告为 fix round 1，等待独立审查通过后再放行 Task 4。
