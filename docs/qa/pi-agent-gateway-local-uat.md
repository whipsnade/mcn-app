# Pi Agent Gateway 本地 UAT 记录

日期：2026-08-09
范围：方案 B Task 12 本地部分与 Task 13 离线验证
状态：`READY_FOR_REAL_B7_UAT`

## 边界

本记录只覆盖本地 fake topology、事务测试库、FastAPI 代码和 fake-friendly Pi Gateway。未启动历史
Pi RPC/POC 真实六场景 Task 9，未调用真实模型、DataTap、钱包、积分，未执行真实 B7 UAT、生产切流或方案 C。

## 两租户场景

- tenant-a 的声明 backend 为 `pi`，tenant-b 为 `current`；两者使用不同 attempt/source-event 身份。
- kill switch 打开时只把新 Run 解析为 `current`；历史 snapshot 不被修改。
- `effective_runtime_backend` 对未知 backend fail-closed；Pi rollout 需要 active License、兼容 tenant
  config 和健康 Gateway capacity。
- source event 的 `{attempt_id}:{sequence}` 身份跨租户不可混用，工具投影只保留安全字段。

## 自动化证据

Task 12 定向：

```text
backend/tests/pi_gateway/test_runtime_rollout.py
backend/tests/integration/test_pi_gateway_local_uat.py
backend/tests/admin/test_gateway_admin.py
9 passed
```

TenantAdmin backend 切换 red/green 测试加入前端套件；前端全套 **233 passed（31 files）**。
后端全套 **1949 passed、22 skipped**；修改 Python 文件及 `ruff check app tests` 全部通过。
Pi Gateway **56 passed（17 files）**，typecheck/build 通过；Pi Runtime **47 passed（9 files）**，
typecheck 通过；根目录 lint/build 通过（仅既有 chunk size warning）。

本地**组件级** fake topology 由后端事务测试、Pi Gateway fake control plane/worker 测试和前端 fake API
共同驱动；没有启动 FastAPI/Gateway 真实进程、外部 provider，也没有创建真实 round。最终全量复跑产生的 3 个 xlsx 仅作为测试产物移到
`/private/tmp/b0-task13-final-artifacts-r2-XHOL1Q`，未进入工作树；此前首轮 6 个产物也已移出工作树。
后端迁移 head 为 `0041_runtime_usage_constraints`；Pi Gateway 依赖锁定在
`@earendil-works/pi-coding-agent@0.79.10`、`@earendil-works/pi-ai@0.74.2`、
`@earendil-works/pi-tui@0.74.2`、`pi-mcp-adapter@2.20.1`、`typebox@1.3.11`。

## 判定与未授权范围

本地结果只证明路由选择、灰度前置条件、事件身份和 UI 请求边界；不证明真实模型质量、DataTap SLA、
生产网络、真实钱包扣账或 B7 发布。真实 B7 UAT 需另行授权并按运维手册保留 append-only 证据；在此
之前不得把状态改写为 Gate A PASS、B7 PASS 或 production ready。
