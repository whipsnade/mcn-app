# Agent Runtime 真实 UAT 逐轮记录

> 由 `backend/tests/integration/test_agent_runtime_real.py` 在每轮 UAT 的
> pytest session 收尾时自动追加（追加不覆盖）。结构化结果见
> `outputs/agent-runtime-uat-results.json`（每轮覆盖，仅保留最新一轮）。

## 2026-08-05T10:36:08 · UAT 轮次

- 环境：APP_ENV=test / DB=kol_insight_test / AUTH_MODE=mock；场景数=2

| 场景 | 状态 | 决策数 | 积分前→后 | 参数摘要 | 调用 | Artifact | 失败阶段/错误码 |
|---|---|---|---|---|---|---|---|
| brand_analysis_parent | completed | 27 | 1000→1000 | 请分析最近一个月瑞幸咖啡的品牌声量和情感表现，并产出正式分析报告。 | match_best_tag:settled(10分)；query_analysis_data:failed/failed_confirmed；social_statistic_overview:failed/failed_confirmed；analysis_target_search:settled(10分)；query_analysis_data:failed/failed_confirmed；social_statistic_trend:failed/failed_confirmed；social_statistic_brand_activity:failed/failed_confirmed；query_raw_posts:failed/failed_confirmed；query_analysis_data:settled(10分)；query_analysis_data:settled(10分)；query_analysis_data:settled(10分)；query_analysis_data:failed/failed_confirmed；query_analysis_data:settled(10分)；query_analysis_data:settled(10分)；query_raw_posts:settled(10分)；query_analysis_data:settled(10分)；social_statistic_trend:unknown/result_unknown | brand_report_v3 v1 restricted lineage_ok | query_analysis_data:failed/failed_confirmed；social_statistic_overview:failed/failed_confirmed；query_analysis_data:failed/failed_confirmed；social_statistic_trend:failed/failed_confirmed；social_statistic_brand_activity:failed/failed_confirmed；query_raw_posts:failed/failed_confirmed；query_analysis_data:failed/failed_confirmed；social_statistic_trend:unknown/result_unknown；unknown call social_statistic_trend keeps reservation but lacks reconciliation audit row (recovery loop not exercised in UAT) |
| insight_drilldown | failed | 23 | 900→900 | 基于已发布的品牌分析报告（parent_artifact_version_id=6f6fe67e-525a-496e-9 | query_analysis_data:failed/failed_confirmed；query_analysis_data:failed/failed_confirmed；query_analysis_data:failed/failed_confirmed；social_statistic_trend:failed/failed_confirmed；calculate_expression:settled(0分)；calculate_expression:settled(0分)；calculate_expression:settled(0分) | — | query_analysis_data:failed/failed_confirmed；query_analysis_data:failed/failed_confirmed；query_analysis_data:failed/failed_confirmed；social_statistic_trend:failed/failed_confirmed |

## 2026-08-05T10:56:12 · UAT 轮次

- 环境：APP_ENV=test / DB=kol_insight_test / AUTH_MODE=mock；场景数=1

| 场景 | 状态 | 决策数 | 积分前→后 | 参数摘要 | 调用 | Artifact | 失败阶段/错误码 |
|---|---|---|---|---|---|---|---|
| brand_analysis_parent | failed | 21 | 1000→1000 | 请分析最近一个月瑞幸咖啡的品牌声量和情感表现，并产出正式分析报告。 | match_best_tag:settled(10分)；query_analysis_data:failed/failed_confirmed；query_analysis_data:settled(10分)；query_analysis_data:settled(10分)；query_analysis_data:settled(10分)；query_analysis_data:settled(10分)；query_analysis_data:failed/failed_confirmed；query_analysis_data:settled(10分)；social_statistic_hot_topic:settled(10分)；query_raw_posts:settled(10分)；social_statistic_hot_topic:unknown/result_unknown；query_analysis_data:unknown/result_unknown；social_statistic_trend:unknown/result_unknown；social_statistic_overview:unknown/result_unknown；social_statistic_trend:unknown/result_unknown | — | query_analysis_data:failed/failed_confirmed；query_analysis_data:failed/failed_confirmed；social_statistic_hot_topic:unknown/result_unknown；query_analysis_data:unknown/result_unknown；social_statistic_trend:unknown/result_unknown；social_statistic_overview:unknown/result_unknown；social_statistic_trend:unknown/result_unknown；unknown call social_statistic_hot_topic keeps reservation but lacks reconciliation audit row (recovery loop not exercised in UAT)；unknown call query_analysis_data keeps reservation but lacks reconciliation audit row (recovery loop not exercised in UAT)；unknown call social_statistic_trend keeps reservation but lacks reconciliation audit row (recovery loop not exercised in UAT)；unknown call social_statistic_overview keeps reservation but lacks reconciliation audit row (recovery loop not exercised in UAT)；unknown call social_statistic_trend keeps reservation but lacks reconciliation audit row (recovery loop not exercised in UAT) |

## 2026-08-05T11:14:58 · UAT 轮次

- 环境：APP_ENV=test / DB=kol_insight_test / AUTH_MODE=mock；场景数=1

| 场景 | 状态 | 决策数 | 积分前→后 | 参数摘要 | 调用 | Artifact | 失败阶段/错误码 |
|---|---|---|---|---|---|---|---|
| brand_analysis_parent | failed | 25 | 1000→1000 | 请分析最近一个月瑞幸咖啡的品牌声量和情感表现，并产出正式分析报告。 | match_best_tag:settled(10分)；query_analysis_data:settled(10分)；query_analysis_data:settled(10分)；social_statistic_overview:settled(10分)；social_statistic_overview:settled(10分)；social_statistic_trend:unknown/result_unknown；query_analysis_data:settled(10分)；social_statistic_hot_topic:settled(10分)；social_statistic_hot_user:settled(10分)；query_raw_posts:settled(10分)；aggregate_metrics:settled(0分)；query_analysis_data:failed/failed_confirmed；social_statistic_trend:unknown/result_unknown；query_analysis_data:settled(10分)；query_analysis_data:settled(10分) | — | social_statistic_trend:unknown/result_unknown；query_analysis_data:failed/failed_confirmed；social_statistic_trend:unknown/result_unknown；unknown call social_statistic_trend keeps reservation but lacks reconciliation audit row (recovery loop not exercised in UAT)；unknown call social_statistic_trend keeps reservation but lacks reconciliation audit row (recovery loop not exercised in UAT) |

## 2026-08-07T02:32:12 · UAT 轮次

- 环境：APP_ENV=test / DB=kol_insight_test / AUTH_MODE=mock；场景数=4

| 场景 | 状态 | 决策数 | 积分前→后 | 参数摘要 | 调用 | Artifact | 失败阶段/错误码 |
|---|---|---|---|---|---|---|---|
| clarification | clarification_requested | 1 | 1000→1000 | 帮我分析一下某个品牌的声量和情感，但我不确定具体要分析哪个品牌。 | 无调用 | — | — |
| brand_analysis | completed | 33 | 1000→1000 | 请分析最近一个月瑞幸咖啡的品牌声量和情感表现，并产出正式分析报告。 | match_best_tag:settled(10分)；query_analysis_data:settled(10分)；query_analysis_data:settled(10分)；query_analysis_data:failed/failed_confirmed；social_statistic_trend:unknown/result_unknown；query_analysis_data:settled(10分)；query_analysis_data:settled(10分)；query_analysis_data:settled(10分)；social_statistic_hot_topic:settled(10分)；query_raw_posts:settled(10分)；social_statistic_hot_user:settled(10分)；query_analysis_data:settled(10分)；aggregate_metrics:settled(0分)；social_statistic_user_profile:settled(10分)；query_analysis_data:settled(10分)；query_raw_posts:settled(10分) | brand_report_v3 v1 restricted lineage_ok | query_analysis_data:failed/failed_confirmed；social_statistic_trend:unknown/result_unknown；unknown call social_statistic_trend keeps reservation but lacks reconciliation audit row (recovery loop not exercised in UAT) |
| campaign_analysis | clarification_requested | 22 | 1000→1000 | 瑞幸咖啡最近有‘9.9咖啡节’活动，请分析这个活动在社交媒体的传播效果。 | social_statistic_brand_activity:unknown/result_unknown；social_statistic_trend:settled(10分)；social_statistic_hot_topic:failed/failed_confirmed；match_best_tag:settled(10分)；social_statistic_trend:unknown/result_unknown；social_statistic_overview:settled(10分)；query_analysis_data:settled(10分)；query_analysis_data:settled(10分)；query_analysis_data:settled(10分)；query_analysis_data:settled(10分)；query_raw_posts:failed/failed_confirmed；query_raw_posts:settled(10分)；query_analysis_data:settled(10分)；query_analysis_data:settled(10分)；query_analysis_data:settled(10分)；query_raw_posts:settled(10分)；aggregate_metrics:settled(0分) | — | social_statistic_brand_activity:unknown/result_unknown；social_statistic_hot_topic:failed/failed_confirmed；social_statistic_trend:unknown/result_unknown；query_raw_posts:failed/failed_confirmed；unknown call social_statistic_brand_activity keeps reservation but lacks reconciliation audit row (recovery loop not exercised in UAT)；unknown call social_statistic_trend keeps reservation but lacks reconciliation audit row (recovery loop not exercised in UAT) |
| campaign_analysis_answer1 | running | 0 | 860→860 | 活动：瑞幸咖啡「9.9咖啡节」；时间范围：最近30天；平台：小红书、抖音。请直接执行分析并产出正式活动报告。 | 无调用 | — | — |

> 本轮中断说明：`campaign_analysis_answer1` 在真实模型供应商多次重连且连续多个
> 决策墙钟未返回后，由操作者发送 SIGINT 结束同一次 pytest 会话，未重跑。pytest
> 已执行 UAT fixture 收尾、写入本表并清理 `uat-*` 隔离用户数据。该场景状态为
> `running`，不应被解释为活动报告 UAT 通过；本轮也未完成达人、钻取、缓存及故障
> 场景的真实验收。
