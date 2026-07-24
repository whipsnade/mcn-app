# GoalPlanner 影子模式最终修复报告

- 基线：`980810e`
- 修复日期：2026-07-24
- 范围：修复最终审查中的 1 个 Critical、5 个 Important、文档路径 Minor；
  索引 Minor 按阶段一边界记录取舍，不新增迁移。
- 业务边界：未启用影子模式，未新增业务表、API、MCP 或钱包路径，未实现
  TaskGoal / TaskArtifact。

## 调试与 TDD 方法

开始前完整阅读了 `AGENTS.md`、最终审查发现、实施计划的 Global Constraints 与
Phase 1 Exit Review，以及 `systematic-debugging`、`test-driven-development` 技能。
按项目约束使用 CodeGraph 分别查询：

1. GoalPlanner exemplar 数据流；
2. Planner → prompt 日志 → evaluation 数据流；
3. TaskExecutor 的 interrupted 收尾路径。

CodeGraph 能解析现有 Executor 和共享 exemplar，但其工作树索引未收录本分支新增的
`backend/app/goals/*` 文件；两条指向新增 GoalPlanner 符号的 trace 返回 symbol not
found。随后只对已确定的目标文件和调用方做精确读取，没有用全库 grep 重建调用图。

统一假设与证据如下：

- 适配器的 `status=success` 只证明 `GoalPlannerOutput` Schema 合法，不证明
  `validate_goal_plan` 语义合法；
- Planner 每个 task 最多写两条带 attempt tag 的日志；
- `interrupted` 不在终态集合且可被恢复任务重新领取；
- GoalPlanner exemplar 的学习价值来自目标结构，不需要任何品牌、活动、问题或原文
  实体。

## 发现、根因与修复

### Critical：GoalPlanner exemplar 跨用户且泄露自由文本

根因：

- `find_success_exemplars` 没有 `user_id` 查询条件；
- `GoalPlannerContextBuilder` 未传 task 的用户；
- GoalPlanner 复用了通用 `_decision_fragment`，把 `active_brand`、`question`、
  `params.brand/campaign/requirement`、`request_evidence` 等原文注入下一次模型请求。

修复：

- `find_success_exemplars` 增加 fail-closed 的用户条件；传 `None` 时只匹配
  `user_id IS NULL`，不再扫描其他用户；
- 更新 brainstorm、quick、agent loop、GoalPlanner 四处生产调用方，全部传真实
  `user_id`；
- GoalPlanner 使用专用匿名结构投影，只保留 action、brand_source、布尔存在性、
  goal_type、sequence、依赖、平台数量等结构；
- Prompt 明确 exemplar 只参考匿名结构，不得复制实体；
- 双用户数据库测试验证查询隔离，并断言品牌、活动、手机号、URL、Token、问题和证据
  原文均不出现在 excerpt。

### Important：interrupted 错误触发影子规划

根因：

- `_run_agent_loop` 直接返回 `mark_interrupted()` 的写入成功布尔值；
- `TaskExecutor.run` 把该布尔值解释为“终态已持久化”，从而调用影子 Planner。

修复：

- possibly-sent / unknown 分支仍持久化 interrupted，但固定返回 `False`；
- Executor 回归测试注入真实 shadow fake，断言 interrupted 后影子调用次数为 0。

### Important：第二次语义失败仍被统计为成功或 exemplar

根因：

- 模型适配器在 Planner 的语义校验之前写入 `status=success`；
- evaluation 和 exemplar 只信任数据库状态，没有重新执行当前 Schema 和语义校验；
- 仅按 `status=success` 查询会在最终 attempt 失败时错误回退到先前 attempt。

修复：

- 新增 `app/goals/logs.py` 作为唯一最终语义判定边界；
- 每个 task 先选最大 attempt，再用其 response 执行
  `GoalPlannerOutput.model_validate_json(strict=True)`；
- 从原始 messages 恢复 `current_message`、session brand、account default brand，再执行
  `validate_goal_plan`；
- 最终 Schema/语义失败记为 evaluation 的有效 `invalid`，且整个 task 不进入 exemplar；
- 不改表、不改历史日志状态、不新增迁移。

### Important：评估延迟与 limit 口径错误

根因：

- 原实现只保留最大 attempt 行，丢失首次调用耗时；
- CLI 在 task 级去重前直接 SQL `LIMIT limit`，重试行会挤掉任务样本；
- created_at 为秒级精度时没有稳定 tie-breaker。

修复：

- 每个 task 累加已读取的所有 attempt `duration_ms`，再计算 task 级平均新增延迟；
- CLI 按 `created_at DESC, id DESC` 稳定读取 `2 * limit` 行；
- 汇总完成 task 去重后才截断到 `limit`；
- `task_id IS NULL` 的日志以 `("log", id)` 独立分组，不会互相合并；
- 边界测试覆盖两 attempt、task 级 limit 和无 task_id 独立日志。

### Important：确定性语义校验过宽

根因：

- brand / campaign 只做 Python 真值判断，空白字符串可通过；
- brand_source 没有和 active_brand、session/account 上下文核对；
- 品牌作用域 Goal 的 `params.brand` 不要求与 active brand 一致；
- kol_selection 只要求任意短子串出现在当前消息，“达人表现/达人贡献”也可通过。

修复：

- 所有相关业务字符串先 `strip` 再规范化和校验；
- `validate_goal_plan` 增加可选 `session_brand`、
  `account_default_brand`，保持旧调用兼容；
- Planner 传入真实 session/account 上下文；
- explicit 必须能在当前消息定位；session/account 必须匹配真实上下文和优先级；
  本阶段 account default 为 `None`，因此 account source 无效；
- 所有带品牌的 Goal 必须与 active brand 一致；
- kol_selection 证据必须是当前消息中至少 4 个规范化字符的原文，并包含明确的
  圈选/筛选/推荐/寻找/候选名单动作与达人对象；
- 测试证明“达人表现/达人贡献”拒绝，“推荐下一轮达人/圈选下一轮达人”通过。

### Important：引号式 JSON 凭据未脱敏

根因：

- assignment 正则只允许敏感键后直接出现 `:` / `=`，不允许 `"token"` 或
  `"api_key"` 形式的闭合引号。

修复：

- 正则支持配对的单/双引号键；
- 测试验证 `{"token":"..."}`、`{"api_key":"..."}` 被移除，同时保留
  “品牌 token 化传播策略”“api key 视觉主题”等普通业务文本。

### Minor：UAT Python 路径

根因：

- 运行手册在 `backend/` 下使用 `.venv/bin/python`，但 UAT 虚拟环境位于项目根。

修复：

- 本地命令保留 `backend/.venv/bin/python`；
- UAT 独立命令改为 `/home/kol_insight/backend` 下的
  `../.venv/bin/python`；
- 实际 `--help` 验证又发现直接执行脚本时 `scripts/` 独占模块搜索路径，新增
  subprocess RED 后在脚本入口显式加入 backend 根，文档命令现可直接执行。

### Minor：`(purpose, created_at)` 索引取舍

本阶段不新增索引迁移。理由：

- UAT CLI 是低频、只读人工评估；
- `--limit` 强制为 1–1000，SQL 最多读取 `2 * limit` 行；
- 当前阶段禁止无真实规模证据的迁移；
- 运行手册已记录：待真实日志量、执行计划和延迟证明必要后，再评估
  `(purpose, created_at)` 索引。

## RED / GREEN 证据

所有 pytest 命令均在以下安全前提下运行：只从仓库现有根 `.env` 与
`backend/.env` 加载真实供应商凭据且不输出；显式覆盖
`APP_ENV=test`、`AUTH_MODE=mock`、`MYSQL_DATABASE=kol_insight_test`、
`MYSQL_USER=kol_test`、测试专用密码/JWT；模型名临时固定为测试契约
`deepseek-v4-pro`。

### RED

1. 纯逻辑根因集合：

   ```bash
   pytest -q backend/tests/goals/test_validation.py \
     backend/tests/goals/test_planner.py::test_planner_validates_brand_source_against_session_context \
     backend/tests/goals/test_evaluation.py \
     backend/tests/security/test_log_redaction.py::test_redaction_masks_quoted_json_credentials_without_overmatching_business_text \
     backend/tests/tasks/test_agent_loop.py::test_agent_loop_unknown_call_interrupts_without_report \
     backend/tests/goals/test_prompt_contract.py --tb=short
   ```

   关键输出：`14 failed, 10 passed`。失败分别命中空白/source/动作校验、Planner
   上下文、最终语义状态、limit、JSON 引号脱敏、interrupted 影子调用和 Prompt。

2. 用户隔离与最终 exemplar：

   ```bash
   pytest -q \
     backend/tests/goals/test_context.py::test_context_uses_trigger_message_session_brand_and_user_scoped_exemplars \
     backend/tests/model/test_exemplars.py::test_goal_planner_exemplars_are_user_scoped_and_structurally_anonymous \
     backend/tests/model/test_exemplars.py::test_goal_planner_exemplar_uses_only_final_semantic_success
   ```

   关键输出：`3 failed`；均因 `user_id` 参数未支持或 ContextBuilder 未传递。

3. 文档 CLI 入口：

   ```bash
   pytest -q tests/goals/test_evaluation.py::test_cli_help_runs_as_documented_from_backend_root
   ```

   关键输出：`1 failed`，`ModuleNotFoundError: No module named 'app.goals'`。

### GREEN

1. 对应纯逻辑集合：`24 passed in 0.04s`。
2. 双用户/最终 exemplar 数据库集合：`3 passed in 0.04s`。
3. CLI subprocess：`1 passed`。
4. 最终聚焦回归：

   ```bash
   pytest -q tests/goals tests/model tests/tasks tests/security --tb=short
   ```

   输出：`188 passed in 3.28s`。
5. CLI：

   ```bash
   python scripts/evaluate_goal_planner_shadow.py --help
   ```

   输出含 `usage: evaluate_goal_planner_shadow.py [-h] [--limit LIMIT]`。
6. 范围 Ruff：`All checks passed!`。

## 完整验证

### backend pytest

```bash
pytest -q
```

结果：`499 passed, 4 warnings in 21.74s`。

4 条 warning 均为既有 FastAPI / Starlette
`HTTP_422_UNPROCESSABLE_ENTITY` 弃用提示，位于 quick 路由测试，与本修复无关；
没有失败、错误或跳过。

### 完整 Ruff

```bash
ruff check --no-cache app tests scripts
```

结果：`All checks passed!`。

### 其他

- `git diff --check`：通过；
- 数据库明确锁定 `kol_insight_test`，未写开发库；
- 未运行迁移，未新增迁移文件；
- 未启用 `GOAL_PLANNER_SHADOW_ENABLED`。

## 变更文件

生产代码：

- `backend/app/brainstorm/service.py`
- `backend/app/core/redaction.py`
- `backend/app/goals/context.py`
- `backend/app/goals/evaluation.py`
- `backend/app/goals/logs.py`（新增）
- `backend/app/goals/planner.py`
- `backend/app/goals/validation.py`
- `backend/app/model/exemplars.py`
- `backend/app/model/prompts.py`
- `backend/app/quick/agent.py`
- `backend/app/tasks/dependencies.py`
- `backend/app/tasks/executor.py`
- `backend/scripts/evaluate_goal_planner_shadow.py`

测试：

- `backend/tests/goals/test_context.py`
- `backend/tests/goals/test_evaluation.py`
- `backend/tests/goals/test_planner.py`
- `backend/tests/goals/test_prompt_contract.py`
- `backend/tests/goals/test_validation.py`
- `backend/tests/model/test_exemplars.py`
- `backend/tests/security/test_log_redaction.py`
- `backend/tests/tasks/test_agent_loop.py`

文档：

- `docs/runbooks/phase-2-runtime.md`
- `.superpowers/sdd/final-fix-report.md`

## 自审

- 已检查 `find_success_exemplars` 的全部生产调用方：brainstorm、quick、agent loop、
  GoalPlanner 均传真实 user_id；
- 已检查 `validate_goal_plan` 的全部调用方：旧两参数调用仍兼容，Planner 与日志重判
  传入真实上下文；
- GoalPlanner 匿名 excerpt 不包含 request 原文、品牌、活动、question、requirement、
  evidence、URL、手机号或凭据；
- 最大 attempt 决定最终语义，先前 attempt 不能回退成为成功案例；
- evaluation 的 action / goal_type / brand_source 仅统计最终语义成功任务；
- interrupted 只持久化恢复状态，不会进入影子钩子；
- 稳定排序、task 去重后 limit、无 task_id 独立分组均有测试；
- 影子开关默认值、MCP、钱包、旧 Agent Loop、KOL 沉淀和终态契约未被扩大或改写；
- 没有 Critical / Important 遗留；索引 Minor 按阶段约束明确延期。

## 最终复审追加修复（2026-07-24）

### 根因与最小修复

1. KOL 圈选校验只在 `request_evidence` 内找到“推荐…达人”即通过，未检查该匹配在
   完整消息中的后续宾语，因此截断证据和“达人投放策略”都会误判为圈选。
   修复后把证据匹配映射回完整消息；若达人对象后紧跟投放、内容、营销、传播、
   运营或合作的策略/方案/规划/计划，则拒绝为
   `selection_intent_not_explicit`。真实的“推荐下一轮达人”“圈选达人名单”仍通过。
2. `brand_source=explicit` 只要求 `active_brand` 出现在消息中，“品牌”“该品牌”“它”
   等指代词可被当成真实品牌。修复后显式拒绝
   `品牌/这个品牌/该品牌/它/本品牌`，统一触发
   `brand_source_context_mismatch`，真实品牌“喜茶”保持有效。
3. 自由文本脱敏的引号值模式不识别 JSON 转义，遇到 `\"` 会提前结束匹配并泄露
   后缀。修复后单、双引号值均按转义感知方式完整消费；security 测试与 GoalPlanner
   evaluation 投影测试共同覆盖 password、token、api_key 的转义引号与反斜杠。

### RED / GREEN

- RED：新增的 9 个聚焦用例全部按预期失败：
  `9 failed in 0.12s`。
- GREEN：相同命令在最小实现后全部通过：
  `9 passed in 0.01s`。
- 相关域回归：
  `pytest -q tests/goals tests/security tests/model tests/tasks`，
  结果 `198 passed in 3.22s`。

### 最终验证

- CLI：
  `python scripts/evaluate_goal_planner_shadow.py --help`，
  正常输出 `usage: evaluate_goal_planner_shadow.py [-h] [--limit LIMIT]`。
- 全量后端：
  `pytest -q`，
  结果 `509 passed, 4 warnings in 34.48s`；4 条仍为既有 Starlette 422 弃用告警。
- 完整 Ruff：
  `ruff check --no-cache app tests scripts`，
  结果 `All checks passed!`。
- 所有 pytest 均显式锁定 `kol_insight_test`，只从仓库现有环境文件加载供应商凭据，
  且未输出凭据；未运行迁移，未启用影子模式。
