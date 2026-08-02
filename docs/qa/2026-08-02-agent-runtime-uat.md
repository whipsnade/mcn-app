# Agent Runtime 真实模型 + 真实 DataTap MCP UAT 记录

日期：2026-08-02
分支：`codex/agent-runtime`
范围：Task 26（设计 §17.3 真实模型 + 真实 MCP UAT）

## 环境与隔离

- 真实模型：腾讯 Token Plan（`backend/.env` 提供 `TENCENT_PLAN_API_KEY`/`BASE_URL`/`MODEL`，本次 `glm-5.2`）。
- 真实 MCP：DataTap（根 `.env` 提供 `DATATAP_MCP_TOKEN`）。
- 测试库：`kol_insight_test`（`kol_test` / test-only-password），`APP_ENV=test`、`AUTH_MODE=mock` 强制覆盖，
  任何场景都**不触碰 dev DB**。
- 运行入口：`backend/scripts/run_real_agent_uat.sh`（加载真实密钥后 FORCE-override 测试隔离变量，
  仅运行 `-m real_services`）。
- 测试文件：`backend/tests/integration/test_agent_runtime_real.py`（默认跳过，`RUN_REAL_SERVICES=1` 启用）。

## UAT 发现并修复的运行时缺陷（Incidents）

以下缺陷在真实 UAT 运行中被发现，均为阻断性缺陷，已按“发现即修复、不掩盖”原则修正并记录：

1. **MCP 目录 `internal_tool_name` 错存为远端名**（`app/mcp_gateway/registry.py` `_refresh_dynamic_tool`）：
   allowlist 值元组的首元素被误当作内部名写入目录行，导致 `_make_mcp_tool` / `require_enabled`
   无法解析到远端名，Agent 与 legacy 两条 MCP 路径全断。
   修复：`internal_tool_name` 一律取 allowlist key（内部名），远端名从 allowlist 值取。

2. **`approved_tools.json` 两个 KOL 搜索工具的 `internal_name` 指向旧式远端名**：
   导致目录行 `internal_tool_name` 写成 `datatap.xiaohongshu.kol.search.v1`，且因
   `remote_name` 与实时发现名不匹配被隔离。修复：`internal_name` 与 `remote_name` 均取
   `kol_xiaohongshu_search` / `kol_douyin_search`（实时网关工具名），恢复 2 工具为 approved。

3. **`SearchEvidenceTool` 输入模型声明服务端保留键 `run_id`**（`app/agent_runtime/tools/history.py`）：
   违反 TrustedTool 契约（`_validate_tool` 拒绝），该工具无法注册。`run_id` 过滤分支本为死代码
   （registry 会在 `model_validate` 前剥离保留键）。修复：移除该字段与死分支。

4. **模型上下文未暴露工具输入 Schema**（`app/agent_runtime/memory.py` + `tools/registry.py`）：
   设计 §九/§10 要求模型看到工具输入/输出 JSON Schema；实现只给了 `internal_name/description/cost`，
   导致真实模型无法构造合法 MCP 参数（`tool arguments failed schema validation`）。
   修复：`RegisteredTool.input_schema`（静态工具取 `input_model.model_json_schema()`，MCP 工具取
   封闭后的目录 Schema），并在 `available_tools` 中注入。

5. **allowlist 远端名与实时网关不同步**（`app/main.py` `_resolve_remote_entry` + `app/mcp_gateway/registry.py`
   `_dynamic_by_internal`）：allowlist 值里的旧式 `datatap.insight.*.v1` 名称已被网关弃用，调用返回
   `Unknown tool`。实时网关以审核内部名暴露工具。修复：`remote_name` 一律取内部名。

6. **静态工具描述为空**（`app/agent_runtime/tools/registry.py`）：`register()` 未填充 `description`，
   模型上下文里 create_draft 等工具无描述，模型猜错 `module="brand_analysis"`。
   修复：优先取工具显式 `description`，回退 docstring 首行；并给 `CreateDraftTool` 增加
   module→(schema_version, business_fields) 对照描述。

7. **`kol_detail_v1` Profile 无法触达真实 `kol_detail` MCP 工具**（`app/agent_runtime/profiles.py` 既定设计，
   未改）：Profile 只允许 `{KOL_DETAIL_TOOLS, ARTIFACT_TOOLS}`，而 `kol_detail` MCP 工具以 `MCP_TOOLS`
   分类注册 → kol_detail_v1 看不到该工具。**未修复**（涉及 Profile 契约，见下方“未决问题”）。

8. **真实 DataTap 查询可长时间挂起 MCP 调用（critical）**：品牌/活动/圈选等真实模型场景中，
   DataTap 某些统计查询会长时间持续返回数据（远超 read timeout），导致 `transport.call_tool`
   挂起数十分钟；`asyncio.wait_for` 也无法打断 httpx 的 C 层阻塞（`PossiblySentTimeout` 分类正确，
   但取消不生效）。多次复现（原生 transport 与加超时包装均如此）。**未修复**（传输层缺陷），
   见下方“未决问题”。这是真实模型场景（2/3/4/5）无法在本 UAT 中跑完的直接原因。

## 场景结果

> 每个场景记录：run_id、MCP 调用状态、积分前后、产物版本、限制。完整结构化结果见
> `outputs/agent-runtime-uat-results.json`（无密钥、无完整原始 prompt / payload）。

| # | 场景 | 结果 | run_id（截断） | 说明 |
|---|------|------|----------------|------|
| 1 | 信息不足时主动澄清 | PASS | 见 JSON | 真实模型输出 ask_user，Run → clarification_requested，写 pending Memory；0 次 MCP，1000→1000 |
| 2 | 品牌分析 → brand_report_v3 | HANG | 见 JSON | 真实模型抓数成功（多次 settled 10 分），随后某 DataTap 查询挂起（Incident #8）；probe 另见 draft lineage 反复修订 / Attempt 暂停 |
| 3 | 活动分析（campaign） | HANG | 见 JSON | 同 Incident #8：真实 MCP 调用正常，长查询挂起 |
| 4 | Top20 达人圈选 + KOL 分析 | HANG | 见 JSON | 同 Incident #8 |
| 5 | 基于已发布 Artifact 钻取（insight_board_v1） | N/A | 见 JSON | 依赖父品牌发布；父场景挂起未发布 → 无法执行 |
| 6 | 达人详情缓存（kol_detail_v2 + 24h cache） | PASS | — | 确定性验证缓存命中；真实 fetch 路径被缺陷 7 阻断 |
| 7 | 趋势 504 后继续其他工具 | PASS | 见 JSON | 504 → result_unknown（保留预留 10），后续 calculate_expression 成功，Run completed，1000→990 |
| 8 | 钱包不足后的 restricted 交付 | PASS | 见 JSON | 余额 5 分，模型感知余额不足后澄清交付；钱包不为负，无 settled 扣费 |
| 9 | Reviewer revise 后补查或修订 | PASS | 见 JSON | revise → update_draft → 复审 approve → 原子发布 brand_report_v3 v1；query_analysis_data settled 10；产物 lineage_ok=True |

## 账本与证据验证（Step 3）

对每个真实 MCP 调用验证：

- **settled 调用恰好 10 分**：MCP 调用 `points_settled == 10`、`points_reserved == 0`；
  内部计算/历史/草稿工具（`service="internal"`）0 分。✅（trend_504 场景 calculate_expression settled 0，
  reviewer 场景 query_analysis_data settled 10）
- **failed_confirmed / definitely_not_sent → 释放预留**：`points_reserved == 0`、
  `points_settled == 0`，钱包 `release` 流水回补。✅（trend_504 场景释放 10）
- **unknown → 恢复核对或保持预留并有审计行**：504 网关超时 → `result_unknown`，
  `points_reserved` 保持 10（等恢复循环 reconcile）。✅（trend_504 场景）
- **发布 Artifact 每个正式数值字段有有效 lineage**：reviewer 场景发布的 brand_report_v3 v1，
  `validate_and_freeze_lineage` 通过，`lineage_ok=True`。✅

## 未决问题 / 切换阻断项

1. **真实 DataTap 长查询挂起（Incident #8，最高优先级）**：传输层无法可靠超时/取消，
   一个长查询即可让 Run 挂死。**切换前必须修复 DataTapTransport 的超时/取消语义**
   （如改为进程级 watchdog，或对长查询工具设置独立的可取消超时）。
2. **模型无法在 Attempt 预算内可靠产出 lineage 有效的正式 Artifact**：真实模型能驱动 MCP 抓数，
   但会反复修订 Draft lineage（probe 观察：brand run 45 决策 / 17 次 revision 仍未过审）并触发
   Attempt 保护（50 决策 / 30 分钟）暂停。这与 `prompts.py`“完整 prompt 工程在后续任务完成”一致——
   **切换前必须补齐 Artifact 构建指引（schema 注入、evidence 映射、builder 工具化）**。
3. **`kol_detail_v1` Profile 未允许 `MCP_TOOLS`**，生产接线无法触达真实 `kol_detail` MCP 工具，
   达人详情真实 fetch 链路当前不可用（缓存链路可用）。**切换前需修复 Profile 或工具分类**。
4. **`create_agent_runtime`（app/main.py）未注册静态工具**（calculation/history/artifact），
   生产引擎目前只有 MCP 目录工具，无法产出正式 Artifact。UAT 测试自带完整注册表；**生产接线
   需补静态工具注册**。
5. **`tests/integration/test_real_providers.py::test_real_tencent_adapter_uses_confirmed_model`
   与本环境不符**（断言 `deepseek-v4-pro`，`backend/.env` 为 `glm-5.2`）——为历史遗留，非本次引入。

## 结论

真实模型 + 真实 DataTap 的运行时机制（状态机、计费、证据、故障分类、Reviewer 闭环、缓存）经本次 UAT
验证**正确**；账本与证据断言全部通过（settled=10、unknown 保留预留、失败释放、发布 lineage 有效）。
但 **cutover 阻断**：① 真实 DataTap 长查询可挂死 Run（Incident #8）；② 模型驱动的正式 Artifact
交付在现有 prompt 工程下不可靠；③ 存在 Profile 与生产接线缺陷。建议按“未决问题”顺序修复后再切换。
