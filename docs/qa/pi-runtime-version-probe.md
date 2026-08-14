# Pi Runtime 版本与 RPC 探针

日期：2026-08-07

## 版本锁定

- Pi 官方当前包：`@earendil-works/pi-coding-agent@0.84.1`。
- `@modelcontextprotocol/sdk@1.30.0`。
- `typescript@7.0.2`、`tsx@4.23.10`、`vitest@4.1.10`。
- 上述版本由 `npm view` 获取，并由 `npm install --ignore-scripts` 写入
  `pi-runtime/package-lock.json`；所有直接依赖均为精确版本。

此前 scope `@mariozechner/pi-coding-agent@0.73.1` 的 npm 元数据提示迁移至
`@earendil-works`，因此没有将弃用 scope 纳入 POC。

官方依据：

- [Pi README](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md)
- [RPC 协议](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)
- [Custom Models](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md)
- [Custom Providers](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/custom-provider.md)

## 无模型 RPC/资源探针

`pi --version` 输出为 `0.84.1`。`pi --help` 确认支持 `--mode rpc`、`--no-session`、
`--no-builtin-tools`、`--no-context-files`、`--no-extensions`、`-e`、`--no-skills`、
`--skill`、`--provider`、`--model` 与 `--thinking`。

探针在隔离临时目录执行，设置 `PI_CODING_AGENT_DIR` 与 `PI_OFFLINE=1`，并以如下资源
边界启动：

```text
pi --mode rpc --no-session --no-builtin-tools --no-context-files \
  --no-extensions -e <临时 extension> --no-skills --skill <临时 skill>
```

stdin 发送的命令为 `get_state` 与 `abort`。stdout 恰有两条 JSONL 记录，均为
`type: "response"`：`get_state: true` 与 `abort: true`；stderr 为 0 字节。
这验证了显式 Extension/Skill 与禁用自动发现可以组合，且无模型 RPC 诊断不会污染
协议 stdout。`get_state` 中工具列表为空，符合 `--no-builtin-tools`。

## 同 Runtime 模型兼容探针

当前后端 Settings 的独立配置文件存在；endpoint 与 API key 均存在。为避免泄露敏感值，
endpoint 仅记录为 `https://api.deepseek.com/…`，模型为 `model:deepseek-v4-flash`，thinking：
`high`。

在另一个临时 `PI_CODING_AGENT_DIR/models.json` 中注册唯一 provider
`kol_insight_pi_poc`，使用当前 Settings 的同一 OpenAI-compatible endpoint 与模型。
API key 只以 `$TENCENT_PLAN_API_KEY` 环境变量引用，不写入文件、不出现在命令或日志中。
模型配置声明 `openai-completions`、`reasoning_effort` compatibility 与 `high` thinking
映射。

`pi --list-models kol_insight_pi_poc` 成功列出 `model:deepseek-v4-flash`（thinking=yes）。随后以
`--mode rpc --no-session --no-builtin-tools --no-context-files --no-extensions --no-skills`
运行一次固定的最小合成 prompt（文本不记录）。结果：

- `get_state`：成功，实际 provider=`kol_insight_pi_poc`、model=`model:deepseek-v4-flash`、
  thinking=`high`。
- `prompt`：成功接受；事件依次为 `agent_start`、`turn_start`、`message_start`、
  `message_end`；stderr=0。
- 供应商的 `message_end` 未提供 usage/token 字段，故 token usage 记录为“上游未返回”，
  未做估算。

结论：Pi 0.84.1 能以当前 Runtime 完全相同的 endpoint/model/thinking 完成最小 RPC
请求；没有换用其他模型。

## Fix round 1：可审计 opt-in 探针

`npm run probe:rpc` 默认不随测试执行。它创建并删除独立临时目录，其中同时放置有效的
自动发现与显式资源 fixture：

- `PI_CODING_AGENT_DIR/extensions/poc-auto-extension.mjs` 与
  `PI_CODING_AGENT_DIR/skills/poc-auto-skill/SKILL.md` 是**不得加载**的自动发现资源。
- `-e <临时 explicit extension>` 与 `--skill <临时 SKILL.md>` 是唯一允许加载的资源。
- 两个 `SKILL.md` 都使用有效 Agent Skills YAML frontmatter（含 `name` 与 `description`）；
  Pi 0.84.1 在 `description` 缺失时只产生日志诊断而不会加载该 Skill。

该脚本使用 `--mode rpc --no-session --no-builtin-tools --no-context-files --no-extensions -e …`
和 `--no-skills --skill …`，向 stdin 发送以下完整的非敏感协议请求：

```json
{"id":"poc-get-state","type":"get_state"}
{"id":"poc-get-commands","type":"get_commands"}
{"id":"poc-abort","type":"abort"}
```

实测摘要：

```json
{
  "stdoutJsonlOnly": true,
  "responseIds": ["poc-get-state", "poc-get-commands", "poc-abort"],
  "explicitResources": ["poc-explicit-extension", "skill:poc-explicit-skill"],
  "resourceShape": [
    {"name":"poc-explicit-extension","source":"extension"},
    {"name":"skill:poc-explicit-skill","source":"skill"}
  ],
  "automaticResourcesAbsent": true,
  "responseShape": [
    {"type":"response","id":"poc-get-state","command":"get_state","success":true},
    {"type":"response","id":"poc-get-commands","command":"get_commands","success":true},
    {"type":"response","id":"poc-abort","command":"abort","success":true}
  ],
  "stderrBytes": 0
}
```

`npm run probe:model` 也是默认 opt-in。调用者在单个已授权 shell 中提供当前 Runtime 环境变量；
脚本不读取或复制 `.env`。它把 `baseUrl`、精确 model 与 thinking 写入权限为 `0600` 的临时
`models.json`，其中 key 始终仅为 `$TENCENT_PLAN_API_KEY` 引用，结束时删除整个临时目录。
它不发送 prompt，只用 `get_state` 与 `abort` 断言实际 provider/model/thinking：

```json
{
  "actualProvider": "kol_insight_pi_poc",
  "actualModel": "model:deepseek-v4-flash",
  "actualThinking": "high",
  "responseShape": [
    {"type":"response","id":"poc-model-state","command":"get_state","success":true},
    {"type":"response","id":"poc-model-abort","command":"abort","success":true}
  ],
  "stdoutJsonlOnly": true,
  "stderrBytes": 0
}
```

两个 opt-in probe 都为 Pi 子进程设置 **10 秒硬超时**；超时会发送 `SIGTERM` 并以
`pi_rpc_probe_timeout` 或 `pi_model_probe_timeout` 失败，从而不会等待模型或进程无限返回。
`probe:model` 本身不发送 prompt，只执行本地 `get_state` 与 `abort`。

覆盖测试扩展为 8 项：增加损坏 JSON、缺少 `type`、response 关联 id/成功形状、自动资源泄漏、
显式资源缺失、伪装资源 source，以及不带 key 值的临时模型配置验证。`npm test && npm run typecheck`
通过。

## 生产 Gateway 锁定组合（方案 B Task 3）

生产 `pi-gateway` 使用已经在 POC 兼容性验证过的精确组合，不把迁移前探针版本当作升级依据：

```text
@earendil-works/pi-coding-agent 0.79.10
@earendil-works/pi-ai             0.74.2
@earendil-works/pi-tui            0.74.2
pi-mcp-adapter                    2.20.1
typebox                           1.3.11
```

Gateway 直接调用 `createAgentSession()`，每个 Run 使用
`SessionManager.inMemory()`、`AuthStorage.inMemory()`、`ModelRegistry.inMemory()` 和
`SettingsManager.inMemory()`；不会创建 `auth.json`、`models.json` 或 settings 文件。内置
read/bash/edit/write/grep/find/ls 与 Pi 自动 Skills/context/Extension discovery 均关闭，只有
受控的 adapter/internal-tool allowlist 可被启用。每个 Worker 使用权限为 `0700` 的临时 cwd/agentDir，
结束时执行 abort、取消订阅、dispose 和递归清理。

本节是生产锁定事实记录，保留上文 Pi `0.84.1` 历史探针，不修改历史探针结果，也不触发模型或
DataTap 调用。
