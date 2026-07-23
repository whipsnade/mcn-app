# 第二阶段运行手册

本阶段采用异步流式模块化单体。当前部署只使用一个 Uvicorn worker；任务、租约、MCP 调用和积分账本都由 MySQL 持久化，后续再按负载拆分 Worker。

## 启动与迁移

在项目根目录准备未提交的 `.env`，必须设置 `MYSQL_*`、随机 `JWT_SECRET`、`APP_ENV`、`AUTH_MODE`、`TENCENT_PLAN_API_KEY` 与 `DATATAP_MCP_TOKEN`。模型为任意 OpenAI 兼容端点（`TENCENT_PLAN_BASE_URL` / `TENCENT_PLAN_MODEL`，当前生产使用 Kimi `k3`）；`TENCENT_PLAN_REASONING_EFFORT`（low/high/max）为可选思考深度，仅 k3 等推理模型生效。MCP 固定使用 DataTap；不存在 Provider 切换或模拟回退。

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
- 创建会话后应依次出现 `plan.ready`、真实 MCP 调用和 `report.updated` 自由分析报告；调用失败的诊断仅保存字段名、字段类型、长度与 Schema 校验路径。
- 所有任务按 agent 迭代循环执行（`kind` 固定 `"agent"`）：每轮一个 MCP 调用，`report.updated` 事件先于终态到达，右侧展示自由分析报告。循环由 `orchestration/bi_requirements.py` 的 8 项 BI 数据项驱动：模型 finish 前服务端做覆盖门禁，缺失数据项回喂补齐（连续被拒 3 次后放行；工具 settled 但返回空视为已满足）。循环不设调用次数上限，仅当钱包可用余额不足一次 10 积分调用时停止，任务进入 `insufficient_balance` 终态；余额不足前已采集证据时仍生成分析报告。其循环轨迹持久化在 `plan_json`（`agent_trajectory_v1`）；恢复时按轨迹原参数重放未完成的步骤，绝不重发 `unknown` 调用。连续两次非法决策（工具/参数越界）任务直接失败，错误码即校验失败码。

## GoalPlanner 影子模式

1. UAT 设置 `GOAL_PLANNER_SHADOW_ENABLED=true` 后重启后端。
2. 影子规划在旧任务进入终态后运行，不调用 MCP、不扣积分；当前尚未创建 TaskGoal 与 TaskArtifact，GoalPlanner 未接管执行，真实任务仍走旧 Agent Loop。
3. 使用以下命令汇总最近 100 条 GoalPlanner 日志；JSON 中的 `current_message` 供人工复核：

   ```bash
   cd backend
   .venv/bin/python scripts/evaluate_goal_planner_shadow.py --limit 100
   ```

4. 人工抽查 `brand_source`、campaign 的 `brand` / `campaign`、`kol_selection` 的 `request_evidence`。
5. 非圈选消息出现 `kol_selection` 时不得进入下一阶段。
6. 紧急关闭：设置 `GOAL_PLANNER_SHADOW_ENABLED=false` 并重启；无需数据库回滚。

## 回滚

先关闭新任务并等待当前请求进入终态，再回滚应用版本。数据库迁移只按 Alembic 的可逆 downgrade 执行；不要手工删除账本、调用记录或会话历史。回滚后运行一次只读健康检查和 focused 回归，确认租约、积分和版本门控一致后再开放新任务。

## UAT 部署

- 连接：`ssh root@111.10.192.19`（密钥用 `~/.ssh` 下默认 id_ed25519/id_rsa，免密已配好；服务器主机名显示为 localhost）。
- 布局：项目根 `/home/kol_insight/`（`backend/` 为 FastAPI 后端、`dist/` 为前端构建产物）；后端 `.env` 在 `/home/kol_insight/backend/.env`；虚拟环境 `/home/kol_insight/.venv`。
- 服务：systemd `kol-insight.service`（WorkingDirectory=`/home/kol_insight/backend`，uvicorn 监听 `127.0.0.1:8100`）；重启 `systemctl restart kol-insight.service`。
- 公网入口：nginx `http://111.10.192.19:40099`（`/api/` 反代到 8100，`/` 静态托管 dist；站点配置在 `/etc/nginx/sites-available/kol-insight`）。
- 同步方式：从本地工作区 `rsync`/`scp` 改动文件到 `/home/kol_insight/backend/`（不覆盖远端 `.env`），有迁移时先 `alembic upgrade head`，再重启服务。
- 验证：本机 `curl http://127.0.0.1:8100/healthz` 应返回 `{"status":"ok"}`；公网用 `curl http://111.10.192.19:40099/api/v1/sessions` 期望 401（证明 nginx→后端链路通，`/healthz` 不在 `/api/` 下、不公网暴露）。
- 注意：远端无 --reload，改代码必须重启服务；云安全组与 ufw 是两层，曾误开 ufw 导致 SSH 断连，端口变更需同时确认两侧放行。

## 凭据与日志

日志调用 `app.core.redaction.redact_for_log()` 后再序列化。该函数递归遮蔽授权头、Cookie、手机号、模型/MCP token、JWT 密钥和 MySQL 密码；严禁打印原始请求头、环境变量或完整 Prompt。
