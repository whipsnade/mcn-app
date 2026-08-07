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
