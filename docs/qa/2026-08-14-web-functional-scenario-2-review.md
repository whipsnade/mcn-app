# Web Functional Scenario 2 验收复核记录

日期：2026-08-14
性质：单场景功能验收复核记录（不是 B7 PASS、不是生产就绪、不是零偏差 PASS）

```text
Status: MAIN_INTEGRATION_BLOCKED_BY_DIRTY_MAIN_TREE（详见 §6；Scenario 2 复核本身已完成）
Real external calls authorized: NO（本轮为复核与集成任务，未调用真实模型/DataTap/钱包）
Production cutover authorized: NO
Plan C authorized: NO
```

## 1. 执行身份

| 项 | 值 |
| --- | --- |
| round_id | DIRECT_MCP_WEB_S2_1786672664_bab957e2 |
| branch | codex/direct-artifact-skill-contract-repair |
| execution HEAD | bab957e2e16deb32cc3021ef492c187b24e0ccf2 |
| audited baseline | 37be5b67c52764abb0ab38c458197d3827729144 |
| 七线性提交 | 284e4c7 / 45ec465 / 260f5cc / d4ab189 / f15ff5d / 37be5b6 / bab957e |
| 证据目录 | /private/tmp/DIRECT_MCP_WEB_S2_1786672664_bab957e2/（已 reviewer 封口） |

## 2. 功能验收结论（独立复核后）

```text
FUNCTIONAL_SCENARIO_2_PASS_WITH_ACCOUNTING_WARNINGS
```

独立复核（未复述 operator 报告，从数据库与证据直接核验）：

- 唯一业务 Run `152e95ad-6168-42e3-8dc3-2af862ddfb16`（session_analyst_v1，pi），
  Attempt 恰 1，terminal `completed_with_warnings`；utility Run 分开统计。
- 事件 2565 条 sequence 连续单调；`message.completed`（seq 2562）先于唯一
  `run.completed_with_warnings`（seq 2565）。
- Draft → Publication → 不可变 Version（`e540ff81-…`，brand_report_v3，
  data_status=restricted）全部归属当前 tenant/session/run；publish_attempts=2
  （validation_failed → published，结构化错误自纠错生效）。
- BI 与 Excel 绑定同一 Version（v1）；浏览器下载 xlsx 与后端导出缓存 xlsx
  SHA-256 完全一致（c5fb1db7…）。
- 数据库 Evidence 增量为 0（Direct MCP 架构预期事实）；无 mcp_result_v1 /
  Evidence Bridge 回灌。
- 钱包 1000/0 → 830/20：净支出 150 = settled 15 × 10；账本恒等式成立。

## 3. Accounting warning（未处理，如实披露）

- 本轮新增 result_unknown=2（social_statistic_hot_topic、social_statistic_trend，
  error_class=call_failed），reserved=20 保持，未人工 release/settle/reconcile。
- 积分总暴露 170 ≤ 500。
- 与既有独立遗留项 `ACCOUNTING_UNKNOWN_DIAGNOSTIC_REQUIRED` 模式一致；该遗留项
  不阻塞本次功能代码合并 main，但阻塞完整 B7 PASS 与生产切流。

## 4. Reviewer 修正事项（reviewer 封口口径）

1. adapter `event.isError` 只能说明 UI/adapter 层失败，不能单独证明「已收到
   JSON-RPC 响应」或「标准 MCP Tool Error」；两笔 call_failed 保持 unknown/reserved。
2. 旧 manifest.jsonl 的 record_hash 未把 prev_hash 纳入哈希输入，链头不具备
   密码学绑定全部祖先帧的锚定能力；不重写旧帧，以目录级 hashes.sha256 固定封口。
3. 证据中出现的手机号（18610401033 / 18610401034 / 13000000001 等）均为合成
   环境身份（synthetic 测试用户与 UAT 环境 bootstrap 管理员），非真实用户隐私；
   operator 报告所称「手机号形态扫描 0 命中」仅针对运行日志成立。
4. 判定为 PASS_WITH_ACCOUNTING_WARNINGS，不是 B7_PASS / PRODUCTION_READY /
   零偏差 PASS。

## 5. 合并前代码审核（7c40d864..bab957e2）

结论：**APPROVE_WITH_MINORS**（Critical 0 / Important 0 / Minor 4）。

主链不变量全部成立：标准 MCP Tool Result 原样进入模型、无 Evidence Bridge /
mcp_result_v1 恢复、`model_input_schema` 来自 DTO `model_json_schema()` 单一事实源、
服务器确定性组装服务器字段（模型提交被 `server_owned_field_rejected` 拒绝）、
capability pack marketing-v2/1.1.0 digest 自洽（65d28bb1…）且 marketing-v1 未改动、
归属/allowlist/发布门禁未放松、export_cache 的 lineage 透传不可绕过发布门禁、
BI 与 Excel 同读一个不可变 Version、结构化错误回馈不泄漏提交值（include_context=False
+ 凭证形态剥离）、无密钥/DSN/测试库越界。

4 个 Minor（非阻塞，记录为后续清理项）：
- M1 payload_errors.py 截断算法对「末条错误超长」的处理会把首条误截断（输出仍
  有界合法，仅影响自愈信息质量）。
- M2 insight 组装硬编码 `module="brand"`，与 Evidence 路径按父 module 映射不一致
  （语义标签漂移，不影响 Tab 路由）。
- M3 export_cache `_VersionLike` 的 `data_status="complete"` 为死值且缺
  status/validation_json，缓存路径跳过两道防御性检查（路由只读已发布 Version，
  不可利用）。
- M4 跨模块导入私有下划线函数 `_derive_data_status`/`_partial_leaf_paths`
  （code smell）。

## 6. main 集成状态（Task 3）

- main HEAD：33800ab7f5af0f70bd7ff566403e79d6c8ede321；
  merge-base(main, repair) = main HEAD 本身（repair 是 main 的线性后代，8 提交领先）。
- **main 工作树不干净**，且按授权规则不得覆盖、stash 或清理：
  - ` M .codex/config.toml`
  - ` D "datatap_logs_20260718 (1).xlsx"`
  - `?? .omo/`
  - `?? backend/tests/pi_gateway/`
  - `?? outputs/`
- 因此 merge 未执行，状态 `MAIN_INTEGRATION_BLOCKED_BY_DIRTY_MAIN_TREE`。
  恢复路径（需用户处理或另行授权）：由文件所有者确认上述变更的去留
  （.codex/config.toml 与 xlsx 删除是否提交、三个未跟踪目录是否 ignore/移除），
  待 main 工作树干净后重跑 Task 3（非快进 merge，保留历史），再执行合并后验证。

## 7. 尚未完成

- 未执行 main merge（被 main 工作树阻塞）；未 push、未创建 PR、未部署。
- 未执行合并后验证（pytest/Vitest/前端全量）。
- 未进入完整 B7、未生产切流、未进入方案 C、未执行 Scenario 3–7。
- ACCOUNTING_UNKNOWN_DIAGNOSTIC_REQUIRED 与 4 个 Minor 仍为 backlog。

## 8. 边界声明

本文件不含凭证、DSN、完整 MCP payload；手机号均为合成环境身份。本记录不构成
B7 PASS 或生产就绪声明。
