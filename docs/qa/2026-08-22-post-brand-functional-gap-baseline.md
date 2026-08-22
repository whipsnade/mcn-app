# post-brand 功能差距冻结基线（Task 1）

日期：2026-08-22。本文档冻结「当前实现 vs 唯一成功 Skill Snapshot」的差距事实，
作为 post-brand 功能实施（Task 2-10）的基线。**本 Task 不执行任何外部真实
模型、DataTap、钱包、Web UAT 或部署。**

## 源事实

- 源 Run：`a04213cf-496f-4993-bcec-090d5c05061c`（本地 `kol_insight_test`，阶段三唯一
  成功 B Run；`run_prefix=a04213cf` 恰好命中一个 Run）。
- manifest digest：`49adbfa6c111b2ae72b606ad56b220d04b68cb57be1d3c9159b6f9d1cf635305`。
- 固化 fixture：`backend/tests/marketing_skills/post_brand_success_skill_manifest.json`
  （schema `post_brand_skill_snapshot_v1`，13,373 字节，文件 sha256
  `f4957cfacb329ea689bde8d0a40c383231f667bb8b3f5f9ad33919380055d2cb`，secret 扫描干净）。
- 显式 source map：`backend/tests/marketing_skills/post_brand_skill_source_map.json`
  （8 个 entry 全部 `__global__`；候选扫描 `multi_candidate=[]`，无一歧义）。

## Skill 身份（source map 每 entry）

| skill | revision | revision_id | scope | content_digest（前 12） |
|---|---|---|---|---|
| social-marketing-analyst | 3 | `4eb2581a-6411-41ca-8bdb-7fb6487d21d0` | `__global__` | `0ba44fbde575` |
| brand-research-report | 2 | `00000000-0048-4000-8000-000000000002` | `__global__` | `d3b71aa5efea` |
| campaign-evaluation-report | 2 | `00000000-0048-4000-8000-000000000003` | `__global__` | `e329a4d4d705` |
| kol-selection-report | 2 | `00000000-0048-4000-8000-000000000004` | `__global__` | `bc58c3b200d3` |
| artifact-drilldown | 2 | `00000000-0048-4000-8000-000000000005` | `__global__` | `e28dde1ae0a2` |
| marketing-strategy | 2 | `00000000-0048-4000-8000-000000000006` | `__global__` | `0727943c9c7e` |
| analysis-report | 2 | `00000000-0048-4000-8000-000000000007` | `__global__` | `66cf11bbc4d2` |
| workbook-export | 2 | `00000000-0048-4000-8000-000000000008` | `__global__` | `93f89b9141e8` |

## 已有能力（当前文件/符号）

- 固化导出：`backend/app/marketing_skills/promotion.py`（candidates/export 两接口 +
  secret fail-closed）；CLI `backend/scripts/export_post_brand_skill_snapshot.py`。
- DB Revision/Activation 注册表（0045/0047/0048/0049）、快照冻结
  （`marketing_skills/snapshot.py` `SkillSnapshotService.resolve_for_new_run`）、
  管理 API 与 Revision 不可变。
- 成功 B Run 验证过的链路：MCP 直通 + 串行闸 + 墙钟看门狗 + failed_confirmed 即时释放
  + 止损纪律 Revision 3 + artifact.* SSE 补发 + `brand_report_v3` 发布/同版 Excel。

## 十个实施缺口（事实表）

| # | 缺口 | 当前事实 |
|---|---|---|
| 1 | production package fallback | `resolve_for_new_run` 以静态 pack skills 为基线合并 DB Activation；DB 缺项时可回退 package 内容，非 fail-closed |
| 2 | rev3 数字化调用上限 | 止损纪律仅是 Revision 3 文案（"连续 2 次/单轮 ≤3"），运行时无 per-Run 数字化调用上限执行 |
| 3 | Pi Result 无 canonical commitment | `pi-gateway/src/mcp-accounting-extension.ts` finalize 只传受限 metadata；无 adapter 自生成的完整 Result canonical hash 承诺，服务端无法把模型报告数值绑定到 settled Tool Result 事实 |
| 4 | Skill/Builder 输入合同未按 Run 版本化 | SkillRevision 无 `model_input_contract_version`；Run Snapshot 不冻结 per-artifact 输入合同，新旧合同无法并存 |
| 5 | direct KOL score 可写 | `agent_artifacts/model_inputs/kol_selection.py`（v1 语义沿袭）接受模型直接提交候选数值/官方分数列 |
| 6 | 通用 fulfillment 去重/必需列可由模型指定 | `analysis_report` 输入的 fulfillment/去重/必需列边界可被模型操纵，无确定性服务端投影 |
| 7 | Skill audit UI 缺失 | `src/components/admin/SkillAdmin.tsx` 无 Active/Previous digest、审计时间线与 global/tenant scope 过滤视图 |
| 8 | clarification 可晚于副作用 | `request_clarification` 无零副作用门：模型可在已外发 MCP/写 Artifact 后再请求澄清 |
| 9 | read-only drilldown 缺失且历史工具默认 Session 级 | 无显式历史 Version 只读 Run；`agent_runtime/tools/history.py` 的 `read_artifact`/`read_tool_result` 默认 Session 范围而非冻结 Version/lineage scope |
| 10 | Workbook limit error 信息不足 | `agent_artifacts/exporters/workbook.py` 技术上限错误缺少结构化限制值/当前值信息（errors.py 未承载） |

## 不执行边界

本 Task 未执行外部真实模型、DataTap、钱包、Web UAT、部署、push 以外任何外部动作；
候选临时文件 `/private/tmp/post_brand_skill_source_candidates.json` 核对后不提交。
