# 第二阶段运行手册

本阶段采用异步流式模块化单体。当前部署只使用一个 Uvicorn worker；任务、租约、MCP 调用和积分账本都由 MySQL 持久化，后续再按负载拆分 Worker。

## 启动与迁移

在项目根目录准备未提交的 `.env`，至少设置 `MYSQL_*`、随机 `JWT_SECRET`、`APP_ENV` 和 `AUTH_MODE`。生产环境还必须设置 `MODEL_PROVIDER=tencent_plan`、`MCP_PROVIDER=datatap`、`TENCENT_PLAN_API_KEY` 与 `DATATAP_MCP_TOKEN`。

```bash
cd backend
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

开发和测试可使用 `MODEL_PROVIDER=fake`、`MCP_PROVIDER=fake`，不会访问外部供应商。真实供应商只在完成凭据和 manifest 审核后由运维手工执行一次性冒烟。

## 运行与恢复

- 发布或维护前将管理开关设为“关闭新任务”；已运行任务允许完成，必要时由任务取消接口终止。
- 进程重启后运行恢复作业扫描过期租约，重新领取可恢复任务；已成功收到远端响应的调用只补结算，不重复调用 MCP。
- `unknown` 调用必须由人工或受控协调流程确认后再结算/释放，禁止凭猜测重复请求。
- 账务排查以 `wallet_ledger` 和 MCP 调用状态为准，核对每次成功工具调用 10 积分；发现差异先冻结新任务，再导出账本与调用证据处理。

## 回滚

先关闭新任务并等待当前请求进入终态，再回滚应用版本。数据库迁移只按 Alembic 的可逆 downgrade 执行；不要手工删除账本、调用记录或会话历史。回滚后运行一次只读健康检查和 focused 回归，确认租约、积分和版本门控一致后再开放新任务。

## 凭据与日志

日志调用 `app.core.redaction.redact_for_log()` 后再序列化。该函数递归遮蔽授权头、Cookie、手机号、模型/MCP token、JWT 密钥和 MySQL 密码；严禁打印原始请求头、环境变量或完整 Prompt。
