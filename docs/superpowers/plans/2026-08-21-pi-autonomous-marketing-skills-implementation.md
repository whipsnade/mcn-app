# Pi 自主营销 Skill、通用报告与生产热更新实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不放松租户、工具审核、计费、凭证、unknown 与不可变产物边界的前提下，交付可审计的 Skill Revision/Activation/Snapshot/native loading 管理链路，以及 `analysis_report_v1` 与同版 `workbook_v1`，让 Pi 能处理非固定品牌、活动、达人和混合营销需求。

**Architecture:** Skill 内容以不可变数据库 Revision 为唯一生产事实源，Activation 只保存全局/租户/灰度指针；新 Run 在创建事务中解析并持久化 Skill manifest，Pi Gateway 在每个 worker 内把已校验快照原子物化为只读目录，并以 `noSkills: true` + `additionalSkillPaths` 加载。通用报告是强类型 Artifact Version，Workbook 只引用同一 Version 的 Block 和布局，Exporter 从不可变 payload 确定性分页渲染 Excel；标准 Artifact、Direct MCP Result、现有计费和恢复语义保持兼容。

**Tech Stack:** Python 3.11/3.12、FastAPI、Pydantic v2、SQLAlchemy Async、Alembic、MySQL 8、pytest/ruff；TypeScript、Pi SDK、Vitest；React 19、Tailwind CSS、Recharts；openpyxl。

## Global Constraints

- 遵守“开放业务决策，封闭高风险副作用”：Pi 决定是否澄清、工具顺序、分页、输出类型和停止条件；内核只强制隔离、审核、License、计费、durable-before-send、unknown、密钥、真实性、Version 与文件安全。
- 不恢复 MCP Evidence Bridge、`mcp_result_v1`、固定业务工具顺序/次数、required artifact contract 门禁、Top20/Top40 生产业务上限、Corpus/Stage/observation/candidate 门禁或预定义场景匹配门禁。
- Skill Revision 不可原地覆盖/删除；Skill 内容不从 Git 工作树或用户 `.pi/skills` 读取；Root Policy 与 Tool Contracts 不由 Skill 编辑器修改。
- 新 Run 只使用显式快照；激活只影响后续 Run，运行中 Run、Recovery/resume 使用原快照；快照缺失、路径穿越、symlink、未知文件或 digest 漂移必须 fail-closed 且发生在模型/MCP 外发前。
- `result_unknown` 保持预留并禁止自动重放，其他独立分析仍可继续；empty/partial/failed_confirmed/definitely_not_sent 维持现有可信语义。
- `analysis_report_v1` 不设业务行数上限，只设置配置化技术上限；超限分页或拆 Sheet，不静默删行。`workbook_v1` 不复制业务事实，不接受公式、宏、脚本或二进制 xlsx。
- 只运行每个任务的定向 RED→GREEN 测试；修复后只重跑受影响用例；最终候选只运行一次后端、Pi Gateway/Runtime、前端、相关 E2E、Ruff、迁移和 diff/show 检查；不执行真实模型、DataTap、钱包、生产数据库、部署、Corpus Replay、Stage 2A/2B、60-observation Gate 或连续稳定性验证。
- 所有管理写操作必须携带 `Idempotency-Key`、持久化幂等响应并写 `admin_audit_logs`；所有用户 Run/Artifact/Report 查询必须带当前用户、Session 和租户归属条件。
- 每个功能阶段更新 `changelog/2026-08-21.md`，文档使用中文；禁止 reset、restore、checkout 覆盖文件、stash、rebase、amend、强推和历史重写；每个阶段创建线性前进提交。

---

### Task 1: Skill Revision/Activation 数据模型、校验器与迁移

**Files:**
- Create: `backend/app/marketing_skills/__init__.py`
- Create: `backend/app/marketing_skills/models.py`
- Create: `backend/app/marketing_skills/validation.py`
- Create: `backend/app/marketing_skills/repository.py`
- Create: `backend/migrations/versions/0045_marketing_skill_registry.py`
- Modify: `backend/app/db/models.py`
- Test: `backend/tests/marketing_skills/__init__.py`
- Test: `backend/tests/marketing_skills/test_validation.py`
- Test: `backend/tests/marketing_skills/test_repository.py`
- Test: `backend/tests/marketing_skills/test_migration_0045.py`

**Interfaces:**
- `canonical_skill_digest(content: str) -> str`：先统一 UTF-8/LF 规范，再返回稳定 SHA-256；不得把数据库身份字段混入 digest。
- `validate_skill_content(content: str, *, expected_name: str | None, approved_tools: Collection[str]) -> SkillValidationResult`：解析 `---` frontmatter、校验 `name/description/required_tools`、工具白名单、secret/DSN/Bearer/绝对临时路径/越权声明，并返回规范化元数据和错误码；失败不返回可发布 Revision。
- `resolve_active_revisions(db, *, tenant_id: str, skill_names: Sequence[str], environment: str = "production") -> tuple[ResolvedSkillRevision, ...]`：按租户全量指针、租户灰度、全局默认的优先级解析，灰度桶为 `SHA256(tenant_id + "\0" + skill_name) % 100`，同租户同 Skill 稳定。
- `SkillRevision`：`skill_name/tenant_id/revision/content/content_digest/description/required_tools/artifact_contract/created_by/created_at/change_note`，唯一键为 `(tenant_id, skill_name, revision)`，Revision 与内容均不可更新/删除。
- `SkillActivation`：`environment/tenant_id/skill_name/active_revision_id/previous_revision_id/rollout_percent/updated_by/updated_at`，唯一键为 `(environment, tenant_id, skill_name)`，百分比范围 `0..100`。

- [x] **Step 1: Write the failing test**：覆盖合法 frontmatter、缺字段、名称不一致、未知工具、secret/DSN/绝对路径/越权声明、digest 稳定性、灰度边界和 Revision 不可变约束。
- [x] **Step 2: Run test to verify it fails**：`cd backend && .venv/bin/pytest -q tests/marketing_skills/test_validation.py tests/marketing_skills/test_repository.py`；预期因模块、模型和迁移不存在而失败。
- [x] **Step 3: Write minimal implementation**：实现严格模型、无第三方 frontmatter 隐式执行的解析器、固定错误码、稳定桶算法和 0045 新表；迁移内只写入已审核 `marketing-v2` 基线的不可变 Revision 快照，运行期不再依赖包目录读取 Skill 正文。
- [x] **Step 4: Run test to verify it passes**：运行同一组定向测试，并用 `alembic upgrade head`/`alembic downgrade 0044_agent_run_loop_guard` 的隔离迁移测试确认 upgrade/downgrade 不修改历史迁移。
- [x] **Step 5: Update changelog and commit**：追加背景、模型/迁移、RED→GREEN 结果；执行 `git diff --check`，提交 `feat(skills): add immutable revision registry`。

### Task 2: Skill 管理服务、幂等审计与管理 API

**Files:**
- Create: `backend/app/marketing_skills/schemas.py`
- Create: `backend/app/marketing_skills/service.py`
- Create: `backend/app/marketing_skills/router.py`
- Modify: `backend/app/api/router.py`
- Modify: `backend/app/admin/service.py`
- Test: `backend/tests/marketing_skills/test_service.py`
- Test: `backend/tests/marketing_skills/test_api.py`

**Interfaces:**
- `SkillAdminService.create_revision(admin_id, payload, *, idempotency_key) -> SkillRevisionRead`：验证通过后计算 revision、保存内容与 digest；同一幂等键+不同 request hash 返回 409。
- `SkillAdminService.validate(payload) -> SkillValidationRead`：只做轻量 Markdown/frontmatter/tool/secret 校验，不调用模型、DataTap、Corpus 或完整测试。
- `SkillAdminService.activate(admin_id, skill_name, *, revision, tenant_id, rollout_percent, idempotency_key) -> SkillActivationRead`：在一笔事务内锁定指针、保存 previous pointer、幂等记录和审计。
- `SkillAdminService.rollback(...) -> SkillActivationRead`：只能指向已有 previous Revision，回滚本身仍是新幂等管理写操作。
- API：`GET /api/v1/admin/skills`、`GET /api/v1/admin/skills/{skill_name}`、`GET /api/v1/admin/skills/{skill_name}/revisions/{revision}`、`GET /api/v1/admin/skills/{skill_name}/diff?from_revision=&to_revision=`、`POST /api/v1/admin/skills/validate`、`POST /api/v1/admin/skills/{skill_name}/revisions`、`POST /api/v1/admin/skills/{skill_name}/activate`、`POST /api/v1/admin/skills/{skill_name}/rollback`。

- [x] **Step 1: Write the failing test**：测试管理员以外返回 403、租户不存在/归属错误、缺少/重复/冲突 Idempotency-Key、创建 Revision 审计、diff 使用数据库内容、全局/租户/灰度/回滚指针和重复请求回放。
- [x] **Step 2: Run test to verify it fails**：`cd backend && .venv/bin/pytest -q tests/marketing_skills/test_service.py tests/marketing_skills/test_api.py`；预期路由/服务不存在而失败。
- [x] **Step 3: Write minimal implementation**：复用 `AdminIdempotencyRecord` 与 `AdminAuditLog`，所有响应 JSON 仅含非秘密字段；路由统一使用 `AdminUser`，diff 通过 `difflib.unified_diff` 生成，所有归属失败不泄露 Revision 是否存在。
- [x] **Step 4: Run test to verify it passes**：运行同一组测试；额外执行 `backend/.venv/bin/ruff check app/marketing_skills tests/marketing_skills`。
- [x] **Step 5: Update changelog and commit**：记录 API、错误码、幂等/审计结果；提交 `feat(skills): expose audited revision management api`。

### Task 3: 新 Run Skill Snapshot 与恢复不变性

**Files:**
- Create: `backend/app/marketing_skills/snapshot.py`
- Modify: `backend/app/runtime_config/schemas.py`
- Modify: `backend/app/runtime_config/service.py`
- Modify: `backend/app/marketing_capability_pack/runtime.py`
- Modify: `backend/app/agent_runtime/tools/pi_internal_tools.py`
- Modify: `backend/app/agent_runtime/utility.py`
- Modify: `backend/app/agent_runtime/reviewer.py`
- Modify: `backend/app/agent_runtime/kol_detail.py`
- Test: `backend/tests/marketing_skills/test_snapshot.py`
- Test: `backend/tests/agent_runtime/test_runtime_snapshot_skills.py`
- Test: `backend/tests/agent_runtime/test_runtime_wiring.py`

**Interfaces:**
- `SkillManifestEntry`：`name/revision/content_digest/description/required_tools/artifact_contract/content`，Pydantic `extra="forbid", frozen=True`；`SkillManifest` 还保存 `manifest_digest` 和 `source_scope`。
- `SkillSnapshotService.resolve_for_new_run(db, *, tenant_id, base_capability) -> MarketingRunCapability`：只在新 Run 创建时查询 Active 指针，把 DB Revision 组装进 immutable RuntimeConfigSnapshot；缺行、校验失败、digest 漂移直接抛 `skill_snapshot_invalid`。
- `SkillSnapshotService.validate_existing_run(snapshot) -> None`：只验证已有 JSON 快照，不读取当前 Activation；`snapshot_for_child_run` 复制父 Run Skill manifest，resume/recovery 不重新解析。

- [x] **Step 1: Write the failing test**：覆盖激活前/后新 Run 版本切换、同租户灰度稳定桶、运行中 Run 保持旧 digest、resume/recovery 保持旧 manifest、非法 Revision/缺失内容在模型/MCP 前失败，以及动态 Skill 不改变 Root Policy/Tool Contracts。
- [x] **Step 2: Run test to verify it fails**：`cd backend && .venv/bin/pytest -q tests/marketing_skills/test_snapshot.py tests/agent_runtime/test_runtime_snapshot_skills.py`；预期 snapshot 字段和 resolver 不存在而失败。
- [x] **Step 3: Write minimal implementation**：在 `RuntimeConfigSnapshot` 增加冻结的 Skill manifest/digest 字段；`snapshot_for_new_run` 仅对新 Pi Run 注入 DB Revision；existing/child snapshot 只验证持久 JSON。保留 `load_marketing_skill` 对历史快照的兼容读取，但新 Skill 目录不依赖该工具。
- [x] **Step 4: Run test to verify it passes**：运行同一组测试，并验证 `RuntimeConfigSnapshot.model_dump(mode="json")` 不含 API Key、Bearer、DSN、用户身份或任意本机路径。
- [x] **Step 5: Update changelog and commit**：记录 snapshot 不变性与 fail-closed 边界；提交 `feat(runtime): freeze database-backed skill snapshots per run`。

### Task 4: Pi Gateway 原生 Skill 目录物化与显式加载

**Files:**
- Create: `pi-gateway/src/skill-snapshot.ts`
- Modify: `pi-gateway/src/protocol.ts`
- Modify: `pi-gateway/src/main.ts`
- Modify: `pi-gateway/src/resource-loader.ts`
- Modify: `pi-gateway/src/pi-session.ts`
- Modify: `pi-gateway/tests/resource-loader.test.ts`
- Modify: `pi-gateway/tests/sdk-contract.test.ts`
- Create: `pi-gateway/tests/skill-snapshot.test.ts`
- Modify: `pi-runtime/tests/skills.test.ts`

**Interfaces:**
- `materializeRunSkillSnapshot(rootDir: string, entries: readonly SkillSnapshotEntry[]) -> Promise<string>`：在 `rootDir/.skill-snapshot.tmp-*` 写入受控 `<skill-name>/SKILL.md`，拒绝 symlink、绝对路径、`..`、未知文件和名称/digest 漂移，fsync 后原子 rename，目录/文件权限分别 0700/0600。
- `createProductionResourceLoader({ ..., additionalSkillPaths })`：固定 `noSkills: true`、`noContextFiles: true`、`noPromptTemplates: true`、`noThemes: true`，仅把已物化目录传给 `additionalSkillPaths`；Root Policy 仍由 `systemPrompt` 注入。
- `mapClaimRuntimeSnapshot`：校验每个快照条目的 revision/content digest 与 capability pack 正文；缺失或非法时在 worker spawn 前抛 `pi_gateway_claim_snapshot_invalid`。

- [x] **Step 1: Write the failing test**：测试用户级 `.pi/skills`、项目级 `.pi/skills`、cwd 外目录均不加载；显式快照 Skill 可加载；路径穿越/symlink/未知文件/digest 错误/超限 fail-closed；`additionalSkillPaths` 只有当前 Run 目录。
- [x] **Step 2: Run test to verify it fails**：`cd pi-gateway && npm test -- --run tests/skill-snapshot.test.ts tests/resource-loader.test.ts tests/sdk-contract.test.ts`；预期函数和配置字段不存在而失败。
- [x] **Step 3: Write minimal implementation**：在 `createProductionPiSession` 中先物化快照、再 `loader.reload()`、再 `createAgentSession()`；任何物化错误都不创建模型 Session，也不触发 MCP。保留迁移期 `load_marketing_skill` 工具，但新原生 Skill 正文不得把它作为必经依赖。
- [x] **Step 4: Run test to verify it passes**：运行同一组 Vitest；再执行 `npm run typecheck`（若 package script 名称不同，使用 `npx tsc -p tsconfig.json --noEmit`）。
- [x] **Step 5: Update changelog and commit**：记录 `noSkills: true`、显式路径和 spawn 前 fail-closed 证据；提交 `feat(pi): load only immutable run skill snapshots`。

### Task 5: `analysis_report_v1` 强类型模型输入、发布和能力注册

**Files:**
- Create: `backend/app/agent_artifacts/payloads/analysis_report.py`
- Create: `backend/app/agent_artifacts/model_inputs/analysis_report.py`
- Create: `backend/app/marketing_capability_pack/packs/marketing-v2/contracts/analysis_report_v1.json`
- Create: `backend/app/marketing_capability_pack/packs/marketing-v2/skills/analysis-report/SKILL.md`
- Create: `backend/app/marketing_capability_pack/packs/marketing-v2/skills/workbook-export/SKILL.md`
- Modify: `backend/app/agent_artifacts/payloads/__init__.py`
- Modify: `backend/app/agent_artifacts/model_inputs/__init__.py`
- Modify: `backend/app/agent_artifacts/validation.py`
- Modify: `backend/app/agent_runtime/tools/builders.py`
- Modify: `backend/app/agent_runtime/profiles.py`
- Modify: `backend/app/marketing_capability_pack/loader.py`
- Modify: `backend/app/marketing_capability_pack/runtime.py`
- Modify: `backend/app/marketing_capability_pack/packs/marketing-v2/manifest.json`
- Modify: `backend/app/core/config.py`
- Test: `backend/tests/agent_artifacts/test_analysis_report_payload.py`
- Test: `backend/tests/agent_artifacts/test_analysis_report_publish.py`
- Test: `backend/tests/agent_runtime/tools/test_direct_artifact_builder.py`
- Test: `backend/tests/agent_runtime/test_profiles.py`
- Test: `backend/tests/marketing_capability_pack/test_loader.py`

**Interfaces:**
- `AnalysisReportV1`：包含 `schema_version="analysis_report_v1"`、`title`、`subject_type`、`scope`、服务器推导的 `data_status`、唯一 `blocks[].id`、`fulfillment`、`methodology`、`limitations` 和可选 `workbook` 布局；所有模型 `extra="forbid"`。
- Block 判别联合：`metric_cards`、`typed_table`、`time_series`、`link_list`、`chart`、`narrative`、`methodology_limitations`；表格列类型严格为 `string|integer|number|percent|date|datetime|url|boolean`。
- `AnalysisReportV1Input`：模型只提交业务字段；`assemble_analysis_report_payload(input) -> dict` 服务器补齐 `schema_version/module/data_status`、规范化 block id、限制检查和最终 JSON。
- `fulfillment` 每项必须有 `requested_min/actual_count/status/reason`；真实数量不补造，`partial/unavailable` 自动进入限制披露；技术上限由 Settings 注入，不得写死 Top20/Top40。

- [x] **Step 1: Write the failing test**：覆盖每种 Block、表格类型、唯一 ID、URL/公式/秘密拒绝、data_status 从 availability 推导、数量不足保留实际数量、200+ 行不因业务数量失败、服务器字段伪造拒绝、标准 Artifact 映射不回归。
- [x] **Step 2: Run test to verify it fails**：`cd backend && .venv/bin/pytest -q tests/agent_artifacts/test_analysis_report_payload.py tests/agent_artifacts/test_analysis_report_publish.py tests/agent_runtime/tools/test_direct_artifact_builder.py -k 'analysis_report or direct_model'`；预期 Schema、输入 DTO 和 mapping 不存在而失败。
- [x] **Step 3: Write minimal implementation**：把 `analysis_report_v1` 加入唯一 payload/input/allowlist 映射，新增 `module="report"` 的业务身份组装；manifest digest 重新计算并把新 Output Skill 注册；不修改 `marketing-v1`，不改变标准 Artifact 上限语义。
- [x] **Step 4: Run test to verify it passes**：运行同一组定向测试与 `ruff check app/agent_artifacts app/agent_runtime/tools app/marketing_capability_pack tests/agent_artifacts tests/agent_runtime/tools`。
- [x] **Step 5: Update changelog and commit**：记录强类型字段、服务器所有权和无业务行数门禁；提交 `feat(artifacts): add generic analysis report contract`。

### Task 6: `workbook_v1` 同版确定性 Excel 与缓存

**Files:**
- Create: `backend/app/agent_artifacts/exporters/analysis_report.py`
- Create: `backend/app/agent_artifacts/exporters/workbook.py`
- Modify: `backend/app/agent_artifacts/exporters/__init__.py`
- Modify: `backend/app/agent_artifacts/export_cache.py`
- Modify: `backend/app/agent_artifacts/router.py`
- Modify: `backend/app/agent_artifacts/models.py`
- Modify: `backend/app/agent_artifacts/payloads/analysis_report.py`
- Test: `backend/tests/agent_artifacts/test_analysis_report_export.py`
- Test: `backend/tests/agent_artifacts/test_export_cache.py`
- Test: `backend/tests/agent_artifacts/test_brand_export.py`

**Interfaces:**
- `render_workbook_v1(report: AnalysisReportV1, *, exporter_version: str, limits: WorkbookLimits) -> bytes`：只从同一 Version 的 report blocks/layout 渲染，安全写入值/URL，禁止 formula/macro/script/binary input。
- `workbook_layout_digest(layout: WorkbookLayout) -> str`：对规范化 sheet/block/列/分页/显示格式 JSON 做 SHA-256；缓存 key 必须是 `SHA256(version_id + exporter_version + layout_digest)`，避免 Skill 热更新影响历史 Version。
- 支持自定义表头、跨平台统一列、platform 备注、冻结/筛选/排序、单 Sheet 分页、拆 Sheet 和说明区；技术超限返回结构化 `workbook_technical_limit_exceeded`，不静默截断。

- [x] **Step 1: Write the failing test**：同一 Version/Exporter/layout 两次输出 hash 相同；layout 改变不命中旧缓存；40+ 行全部分页保留；跨平台列、URL、公式注入、非法文件名安全；标准 brand/campaign/kol exporter 仍走原缓存键。
- [x] **Step 2: Run test to verify it fails**：`cd backend && .venv/bin/pytest -q tests/agent_artifacts/test_analysis_report_export.py tests/agent_artifacts/test_export_cache.py`；预期 generic exporter/cache key 不存在而失败。
- [x] **Step 3: Write minimal implementation**：在现有 `ExportCacheService` 上增加可选 layout/exporter cache key，不改现有类型默认行为；实现 openpyxl 纯值渲染、链接安全、分页/拆 Sheet 和有界 cell/row/column/file limits；Excel 只读发布 Version。
- [x] **Step 4: Run test to verify it passes**：运行同一组测试并执行标准 `test_brand_export.py` 受影响用例，确认标准导出仍为 409/成功原语义。
- [x] **Step 5: Update changelog and commit**：记录 BI/Excel 同版、缓存键和安全边界；提交 `feat(artifacts): render deterministic workbook projections`。

### Task 7: Pi 正式 Run 澄清、自主错误降级与主报告终态

**Files:**
- Modify: `backend/app/pi_gateway/completion.py`
- Modify: `backend/app/pi_gateway/service.py`
- Modify: `backend/app/agent_runtime/engine.py`
- Modify: `backend/app/agent_runtime/tools/builders.py`
- Modify: `backend/app/agent_runtime/tools/pi_internal_tools.py`
- Modify: `backend/tests/pi_gateway/test_completion_validator.py`
- Modify: `backend/tests/pi_gateway/test_direct_mcp_architecture.py`
- Create: `backend/tests/integration/pi_uat/test_autonomous_marketing_behaviors.py`
- Modify: `backend/tests/integration/pi_uat/harness.py`

**Interfaces:**
- `CompletionValidator` 对正常分析 Run 只要求当前 Run/Session 的顶层已发布主 Artifact（标准 Artifact 或 `analysis_report_v1`），不要求固定类型；`clarification_requested` 不要求报告；child insight 不计为主报告。
- 保持 `standard MCP Tool Result -> Pi` 直通；empty/partial/failed/definitely_not_sent/result_unknown 测试只验证现有分类和结算，不把 Evidence Bridge 重新接入。
- 结构化 Draft 错误继续以字段 path/type/reason/retryable 回喂；Pi 可在一次 unknown 后继续其他独立工具，不自动重放 unknown；明确请求不触发机械 clarification。

- [x] **Step 1: Write the failing test**：先补「无主报告的正常 completed 被拒绝」「clarification 可完成」「standard report/analysis_report 主 Version 通过」「unknown 后独立工具仍可执行且无 replay」「长尾 Excel 走 generic report」用例。
- [x] **Step 2: Run test to verify it fails**：`cd backend && .venv/bin/pytest -q tests/pi_gateway/test_completion_validator.py tests/pi_gateway/test_direct_mcp_architecture.py tests/integration/pi_uat/test_autonomous_marketing_behaviors.py`；RED 为 3 个主报告门禁断言失败，且受限沙箱首次因无可写临时目录未进入 pytest 收集。
- [x] **Step 3: Write minimal implementation**：在统一 CompletionValidator 的 terminal/recovery/force-complete 入口复用同一主 Artifact 查询；只按产物归属与发布状态判断，不增加固定业务调用顺序、工具次数或类型门禁。
- [x] **Step 4: Run test to verify it passes**：同一组定向测试 GREEN 为 20 passed；受影响 terminal gate/direct builder 回归为 13 passed；变更文件 Ruff 通过。
- [x] **Step 5: Update changelog and commit**：记录澄清例外、主报告条件和 unknown 继续分析证据；提交 `feat(pi): enforce generic main-report completion semantics`。

### Task 8: 管理端 Skill 工作台

**Files:**
- Create: `src/api/skills.ts`
- Create: `src/api/skills.test.ts`
- Create: `src/components/admin/SkillAdmin.tsx`
- Create: `src/components/admin/SkillAdmin.test.tsx`
- Modify: `src/components/admin/AdminNavigation.tsx`
- Modify: `src/components/AdminPanel.tsx`
- Modify: `src/api/contracts.ts`

**Interfaces:**
- `listAdminSkills/validateAdminSkill/createAdminSkillRevision/getAdminSkillDiff/activateAdminSkill/rollbackAdminSkill` 与后端 DTO 一一对应，所有写请求显式生成/传递 `Idempotency-Key`。
- `SkillAdmin` 状态包括选中 Skill、Revision 列表、只读 diff、Markdown 编辑、校验结果、tenant/rollout 输入、发布/回滚 loading/error/success；回滚使用现有 `ConfirmDialog`，不调用 `window.prompt` 或 `window.confirm`。

- [x] **Step 1: Write the failing test**：API 测试 URL/body/header；组件测试列表、编辑、校验失败、diff、发布、租户灰度、全量激活、回滚确认和审计字段展示。
- [x] **Step 2: Run test to verify it fails**：`npm test -- --run src/api/skills.test.ts src/components/admin/SkillAdmin.test.tsx`；预期模块和导航项不存在而失败。
- [x] **Step 3: Write minimal implementation**：沿用现有 Admin API/组件样式，新增“营销 Skills”模块，使用受控 textarea/editor、结构化错误列表和显式按钮状态；不把 Root Policy 或 Tool Contracts 暴露为可编辑内容。
- [x] **Step 4: Run test to verify it passes**：运行同一组 Vitest，并执行 `npm run lint`；Vitest 通过，lint 仅剩仓库既有 `pi-gateway` 类型错误和缺失外部模块错误。
- [x] **Step 5: Update changelog and commit**：记录 UI 能力与无 confirm/prompt 证明；提交 `feat(admin): add marketing skill management workspace`。

### Task 9: 通用报告 API 类型、BI 展示与 Excel 入口

**Files:**
- Create: `src/components/artifacts/AnalysisReportView.tsx`
- Create: `src/components/artifacts/AnalysisReportView.test.tsx`
- Modify: `src/api/agentArtifacts.ts`
- Modify: `src/api/agentArtifacts.test.ts`
- Modify: `src/components/artifacts/ArtifactWorkspace.tsx`
- Modify: `src/components/artifacts/ArtifactWorkspace.test.tsx`
- Modify: `src/components/artifacts/urlUtils.ts`

**Interfaces:**
- 前端 `AnalysisReportPayload` 是按 `schema_version="analysis_report_v1"` 的 discriminated union；`TypedTable` 依据列类型渲染，不把 null/restricted 显示成 0；URL 只允许 http/https。
- `ArtifactWorkspace` 新增“通用报告”入口，选择最新已发布 `analysis_report_v1`，与标准 Tab 共用版本选择、未读水位和 `exportArtifact`；Excel 下载显示同一 Version。
- `AnalysisReportView` 支持 metric cards、typed table、time series、link list、chart、narrative、methodology/limitations，未知 block 显示受控降级而非执行任意 HTML/脚本。

- [x] **Step 1: Write the failing test**：覆盖 payload 类型收窄、restricted/partial/unavailable 展示、>20/40 行前端全量显示、跨平台列/链接、通用报告 Tab 和同版本导出调用。
- [x] **Step 2: Run test to verify it fails**：`npm test -- --run src/api/agentArtifacts.test.ts src/components/artifacts/AnalysisReportView.test.tsx src/components/artifacts/ArtifactWorkspace.test.tsx`；预期 union、view 和 Tab 不存在而失败。
- [x] **Step 3: Write minimal implementation**：新增安全通用渲染器和 API DTO，复用现有 report primitives/Recharts，不改变标准 Artifact 视图；在 workspace 仅展示后端已发布版本。
- [x] **Step 4: Run test to verify it passes**：同一组 Vitest 为 43 个文件、316 个测试通过；`npm run lint` 仅剩仓库既有 `pi-gateway` 类型错误和缺失外部模块错误。
- [x] **Step 5: Update changelog and commit**：记录同版 BI/Excel 和数据受限表现；提交 `feat(frontend): render generic analysis reports`。

### Task 10: Native Skill/Report 迁移文档、QA 与每日记录

**Files:**
- Modify: `pi-runtime/src/extensions/internal-tools.ts`
- Modify: `pi-runtime/skills/social-marketing-analyst/SKILL.md`
- Modify: `pi-runtime/skills/brand-research-report/SKILL.md`
- Modify: `pi-runtime/skills/campaign-evaluation-report/SKILL.md`
- Modify: `pi-runtime/skills/kol-selection-report/SKILL.md`
- Modify: `pi-runtime/skills/marketing-strategy/SKILL.md`
- Modify: `pi-runtime/skills/artifact-drilldown/SKILL.md`
- Modify: `pi-runtime/tests/internal-tools.test.ts`
- Modify: `pi-runtime/tests/skills.test.ts`
- Modify: `docs/runbooks/pi-agent-gateway.md`
- Create: `docs/qa/2026-08-21-pi-autonomous-marketing-skills.md`
- Create: `changelog/2026-08-21.md`

**Interfaces:**
- 迁移期保留 `load_marketing_skill`，但原生 Skill 的正常路径只依赖 Run Snapshot 注入的目录、Tool Contracts 和 Root Policy；`pi-runtime` POC 仍明确标记为兼容测试入口。
- QA 文档记录设计覆盖矩阵、测试命令、未执行真实外部动作和关键安全断言；changelog 记录每阶段提交/验证/遗留事实。

- [x] **Step 1: Write the failing test**：更新技能文本断言，确保不再要求固定工具顺序/Evidence Bridge/旧内部工具作为生产必经路径，且 compatibility tool 仍存在于迁移期定义。
- [x] **Step 2: Run test to verify it fails**：`cd pi-runtime && npm test -- --run tests/skills.test.ts tests/internal-tools.test.ts`；旧文案断言按预期失败；内部工具套件因当前 worktree 缺少 `typebox` 无法收集，未擅自安装依赖。
- [x] **Step 3: Write minimal implementation**：更新文案为模型自主决策、直接 MCP Result、generic report/workbook、数据不足披露和 clarification 语义；runbook/QA 明确 native loader 与数据库 Revision 单一事实源，不把 Skill 内容测试升级成全量回归门禁。
- [x] **Step 4: Run test to verify it passes**：`tests/skills.test.ts` 为 1 个文件、6 个测试通过；`git diff --check`、secret/DSN/Bearer 扫描和旧桥接/固定规格静态扫描通过。
- [x] **Step 5: Update changelog and commit**：已提交 `b3249e9 docs: document autonomous skill rollout and report contract`。

### Task 11: 设计覆盖自审、独立审查与最终候选验证

**Files:**
- Modify: `docs/superpowers/plans/2026-08-21-pi-autonomous-marketing-skills-implementation.md`
- Modify: `docs/qa/2026-08-21-pi-autonomous-marketing-skills.md`
- Modify: `docs/runbooks/pi-agent-gateway.md`
- Modify: `changelog/2026-08-21.md`

- [x] **Step 1: Spec coverage self-review**：已逐条对照设计 §2–§15 与 Task 1–10，确认 Revision/Activation/灰度/回滚、snapshot、native loader、管理 UI、analysis_report、workbook、标准兼容、澄清、direct MCP、unknown、技术上限和文档均有实现或明确验证边界；剩余占位仅为本 Task 未完成 checkbox。
- [x] **Step 2: Run affected verification**：遵循用户明确“不重复全量测试”，未重跑 backend/Gateway/Runtime/前端全量、E2E、迁移升级回滚或真实外部验证；已在最终执行分支完成受影响 Skill 定向测试、`git diff --check`、`git show --check` 和 secret/DSN/Bearer 扫描，结果记录在 QA 文档。
- [x] **Step 3: Independent review**：只读审查 `63d3cf7..b3249e9`；Critical 0，Important 2，Minor 1，重点架构边界未发现其他问题。
- [x] **Step 4: Fix only affected findings**：先补 RED 测试，再修复报告元数据展示、全局 scope 唯一性与租户 Diff；受影响后端 18 项、前端 10 项和后端 Ruff 通过，未重跑无关全量。
- [x] **Step 5: Create integration candidate**：已保持 `main` 引用不动，在独立 worktree 从执行 HEAD `b3ffe36` 创建 `codex/pi-autonomous-marketing-skills-integration`，以 `--no-ff` 合并提交 `158e503`；仅在候选上完成一次受影响验证，不 push、不部署、不移动 main 引用。
- [ ] **Step 6: Mark Goal complete**：本步骤暂不勾选。2026-08-21 用户重新授权后，发现历史 `required-artifact runtime gate` 仍需从新 Pi Runtime 路径移除；本计划继续执行后续修复、最终候选、合入 main、CI、预发布、真实 Web UAT 与生产灰度，不能以旧候选或离线通过提前结束 Goal。

### Task 12：移除新 Pi Runtime 的固定 required-artifact 完成门禁（2026-08-21 重新授权）

**目标：** 保留历史字段和旧 Snapshot 的读取兼容，但让新 Run 只使用通用正式完成不变量：正式分析
Run 必须有当前 Run/Session/tenant 归属下至少一个已发布、不可变、lineage 有效的顶层主报告；报告
类型由 Pi 在 RuntimeSnapshot allowlist 内自主选择标准 Artifact 或 `analysis_report_v1`。

- [x] **Step 1: CodeGraph 与运行路径盘点**：尝试从 CompletionValidator、terminal ACK、Recovery、
  force-complete 和离线 UAT 追踪消费路径；现有 CodeGraph 索引落后于 release worktree，未将新符号纳入
  索引，因此以当前源码的结构化 `rg` 复核实际路径，并在 QA 留存该限制。确认统一校验由
  `CompletionValidator` 进入 engine、terminal、recovery 和 force-complete。
- [x] **Step 2: 定向 RED 测试**：锁定标准 Artifact、`analysis_report_v1`、clarification 无报告、
  正式 Run 无主报告拒绝、跨 tenant/历史/未发布 Draft 拒绝、Recovery/terminal ACK/force-complete
  一致校验、workbook 与报告 Version 不一致拒绝和 legacy Snapshot 只读不回写。
- [x] **Step 3: 最小实现**：删除新 Snapshot 对固定 required contract 的消费；保留旧字段、DTO、数据库列
  和历史 Snapshot 的旧语义读取。新增 server-owned `completion_mode`（只区分正式分析与交互，不是
  Artifact 类型，且不从用户文本、模型输出或 Builder 推导）。新路径不新增固定 Artifact 推导器，
  不恢复 Evidence Bridge、`mcp_result_v1`、candidate/Corpus Gate。
- [x] **Step 4: 定向 GREEN**：受影响 Completion/terminal/Recovery/settle/runtime-config 测试
  `57 passed`；标准与通用报告共用主报告校验，clarification/interaction 例外不放宽正式分析安全边界。
- [x] **Step 5: 复验既有红灯与离线 UAT**：原先 12 项红灯只执行一次，结果 `11 passed, 1 failed`；
  仅隔离该失败项复跑一次后 `1 passed`。随后离线 UAT 全套只执行一次，`28 passed`。不重复完整 UAT。
- [ ] **Step 6: 独立审查与 `fix(runtime)` 提交**：待审查确认 Critical 0 / Important 0 后，创建独立
  `fix(runtime)` 提交，并继续 Task 13 的发布链路。

### Task 13：生产发布继续执行

- [ ] 最终发布树验证并重建包含 Task 12 修复的 integration candidate。
- [ ] 合入 `main` 并通过 CI；失败时只修复真实失败，不删除门禁或伪造结果。
- [ ] 预发布部署与一次真实 Web UAT；确认真实模型、DataTap、钱包、Reviewer、Version、lineage
  和同版 workbook 约束。
- [ ] 生产灰度 `5% → 25% → 100%`，完成生产验收、监控、文档封口和回滚演练记录，最终状态才可写为
  `PRODUCTION_RELEASE_COMPLETE`。

## Spec Coverage Self-Review Matrix

| 设计验收主题 | 覆盖任务 | 关键证据 |
|---|---:|---|
| Revision、Activation、全局/租户/灰度/回滚、幂等、审计 | 1–3 | 0045、repository/service/API 定向测试 |
| 新 Run immutable snapshot、热更新边界、resume/recovery | 3 | RuntimeConfigSnapshot、manifest digest、snapshot tests |
| 原生 Pi `noSkills: true` + `additionalSkillPaths`、路径/digest fail-closed | 4 | materializer/resource-loader/sdk tests |
| Root Policy/Tool Contracts 与迁移期兼容工具 | 3–4、10 | snapshot allowlist、native skill text tests |
| 管理端列表/编辑/校验/diff/发布/灰度/回滚/审计 | 2、8 | FastAPI + React 定向测试 |
| `analysis_report_v1` typed blocks、fulfillment、partial/unavailable、无 Top20 业务门禁 | 5、9 | Pydantic/publish/UI tests |
| 同一 Version 的 `workbook_v1`、确定性 Excel、布局缓存、安全分页 | 6、9 | exporter/cache/router tests |
| 澄清、明确请求、自由组合、direct MCP、错误语义、unknown 继续 | 7、10 | Pi UAT/Completion/Gateway/skill tests |
| 标准 Artifact/BI/Exporter 兼容与一次最终验证 | 5–7、11 | 受影响回归 + 最终唯一全量 |
| runbook、QA、changelog、独立审查和本地 integration candidate | 10–11 | 文档、审查记录、merge candidate |
