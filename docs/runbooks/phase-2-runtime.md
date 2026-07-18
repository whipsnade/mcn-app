# 第二阶段运行手册

本阶段采用异步流式模块化单体。当前部署只使用一个 Uvicorn worker；任务、租约、MCP 调用和积分账本都由 MySQL 持久化，后续再按负载拆分 Worker。

## 启动与迁移

在项目根目录准备未提交的 `.env`，必须设置 `MYSQL_*`、随机 `JWT_SECRET`、`APP_ENV`、`AUTH_MODE`、`TENCENT_PLAN_API_KEY` 与 `DATATAP_MCP_TOKEN`。模型固定使用腾讯 Token Plan 的 `DeepSeek-V4-Pro`，MCP 固定使用 DataTap；不存在 Provider 切换或模拟回退。

```bash
cd backend
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

开发环境只保留 `AUTH_MODE=mock` 的短信与微信登录模拟。模型与 MCP 在所有环境均使用真实供应商；测试前确认密钥有效，且测试日志不得输出模型响应、达人数据、令牌或接口地址。

可用以下命令验证真实配置与供应商连通性：

```bash
cd backend
.venv/bin/python -c 'from app.core.config import get_settings; s=get_settings(); print({"model":"deepseek-v4-pro","datatap_configured":bool(s.datatap_mcp_token.get_secret_value().strip())})'
.venv/bin/pytest tests/integration/test_real_providers.py -q
```

## 运行与恢复

- 发布或维护前将管理开关设为“关闭新任务”；已运行任务允许完成，必要时由任务取消接口终止。
- 进程重启后运行恢复作业扫描过期租约，重新领取可恢复任务；已成功收到远端响应的调用只补结算，不重复调用 MCP。
- `unknown` 调用必须由人工或受控协调流程确认后再结算/释放，禁止凭猜测重复请求。
- 账务排查以 `wallet_ledger` 和 MCP 调用状态为准，核对每次成功工具调用 10 积分；发现差异先冻结新任务，再导出账本与调用证据处理。
- 创建会话后应依次出现 `plan.ready`、真实 MCP 调用和 BI 报表；调用失败的诊断仅保存字段名、字段类型、长度与 Schema 校验路径。
- agent 任务（`kind="agent"`）按迭代循环执行：每轮一个 MCP 调用，`report.updated` 事件先于终态到达，右侧展示自由分析报告。其循环轨迹持久化在 `plan_json`（`agent_trajectory_v1`）；恢复时按轨迹原参数重放未完成的步骤，与 pipeline 一样绝不重发 `unknown` 调用。连续两次非法决策（工具/参数越界）任务直接失败，错误码即校验失败码。

## 回滚

先关闭新任务并等待当前请求进入终态，再回滚应用版本。数据库迁移只按 Alembic 的可逆 downgrade 执行；不要手工删除账本、调用记录或会话历史。回滚后运行一次只读健康检查和 focused 回归，确认租约、积分和版本门控一致后再开放新任务。

## 凭据与日志

日志调用 `app.core.redaction.redact_for_log()` 后再序列化。该函数递归遮蔽授权头、Cookie、手机号、模型/MCP token、JWT 密钥和 MySQL 密码；严禁打印原始请求头、环境变量或完整 Prompt。
