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
