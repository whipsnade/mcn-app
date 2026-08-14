# Marketing Capability Pack B0 设计

## 目标与 Gate

B0 为 Pi POC 提供独立、可版本化、可离线验证的营销业务能力包。A-Infra 已通过，只允许本设计与 B0–B6 的本地开发；A-Business 必须由后续获得明确授权的 append-only 真实业务 round 判定。B7 租户灰度、真实多租户 UAT、生产切流及方案 C 均锁定：只有 B7 稳定一个发布周期且用户明确授权后才可开始。

本阶段不会调用模型、DataTap、钱包或积分，也不会修改历史 round。

## 包边界与版本模型

Capability Pack 位于 `backend/app/marketing_capability_pack/packs/marketing-v1/`：

```text
marketing-v1/
  manifest.json
  policies/root-policy.md
  skills/{social-marketing-analyst,brand-research-report,campaign-evaluation-report,
          kol-selection-report,artifact-drilldown,marketing-strategy}/SKILL.md
  contracts/{brand_report_v3,campaign_report_v3,kol_selection_v3}.json
  evals/{brand,campaign,kol,drilldown,clarification,non_marketing}.json
```

`manifest.json` 是唯一入口，包含 `pack_name=marketing`, `pack_version=1.0.0`、根策略、每个 Skill 的 name/version/SHA-256 digest、必需内部工具、Artifact 类型与 schema 版本、Builder/Exporter 版本和兼容的 runtime contract version `marketing_runtime_v1`。加载器只接受 manifest 中的相对文件名：先 `resolve()`，再要求其位于 pack 根目录、不是符号链接、且与 digest 相符。manifest、技能正文和所有公开返回值都不得包含 endpoint、密钥、模型 key 或租户私有配置。

Run 快照在 `prompt_snapshot_json["marketing_capability_pack"]` 记录 pack、root policy、enabled skills、builder/schema/exporter、model 与 data gateway 的版本/digest，以及实际已加载 Skill 的 name/version/digest。快照是只读事实，不以 PID、RPC JSONL、MySQL Session、钱包、SSE 或 DataTap 凭证作为输入。

## 三层运行边界

1. Runtime Prompt：每个营销 Run 在受控 system context 直接注入完整 root-policy 正文；它永不通过模型工具发现。
2. Business Skill：模型仅能经 `load_marketing_skill(skill_name, requested_version?)` 读取当前快照中启用的专用 Skill。工具返回 name、version、digest、content、required_tools、artifact_contract；未知、禁用、版本/digest 不匹配、跨 Run/租户和路径式参数全部 fail-closed。Pi 保持 `--no-builtin-tools`，不开放 read、shell、文件、任意 HTTP、bash、edit、write、grep、find 或 ls。
3. Run Context：会话范围、已发布 Artifact Version、Evidence ID、可用内部工具和模型调用由 Runtime 管理。DataTap 错误原样交给模型判断；Harness 不换参、不换工具、不自动重放。

Root policy 固定限定社媒营销；区分报告、钻取、澄清和策略；要求正式产物只能走 Builder/Publication；所有精确数值必须有 Evidence；明确 partial/unavailable 与禁止编造；要求非营销问题以固定范围回复终止，且不调用工具/不发布 Artifact。

## 正式数据流与发布规则

```text
Evidence (同一 Run/Session) -> normalization -> canonical data + field lineage
  -> deterministic Builder -> structured Artifact payload -> Publication Validator
  -> immutable Artifact Version -> same-version Excel / BI
```

模型只能请求 Draft，不能直接写 payload、Excel 或 BI。Builder 将可用 Evidence 映射为 canonical sections；字段以 `{path, availability, evidence_ids, artifact_version_id}` 记录 lineage。Publication 在创建 Version 前验证：每一条结构化数字 claim 的 supporting_paths 必须解析到本版本可用 canonical field；该 field 的 Evidence ID 必须同 Run/Session；unavailable 不得被 narrative 断言；partial 必有 non-empty limitations；任一失败返回可解释反馈且不 publish。自由 Markdown 不用正则作为主验证对象。

品牌和活动 Builder 复用脱敏合成的 DataTap shape fixtures。overview 映射声量，sentiment 映射极性，trend 映射时间点、单位与 lineage，top_posts 按平台可得性保留 title/platform/link；不存在的数据保持 unavailable，绝不补 0。Exporter/BI 只接受已发布 Artifact Version ID。

KOL scope 固化 brand/category、platforms、audience、region、age_range、period、budget、filters、ranking_mode、top_limit、scoring_version。候选必须有 nickname、允许平台、kol_uid 或可验证身份，且不能全部评分维度缺失；严格缺失指标仍为 0，但空对象不构成候选。空名单返回 Builder gaps 而不发布；叙事点名的达人必须在同版 items；同一范围的多份名单只有不同明确 `artifact_type/module` 才可同时正式存在。

## Hard checks、finalizer 与离线回放

`numeric_lineage_complete`、`scope_preserved`、`valid_candidates`、`narrative_grounded`、`drilldown_bound_to_version`、`drilldown_grounded`、`non_marketing_refused`、`clarification_no_tool_call`、`no_duplicate_report` 与 `partial_limitations_complete` 都以结构化 fixture 和 Version 精确检查。任一失败即 `EVALUATED_FAIL`；非报告案例也必须检查其预期行为，不能以“不是报告”通过。

Gate/finalizer 纯数据模型移入 `pi_runtime_poc/gate.py`。`finalize_pi_runtime_poc.py` 只读取 fixture、execution、human-review，单次 append-only 写 summary；不 import Settings、SQLAlchemy、Exporter、FastAPI，不建 Engine，不调用模型、MCP、钱包或积分。子进程无 MySQL/模型/DataTap 环境变量时仍必须运行。

六个离线回放用 fake/recorded Pi event 与脱敏 shape fixture，覆盖品牌、活动、KOL、钻取、澄清和非营销拒答。前三者验证结构化 hard checks，钻取绑定确切 Version 且 DataTap=0，澄清与拒答均为 0 Artifact/0 DataTap。

## 稳定接口、升级与回滚

后续 B1–B6 只依赖 `CapabilityPackLoader.load_manifest() -> CapabilityPackSnapshot`、`load_skill(snapshot, name, version) -> LoadedMarketingSkill`、`build_run_snapshot(snapshot) -> dict`、`validate_publication(payload, evidence, artifact_version_id) -> PublicationResult` 与 `evaluate_offline_round(...) -> GateResult`。这些接口只传值对象；不暴露 Pack 路径或基础设施对象。

新增 pack 以新目录和 semver manifest 发布；既有 Run 永远按其持久化 snapshot 重放。兼容 runtime contract 以 manifest 白名单判断，不兼容版本 fail-closed。回滚仅将新 Run 选择恢复到上一已验证 Pack；已发布 Artifact 和历史 snapshot 不改写。

## 自审

设计覆盖双 Gate、目录与版本、三层边界、root policy、受控技能、Builder/Validator/Publication、证据数据流、Run snapshot、B1–B6 接口、B7/C 锁定和升级/回滚。无数据库迁移、外部 API 或 Artifact schema 的重大未决选择；所有新字段先保存在既有 JSON snapshot/payload 边界内。
