# 品牌之后营销能力功能实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 在获得新的实施授权后，使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐 Task 执行；本文件所有步骤使用 checkbox 追踪。本设计会话不得执行本计划。

**Goal:** 在不限制 Pi 业务自主性的前提下，完成品牌 Skill 默认固化、服务端 KOL 评分、自由组合 Report/Workbook、交互控制、Skill 管理与 Artifact 全生命周期。

**Architecture:** production Skill 只从数据库 Revision/Activation 解析，新环境由不可变 bootstrap bundle 初始化；标准 Artifact 与 `analysis_report_v1` 并存。DataTap Result 原样直通，同时以非语义 SHA-256 承诺绑定当前 Run settled 来源；KOL 官方评分与 fulfillment 只从绑定行经共享服务端投影生成。Artifact Version 是 BI 与 Excel 的共同只读来源，clarification/cancel/version-bound drilldown 由可信运行边界约束。

**Tech Stack:** Python 3.11/3.12、FastAPI、Pydantic v2、SQLAlchemy Async、Alembic/MySQL 8、React 19/TypeScript/Vite、Vitest、Playwright、openpyxl、Pi Gateway/TypeScript。

**Spec:** `docs/superpowers/specs/2026-08-22-post-brand-marketing-functional-design.md`

## Global Constraints

- 从实施时重新核验的最新 `origin/main` 创建独立 worktree；禁止 reset/rebase/amend/stash/force-push 和历史重写。
- 先补最小 RED，再写最小实现；每个 Task 只跑列出的受影响测试；Task 9 才执行一次综合离线验证。
- Pi/模型自主决定澄清、工具、顺序、数量、失败降级、Artifact 类型与 Workbook 布局。
- 不增加固定阶段、固定工具顺序/次数、关键词 Artifact 路由、GoalPolicy、required-artifact 类型门禁、模型可见 Evidence Bridge 或 `mcp_result_v1`。允许 adapter 对完整且未改写的标准 Tool Result 计算非语义哈希承诺，finalize 仍只传 metadata，绝不分类/包装/持久化业务 content。
- DataTap 标准 MCP Tool Result content 原样进入模型；`result_unknown` 不自动重放、不自动释放预留。
- 明确、正常执行的用户分析 Run 至少发布一个当前 Run 顶层主报告；合法例外只有 clarification、cancel、硬失败、utility、kol-detail 和显式 read-only drilldown。
- BI 与 Excel 只读同一不可变 Version；null 不变 0；数量不足用 fulfillment；Workbook 不静默截断。
- production Skill 事实源是数据库；package/bundle 仅 bootstrap；running/recovery/resume 不重新解析 Activation。
- 任何外部真实模型、DataTap、钱包、Web UAT、部署、push 或 main 集成都必须在 Task 10 之后取得单独授权。
- 全部 campaign 专项源码、Schema、Skill、BI/Excel、测试场景与 UAT 均排除；共享代码只守既有兼容。

---

## 0. 文件职责图

| 文件/目录 | 责任 |
|---|---|
| `backend/app/marketing_skills/promotion.py`（新） | 从已持久化 Run Snapshot + SkillRevision 生成无秘密、digest 绑定的固化输入 |
| `backend/app/marketing_capability_pack/packs/marketing-v2/bootstrap/post-brand-default-v1.json`（新） | 已验收 B Snapshot 的新环境默认 + 待 UAT candidates；版本化、只增不改 |
| `backend/app/marketing_skills/bootstrap.py`（新） | 校验/读取 bootstrap bundle；不参与已创建 Run 的执行 |
| `backend/migrations/versions/0050_post_brand_skill_defaults.py`（新） | additive 增加 Skill 输入合同版本并插入历史 rev3/candidate；不移动 Activation |
| `backend/app/marketing_skills/snapshot.py` | production 新 Run 必须从 DB 解析全部 Skill，并冻结 Revision ID/scope/输入合同版本 |
| `pi-gateway/src/mcp-accounting-extension.ts` | 标准 Tool Result 原样转交；仅计算 canonical hash/bytes 写既有 finalize metadata |
| `backend/app/agent_runtime/tools/source_binding.py`（新） | 当前 Run settled Tool Result 的 hash/pointer/approved mapping 校验与幂等 lineage 来源 |
| `backend/app/selection/projection.py`（新） | 标准 KOL 与通用报告共享的绑定事实归一、去重、评分和 fulfillment |
| `backend/app/agent_artifacts/model_inputs/kol_selection.py` | v1/v2 并存；v2 只接来源引用/叙事，不接候选数值或官方分数 |
| `backend/app/agent_artifacts/model_inputs/analysis_report.py` | 通用报告 KOL server projection 输入和最终 Block/fulfillment 组装 |
| `backend/app/agent_artifacts/payloads/kol_selection.py` | additive fulfillment 输出；历史 v3 兼容 |
| `backend/app/agent_artifacts/payloads/analysis_report.py` | 通用 Block/Workbook 强类型与保留评分列边界 |
| `backend/app/agent_artifacts/exporters/workbook.py` | 从同一 Version 做安全确定性 Workbook 投影 |
| `backend/app/agent_artifacts/exporters/errors.py`、`router.py` | 结构化 Workbook 技术上限/导出错误 |
| `backend/app/agent_runtime/tools/pi_internal_tools.py` | clarification 零副作用门、Skill/model-input 注入 |
| `backend/app/pi_gateway/completion.py` | main_report/read_only 完成要求和历史兼容 |
| `backend/app/agent_runtime/drilldown.py`（新） | 显式历史 Version 只读 Run，禁止 DataTap/Artifact 写入 |
| `backend/app/agent_runtime/tools/history.py` | drilldown 按冻结 Version/lineage scope 限制 read_artifact/read_tool_result |
| `backend/app/marketing_skills/router.py`、`schemas.py`、`service.py` | Skill digest、audit timeline、scope 过滤 API |
| `backend/app/pi_gateway/internal_tools.py` | Pi 专属 Artifact SSE；不得下沉到 ToolRegistry |
| `src/components/admin/SkillAdmin.tsx` | Active/Previous digest、审计时间线、global/tenant 管理 |
| `src/components/artifacts/*` | KOL fulfillment、通用报告、Version 选择、未读、同版下载与钻取 |
| `src/components/agent/AgentRunCard.tsx`、`ChatArea.tsx` | clarification/cancel/pause/resume 按钮与错误反馈 |

## 依赖顺序

`Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6 → Task 7 → Task 8 → Task 9 → Task 10`

Task 3 的共享评分投影是 Task 4/5 的唯一官方 KOL 分数来源；Task 6 的 completion/cancel 语义是 Task 8 read-only drilldown 的前置；Task 9 只在 1–8 全部评审通过后执行。

---

### Task 1：当前实现与成功 Skill Snapshot 差距冻结

**Files:**

- Create: `backend/app/marketing_skills/promotion.py`
- Create: `backend/scripts/export_post_brand_skill_snapshot.py`
- Create: `backend/tests/marketing_skills/test_promotion.py`
- Create: `backend/tests/marketing_skills/post_brand_skill_source_map.json`
- Create: `backend/tests/marketing_skills/post_brand_success_skill_manifest.json`
- Create: `docs/qa/2026-08-22-post-brand-functional-gap-baseline.md`

**Inputs:**

- 唯一成功业务 Run 前缀 `a04213cf`；查询必须恰好命中一个 Run。
- manifest 全 entry 的显式 `revision_id + scope_key` source map；其中 `social-marketing-analyst` 必须映射 Revision ID `4eb2581a-6411-41ca-8bdb-7fb6487d21d0`。
- `AgentRun.runtime_config_snapshot_json.skill_manifest` 与数据库 `SkillRevision`。

**Outputs:**

- 一个无模型输出、无 MCP result、无 token/DSN/secret 的固化 fixture，包含完整规范化 Skill 正文、每个 entry 的 Revision ID/scope/number/digest、manifest digest 和 source Run ID。
- 一份事实表，列出已有能力与十个实施缺口：production package fallback、rev3 数字化调用上限、Pi Result 无 adapter 自生成 canonical commitment/服务端无法绑定模型候选事实、Skill/Builder 输入合同未按 Run 版本化、direct KOL score 可写、通用 fulfillment 去重/必需列可由模型指定、Skill audit UI 缺失、clarification 可晚于副作用、read-only drilldown 缺失且历史工具默认 Session 级、Workbook limit error 信息不足。

**Interfaces:**

- Produces: `async def list_post_brand_skill_source_candidates(db: AsyncSession, *, run_prefix: str) -> PostBrandSkillSourceCandidates`
- Produces: `async def load_post_brand_skill_snapshot(db: AsyncSession, *, run_prefix: str, source_map: Mapping[str, SkillRevisionSource]) -> PostBrandSkillSnapshotExport`
- Produces: `PostBrandSkillSnapshotExport.model_dump(mode="json")`，Task 2 的 bundle 生成器直接消费。
- Error codes: `skill_seed_source_missing`、`skill_seed_source_ambiguous`、`skill_seed_revision_scope_ambiguous`、`skill_seed_revision_mismatch`、`skill_seed_digest_mismatch`。

- [ ] **Step 1: 写最小 RED**

在 `test_promotion.py` 构造两个 Revision 与一个含 manifest 的 Run，先写以下断言：

```python
snapshot = await load_post_brand_skill_snapshot(
    db_session,
    run_prefix="a04213cf",
    source_map={
        "social-marketing-analyst": SkillRevisionSource(
            revision_id="4eb2581a-6411-41ca-8bdb-7fb6487d21d0",
            scope_key="__global__",
        ),
        "brand-research-report": SkillRevisionSource(
            revision_id=brand_revision_id,
            scope_key=brand_scope_key,
        ),
    },
)
assert snapshot.run_id == full_run_id
assert snapshot.entries["social-marketing-analyst"].revision == 3
assert snapshot.entries["social-marketing-analyst"].content_digest == canonical_skill_digest(content)
assert "runtime_config_snapshot_json" not in snapshot.model_dump(mode="json")
```

测试 fixture 的 source map 必须显式覆盖该 manifest 的全部 entry。再覆盖：prefix 多行、map 少/多 key、ID 不属于给定 scope、global/tenant 存在相同 name/revision/digest/content 但 map 未明确、Revision 缺失、Snapshot digest 与 DB content 不同、输出中出现 secret pattern 时 fail-closed。不得用当前 Activation 或 `created_at` 猜历史 Run 当时的 scope。

- [ ] **Step 2: 运行 RED**

Run:

```bash
(cd backend && .venv/bin/pytest -q tests/marketing_skills/test_promotion.py)
```

Expected: collection 失败或 import error，指出 `app.marketing_skills.promotion` 尚不存在。

- [ ] **Step 3: 写最小 GREEN**

在 `promotion.py` 定义 frozen Pydantic DTO：

```python
class PromotedSkillEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    revision_id: str
    scope_key: str
    skill_name: str
    revision: int
    content: str
    content_digest: str
    required_tools: tuple[str, ...]
    artifact_contract: str | None

class PostBrandSkillSnapshotExport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["post_brand_skill_snapshot_v1"]
    run_id: str
    manifest_digest: str
    entries: dict[str, PromotedSkillEntry]
```

查询只读，先由 CLI 计算绑定参数 `run_prefix_pattern = f"{run_prefix}%"`，再按 `run.id LIKE :run_prefix_pattern` 查询并要求唯一。候选扫描对每个 manifest entry 只列出精确匹配 `name + revision + digest + canonical content` 且 scope 为 global/Run tenant 的 Revision ID/scope，不输出正文。导出命令要求 source map key 与 manifest entry key 集合完全相等，逐 entry **只按 map 的 Revision ID**读取 DB，并同时核对 map scope、Run tenant 可见性、正文和 digest；输出前递归扫描 secret/DSN/Bearer 模式。CLI 只把 DTO JSON 写到显式 `--output`，不打印正文或环境变量。

- [ ] **Step 4: 生成并核对真实 fixture**

Run:

```bash
(cd backend && .venv/bin/python scripts/export_post_brand_skill_snapshot.py candidates --run-prefix a04213cf --output /private/tmp/post_brand_skill_source_candidates.json)
(cd backend && .venv/bin/python scripts/export_post_brand_skill_snapshot.py export --run-prefix a04213cf --source-map tests/marketing_skills/post_brand_skill_source_map.json --output tests/marketing_skills/post_brand_success_skill_manifest.json)
```

Expected: 第一条命令输出每个 entry 的无正文候选；实施者把**每个** entry 的准确 `revision_id/scope_key` 写入 source map，且 root 使用已知 ID，再由第二条命令验证并导出。任何 entry 有多个候选而未显式选择时以 `skill_seed_revision_scope_ambiguous` 停止；不得取当前 Activation 代替历史映射。fixture 中已知 Revision 3 的 name 是 `social-marketing-analyst`，不能写成 `brand-research-report@3`。若本地没有该持久化 Run，脚本以 `skill_seed_source_missing` 退出，Task 停止并请求用户提供经同一脚本生成的 candidates/source-map/fixture；禁止从 changelog 摘要拼正文。候选临时文件核对后不提交，source map 与最终 fixture 提交。

- [ ] **Step 5: GREEN 与受影响验证**

Run:

```bash
(cd backend && .venv/bin/pytest -q tests/marketing_skills/test_promotion.py && .venv/bin/ruff check app/marketing_skills/promotion.py scripts/export_post_brand_skill_snapshot.py tests/marketing_skills/test_promotion.py)
```

Expected: promotion tests 全绿；Ruff 0 error。静态扫描 fixture 不含 `Bearer `、DSN、`sk-` 或私钥头。

- [ ] **Step 6: 写 gap baseline 并提交**

baseline 必须记录 source Run/manifest digest、准确 Skill 身份、当前文件/符号、十个缺口和“不执行外部 UAT”。

```bash
git add backend/app/marketing_skills/promotion.py backend/scripts/export_post_brand_skill_snapshot.py backend/tests/marketing_skills/test_promotion.py backend/tests/marketing_skills/post_brand_skill_source_map.json backend/tests/marketing_skills/post_brand_success_skill_manifest.json docs/qa/2026-08-22-post-brand-functional-gap-baseline.md
git commit -m "test(marketing): freeze post-brand skill baseline"
```

---

### Task 2：品牌 Skill Revision 正式固化、默认 seed 与回滚

**Files:**

- Create: `backend/app/marketing_capability_pack/packs/marketing-v2/bootstrap/post-brand-default-v1.json`
- Create: `backend/app/marketing_skills/bootstrap.py`
- Create: `backend/scripts/register_post_brand_bootstrap.py`
- Create: `backend/scripts/initialize_marketing_skill_defaults.py`
- Create: `backend/migrations/versions/0050_post_brand_skill_defaults.py`
- Create: `backend/tests/marketing_skills/test_post_brand_bootstrap.py`
- Create: `backend/tests/marketing_skills/test_migration_0050.py`
- Modify: `backend/app/marketing_capability_pack/loader.py`
- Modify: `backend/app/marketing_capability_pack/models.py`
- Modify: `backend/app/marketing_skills/models.py`
- Modify: `backend/app/marketing_skills/repository.py`
- Modify: `backend/app/marketing_skills/schemas.py`
- Modify: `backend/app/marketing_skills/service.py`
- Modify: `backend/app/marketing_skills/validation.py`
- Modify: `backend/app/marketing_skills/snapshot.py`
- Modify: `backend/app/marketing_capability_pack/runtime.py`
- Modify: `backend/app/runtime_config/schemas.py`
- Modify: `backend/app/runtime_config/service.py`
- Modify: `backend/tests/marketing_capability_pack/test_loader.py`
- Modify: `backend/tests/marketing_skills/test_snapshot.py`
- Modify: `backend/tests/runtime_config/test_snapshots.py`
- Create: `backend/tests/marketing_skills/skill_manifest_digest_vectors.json`
- Modify: `pi-gateway/src/protocol.ts`
- Modify: `pi-gateway/src/main.ts`
- Modify: `pi-gateway/src/skill-snapshot.ts`
- Modify: `pi-gateway/tests/main.test.ts`
- Modify: `pi-gateway/tests/skill-snapshot.test.ts`
- Modify: `backend/app/marketing_capability_pack/packs/marketing-v2/manifest.json`（只登记 bootstrap bundle path/digest；不改 campaign entry）

**Inputs:** Task 1 的 `PostBrandSkillSnapshotExport` fixture。

**Outputs:**

- 精确成功 B Snapshot 默认/回滚基线 + 无固定工具次数的 `social-marketing-analyst` successor Revision 4 candidate。
- migration/startup 只增加向后兼容合同列并插入缺失 Revision，绝不移动任何现有 Activation。
- 新环境在 migrations 后显式运行一次 initializer，只激活成功 B Snapshot 的已验收 Revision（`social-marketing-analyst` active=Revision 3）；Revision 4、KOL/analysis successor 都保持 candidate。已有环境后续激活必须走管理 API。
- production 新 Run 缺任一 DB Skill Activation 时 fail-closed，不回退 package Skill。
- 旧/新模型输入合同按 Run Snapshot 并存，部署不能原地改变 running/resume Run 的可执行 schema。

**Interfaces:**

- Produces: `def load_post_brand_bootstrap() -> PostBrandBootstrapBundle`
- Produces: `def validate_bootstrap_digest(bundle: PostBrandBootstrapBundle) -> None`
- Produces: `async def initialize_marketing_skill_defaults(db, *, environment: str, bundle_names: tuple[str, ...], new_environment: Literal[True]) -> SkillBootstrapApplicationRead`
- Adds: `CapabilityPackSnapshot.bootstrap_bundles: tuple[BootstrapBundleSpec, ...]`
- Adds: `SkillRevision.model_input_contract_version: str`，数据库旧行/server default 为 `direct_model_input_v1`。
- Adds: `SkillManifest.schema_version="skill_manifest_v2"` entry 的 `revision_id/scope_key/model_input_contract_version`；历史缺 discriminator 严格按 v1 digest 算法。
- Adds: `RuntimeConfigSnapshot.artifact_input_contract_versions: dict[str, Literal["direct_model_input_v1", "source_bound_input_v2"]]`，新 Run 服务端生成，历史缺失解释为 v1。
- Changes Pi protocol: `SkillManifestSnapshot.schemaVersion: "skill_manifest_v1" | "skill_manifest_v2"`；历史 claim 无 `schema_version` 映射 v1，v2 entry 增加 `revisionId/scopeKey/modelInputContractVersion`；`RuntimeSnapshot.artifactInputContractVersions` 必须保留服务端映射。
- Changes digest: `skillManifestDigest(entries, sourceScope, schemaVersion)`；v1 精确重现现有七字段 bytes，v2 把 discriminator 与三个新字段纳入；Python/TypeScript 共同读取 `skill_manifest_digest_vectors.json`。
- Changes: `SkillSnapshotService.resolve_for_new_run(db, *, tenant_id, base_capability, environment="production", require_database_entries=True) -> MarketingRunCapability`
- Errors: `skill_activation_incomplete`、`skill_seed_digest_conflict`。

`BootstrapBundleSpec` 是 frozen dataclass，字段固定为 `name: str`、`path: str`、`digest: str`；manifest 每项只允许这三个 key。

- [ ] **Step 1: 写 bootstrap/production fail-closed RED**

在 `test_post_brand_bootstrap.py` 与 `test_snapshot.py` 先断言：

```python
bundle = load_post_brand_bootstrap()
assert bundle.source_manifest_digest == fixture["manifest_digest"]
assert bundle.revisions["social-marketing-analyst"][0].revision == 3
assert bundle.revisions["social-marketing-analyst"][1].revision == 4
assert "单轮调用" not in bundle.revisions["social-marketing-analyst"][1].content
assert bundle.revisions["social-marketing-analyst"][0].default_activation is True
assert bundle.revisions["social-marketing-analyst"][1].candidate_activation is True

with pytest.raises(SkillSnapshotError, match="skill_activation_incomplete"):
    await service.resolve_for_new_run(
        db,
        tenant_id="tenant-without-activation",
        base_capability=base_capability,
        environment="production",
        require_database_entries=True,
    )
```

migration RED 覆盖：旧 Revision 回填 `direct_model_input_v1`；Revision 缺失时插入；已有相同 digest/contract version 幂等；相同身份不同 digest 或合同版本失败；所有 Activation 指针均不移动；campaign rows/pointers 完全不变。initializer RED 覆盖：仅接受显式 `new_environment=True`；校验目标指针仍为 `0048` 审计基线且没有管理员 Activation 审计；一次事务只设置 `default_activation=true` 的成功 Snapshot Revision 并写 bootstrap audit；Revision 4/candidates 指针不动；重放幂等；任一目标已被人工变更即 `skill_bootstrap_environment_not_fresh`。Backend Snapshot RED 覆盖 manifest v1 原 digest 仍能校验、v2 冻结 ID/scope/contract、旧 Run v1 与新 Run v2 Builder dispatch 不串版。Pi RED 覆盖：历史无 discriminator 的 v1 claim 可映射且 digest 不变；v2 claim 精确保留三新字段与 `artifact_input_contract_versions`；entry 字段、manifest digest、contract map 任一缺失/未知/篡改/与 artifact contract 不一致均在 spawn 前拒绝；old Run resume v1、新 Run v2；Python/TypeScript 对 v1/v2 golden vectors digest 完全相等。loader RED 覆盖 `bootstrap_bundles` 的严格字段、safe path、内容 digest 与 secret/path 拒绝，确保新 manifest 字段不是绕过当前 `_MANIFEST_KEYS` 校验的任意扩展。

- [ ] **Step 2: 运行 RED**

```bash
(cd backend && .venv/bin/pytest -q tests/marketing_capability_pack/test_loader.py tests/marketing_skills/test_post_brand_bootstrap.py tests/marketing_skills/test_migration_0050.py tests/marketing_skills/test_snapshot.py tests/runtime_config/test_snapshots.py)
(cd pi-gateway && npm test -- tests/main.test.ts tests/skill-snapshot.test.ts -t "post-brand manifest contract")
```

Expected: 新 bundle/loader/migration/initializer 缺失，且 production Snapshot 仍会使用 package fallback。

- [ ] **Step 3: 写 immutable bundle 与 successor 正文**

先创建当前基线尚不存在的 `backend/app/marketing_capability_pack/packs/marketing-v2/bootstrap/` 子目录；除本计划列出的三个不可变 bundle 外不放运行时可编辑文件。

Revision 3 必须逐字取 Task 1 fixture。Revision 4 以 Revision 3 为父内容，只把数字化止损段替换为下列确切语义：

```markdown
## 外部数据获取止损纪律

- 当同一业务目标的重复服务端失败已经表明继续微调参数不会增加可靠信息时，停止无效探测；保留已 settled 的结果，把受影响章节标记为 restricted 并写明 limitation。
- 优先保证用户要求范围的真实覆盖，不为追求穷尽而反复调用同族能力。
- 只有当已返回数据确实需要新的聚合口径时，才由模型自主决定是否另行重聚合；不得把重聚合作为固定阶段。
```

Revision 3 的输入合同标记为 `direct_model_input_v1`。Revision 4 的 frontmatter 沿用成功 Snapshot 的 name/description/required_tools/artifact_contract，增加受允许的 `model_input_contract_version: direct_model_input_v1`；规范化后计算新 digest。bundle 顶层含 schema version、source IDs/scopes/digests、每条 Revision 的合同版本、`default_activation/candidate_activation` 和 bundle digest；Revision 3 属默认，Revision 4 仅 candidate。文件发布后只增不改。

- [ ] **Step 4: 写 migration/loader 与 production DB-only 解析**

`0050` 先 additive 增加非空合同版本列并把旧行固定为 v1，再通过 bundle 常量插入 Revision；冲突不 UPDATE content，也不 UPDATE/INSERT Activation。`validation/service/repository` 把 frontmatter 合同版本持久化为 Revision 不可变元数据。`loader.py` 把 `bootstrap_bundles` 加入严格 manifest schema，使用既有 `_safe_path/_read_checked` 校验 bundle 文件；`CapabilityPackSnapshot` 只保存 bundle 的 name/path/digest 元数据，不把 seed 正文带进 Run capability。`snapshot.py` 在 production 要求每个 capability Skill 都有数据库 Revision，并为新 Run 生成 `skill_manifest_v2 + artifact_input_contract_versions`；同一 artifact contract 有不同输入版本时 `skill_input_contract_conflict`。DB extra Skill 仍可按 Activation 加入。

Pi `protocol.ts/main.ts/skill-snapshot.ts` 同步实现 discriminated v1/v2 strict union：v1 无 discriminator 时只允许现有七字段并用原 digest payload；v2 必须有 `schema_version=skill_manifest_v2`，entry exact keys 增加 `revision_id/scope_key/model_input_contract_version`，digest payload 同时含 schema version。`mapClaimRuntimeSnapshot` 把 snake_case contract map 映射为 `artifactInputContractVersions`，要求 map key 与 allowed artifact contracts 对齐，且每个 v2 entry 的非空 artifact contract 值与 map 相等；不能删除未知字段后继续。Skill 文件 materialization 仍只写冻结 content，但携带的 entry/claim 审计元数据保持 v1/v2 不丢失。

`initialize_marketing_skill_defaults.py --new-environment --environment production --bundle post-brand-default-v1` 是环境创建清单中的显式一步：先验证全部目标仍为 0048 基线、无管理员变更，再单事务把指针设为 bundle 的**已验收 default entries**并写审计；所有 candidate 均跳过并记录。它不由 Alembic、startup 或部署脚本自动调用。已有环境即使只缺默认，也走 Task 7 管理 API，不运行 initializer。

package manifest 只增加 `bootstrap_bundles` 登记。实现脚本从 bundle 正文规范化计算 digest、写入 bundle 自身校验字段并同步 manifest；不能靠人手转抄：

```bash
(cd backend && .venv/bin/python scripts/register_post_brand_bootstrap.py \
  --bundle app/marketing_capability_pack/packs/marketing-v2/bootstrap/post-brand-default-v1.json \
  --manifest app/marketing_capability_pack/packs/marketing-v2/manifest.json)
```

脚本必须幂等，输出只包含登记名和 digest，不打印 Skill 正文；测试从实际文件重算并断言 manifest 中是相同的 64 位小写十六进制。Task 9 离线门通过只表示 candidate 可申请真实 UAT，仍不得把 Revision 4 设为新环境默认或既有 production active；切换必须使用 Task 10 后的新授权。

- [ ] **Step 5: GREEN 与受影响验证**

```bash
(cd backend && .venv/bin/pytest -q tests/marketing_capability_pack/test_loader.py tests/marketing_skills/test_post_brand_bootstrap.py tests/marketing_skills/test_migration_0050.py tests/marketing_skills/test_snapshot.py tests/runtime_config/test_snapshots.py && .venv/bin/ruff check app/marketing_skills app/marketing_capability_pack scripts/register_post_brand_bootstrap.py scripts/initialize_marketing_skill_defaults.py tests/marketing_capability_pack tests/marketing_skills tests/runtime_config)
(cd pi-gateway && npm test -- tests/main.test.ts tests/skill-snapshot.test.ts -t "post-brand manifest contract" && npm run typecheck)
```

Expected: bundle/fixture/revision digest 全相等；production 无 DB entry fail-closed；Backend 与 Pi 对 v1/v2 golden vector 完全一致；v2 claim 保留合同 map，篡改 fail-closed；旧 Snapshot/old resume 仍走 v1；campaign fixture 无 diff。

- [ ] **Step 6: 提交**

```bash
git add backend/app/marketing_capability_pack backend/app/marketing_skills backend/app/runtime_config/schemas.py backend/app/runtime_config/service.py backend/scripts/register_post_brand_bootstrap.py backend/scripts/initialize_marketing_skill_defaults.py backend/migrations/versions/0050_post_brand_skill_defaults.py backend/tests/marketing_capability_pack backend/tests/marketing_skills/skill_manifest_digest_vectors.json backend/tests/marketing_skills/test_post_brand_bootstrap.py backend/tests/marketing_skills/test_migration_0050.py backend/tests/marketing_skills/test_snapshot.py backend/tests/runtime_config/test_snapshots.py pi-gateway/src/protocol.ts pi-gateway/src/main.ts pi-gateway/src/skill-snapshot.ts pi-gateway/tests/main.test.ts pi-gateway/tests/skill-snapshot.test.ts
git commit -m "feat(skills): solidify post-brand defaults"
```

---

### Task 3：达人圈选模型输入与确定性评分边界

**Files:**

- Create: `backend/app/marketing_capability_pack/packs/marketing-v2/bootstrap/kol-selection-server-score-v1.json`
- Create: `backend/migrations/versions/0051_kol_selection_skill_contract.py`
- Create: `backend/tests/marketing_skills/test_kol_selection_skill_bundle.py`
- Create: `backend/tests/marketing_skills/test_migration_0051.py`
- Modify: `backend/app/marketing_capability_pack/packs/marketing-v2/manifest.json`
- Create: `backend/app/agent_runtime/tools/source_binding.py`
- Create: `backend/tests/agent_runtime/tools/test_source_binding.py`
- Create: `backend/app/selection/projection.py`
- Create: `backend/tests/selection/test_projection.py`
- Modify: `backend/app/agent_artifacts/model_inputs/kol_selection.py`
- Modify: `backend/app/agent_artifacts/model_inputs/__init__.py`
- Modify: `backend/app/agent_runtime/tools/builders.py`
- Modify: `backend/app/agent_runtime/tools/calculation.py`
- Modify: `backend/app/agent_artifacts/builders/kol_selection.py`
- Modify: `backend/app/agent_artifacts/payloads/kol_selection.py`
- Modify: `backend/tests/agent_runtime/tools/test_direct_artifact_builder.py`
- Modify: `backend/tests/agent_runtime/tools/test_calculation.py`
- Modify: `backend/tests/agent_artifacts/test_kol_selection_builder.py`
- Modify: `backend/app/agent_runtime/evidence.py`
- Modify: `backend/app/pi_gateway/accounting.py`
- Modify: `backend/tests/pi_gateway/test_mcp_result_envelope.py`
- Modify: `backend/tests/pi_gateway/test_direct_mcp_architecture.py`
- Modify: `pi-gateway/src/mcp-accounting-extension.ts`
- Modify: `pi-gateway/tests/mcp-accounting-extension.test.ts`
- Modify: `pi-gateway/tests/direct-mcp-result.test.ts`

**Inputs:** `selection.scoring_v3.score_and_rank_candidates_v3`、用户 scope、模型选择的当前 Run settled Tool Result 行引用。

**Outputs:** `kol-selection-report@3` 不可变 candidate + seed bundle；标准 Evidence builder、Pi direct KOL builder 与 v2 `rank_kols` 共用同一绑定来源/规范化/去重/评分投影；模型不能提交候选数值、字段映射、去重键或任何官方分数。

**Interfaces:**

```python
class McpResultSourceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tool_call_id: str
    tool_result: dict[str, JsonValue]  # 完整、未改写的标准 MCP Tool Result
    row_pointers: tuple[str, ...]      # 模型只选择行，不提供字段映射

class KolProjectionScopeV1(BaseModel):
    brand: str | None
    category: str | None
    platforms: tuple[str, ...]
    audience: AudienceFilter
    filters: SelectionFilters
    content_directions: tuple[str, ...]
    content_formats: tuple[str, ...]
    period: Period | None

class KolProjectionRequest(BaseModel):
    scope: KolProjectionScopeV1
    sources: tuple[McpResultSourceV1, ...]
    requested_min: int
    limit: int | None
    preference: Literal["effect", "balanced", "price"]
```

可信边界签名固定为：

```python
async def resolve_bound_mcp_sources(
    db: AsyncSession,
    *,
    context: ToolContext,
    sources: tuple[McpResultSourceV1, ...],
    projection_kind: Literal["kol_candidates_v1", "viral_posts_v1"],
) -> BoundSourceRows

def project_kol_candidates(
    *, scope: KolProjectionScopeV1, rows: BoundSourceRows,
    requested_min: int, limit: int | None, preference: Preference,
) -> KolScoringProjection
```

`BoundSourceRows` 是 internal-only frozen DTO，包含服务端抽取事实、Evidence ID/path/hash 与 limitations；不能由 Build tool 的模型 input 构造。`KolScoringProjection` 返回去重 candidates、ranked items、scoring config、summary、fulfillment、limitations、availability。

`canonical_tool_result_v1` 固定采用 RFC 8785 JSON Canonicalization Scheme：完整 JSON Tool Result、数组顺序不变、JSON string/null/boolean/number 不作业务归一、UTF-8，hash 为 `sha256:<64 lowercase hex>`。Python/TypeScript 共用同一 golden vectors，拒绝 NaN/Infinity/非 JSON 值。Pi adapter 在 Result 交给模型前对同一个内存对象计算 hash/bytes；转交值做 deep-equal 断言。finalize 请求仍只含 bounded metadata，不含 content/structuredContent/payload。Backend 从 settle ledger metadata 读取 hash，不信任模型提交的 hash。

`kol-selection-report@3` 正文主体固定为：

```markdown
# 达人圈选与分析

先根据用户目标判断平台、品牌或品类、受众、预算、数量、粉丝范围、地域和内容方向是否足以形成候选范围；不足时可请求一次合并澄清，明确时直接分析。你自主选择已审核能力、顺序、数量和失败降级，不把内部工具名展示给用户。

构建正式结果时只提交 `KolSelectionV3InputV2` 允许的 scope、当前 Run settled Tool Result 完整副本与行指针、requested_min、preference、叙事、limitations 和 methodology_input。不得重写候选事实，不得提交字段映射、去重键、rank、rating、任何 score、score_snapshot、data.scoring、actual_count 或 fulfillment status；这些字段由服务器确定性生成。来源/hash/字段级校验失败时按返回 path 修正引用，不得猜分。

同平台候选按稳定 kol_uid 去重；跨平台身份分别保留。数量不足时输出全部真实唯一候选并披露 requested/actual，不伪造补齐。标准 Top20 可使用 kol_selection_v3；超过 20 或需要跨域自定义表时可自主选择 analysis_report_v1。评分合同不可用时不得生成官方分数，可发布只含真实事实和限制的 restricted 通用报告。

所有主页和内容链接必须来自真实返回且为 http/https。发布后 BI 与 Excel 读取同一不可变 Version；null 不变 0。
```

frontmatter 的 name 固定 `kol-selection-report`，required_tools 保持 `build_artifact_draft/publish_artifacts`，artifact_contract 保持 `kol_selection_v3`，`model_input_contract_version` 固定 `source_bound_input_v2`。`0051` 只插入 global Revision 3 candidate；不移动 Activation、不触碰 campaign。initializer 只登记/校验 candidate，不为新环境激活；既有/新环境都要等 Task 10 后单独真实 UAT 通过，再由未来默认 bundle或管理 API 推广。

- [ ] **Step 1: 写“模型伪造分数”RED**

```python
polluted = kol_model_input()
polluted["data"] = {"items": [{"score_snapshot": {"value_score": 99}}]}
result = await tool.execute(ctx, BuildArtifactDraftArgs(
    artifact_type="kol_selection_v3", payload=polluted
))
assert result.error_type == "kol_score_server_owned_field_rejected"
assert "/data/items/0/score_snapshot" in result.safe_summary
```

先写来源绑定 RED：跨 Run/非 settled/unknown/failed ToolCall、缺失或伪造 hash、截短/改字段后的 Tool Result、非法/越界 pointer、未审核工具、模型自带字段映射全部拒绝；原样结果 hash 匹配后才返回行；同一 ToolCall 重复 build 在 ToolCall 行锁下复用同一 `(tool_call_id,payload_hash)` Evidence。断言 `PiGatewayService.finalize_mcp` 仍没有 `EvidenceWriter`/业务 payload，adapter 转交给模型的 Result deep-equal，finalize body 不出现 content/structuredContent。

再写投影 RED：同平台重复 ID 去重；跨平台同昵称保留；缺 ID 排除；绑定的 audience/content facts 确定性派生；模型不能通过提交另一个 `unique_by` 改变计数；40 请求/27 唯一候选返回 partial；scorer unavailable 不产 score/rating。合同共存 RED：旧 Snapshot/v1 Skill 仍走 `KolSelectionV3InputV1`，新 Snapshot/v2 Skill 只接受 source refs，running/resume 不因 Activation 改变。bundle/migration RED 断言 Revision 3 正文/digest/contract metadata 精确一致、migration 不移动 Activation、initializer 不激活 candidate、campaign 行和指针完全不变。

- [ ] **Step 2: 运行 RED**

```bash
(cd backend && .venv/bin/pytest -q tests/marketing_skills/test_kol_selection_skill_bundle.py tests/marketing_skills/test_migration_0051.py tests/selection/test_projection.py tests/agent_runtime/tools/test_source_binding.py tests/agent_runtime/tools/test_direct_artifact_builder.py tests/pi_gateway/test_mcp_result_envelope.py tests/pi_gateway/test_direct_mcp_architecture.py -k "kol or projection or source_bound or server_owned or bundle")
(cd pi-gateway && npm test -- tests/mcp-accounting-extension.test.ts tests/direct-mcp-result.test.ts -t "post-brand source binding")
```

Expected: bundle/migration/projection module 缺失；现有 direct input 接受模型 `data.scoring/score_snapshot`。

- [ ] **Step 3: 实现共享投影**

`source_binding.py` 先锁定 ToolCall，校验当前 context 归属、settled、approved catalog 和 ledger `response_hash`，重算完整结果 hash，再按 reviewed `(internal_tool_name, projection_kind)` mapping 解析 row pointer；映射不接受模型参数。校验通过后以 ToolCall 行锁保证 Evidence 幂等写入，返回 field→Evidence source paths。hash 缺失/不匹配、payload 超技术上限或 pointer 不可映射抛 `McpResultSourceUnbound`；不能降级为模型数值。

`projection.py` 只接受 `BoundSourceRows`，完成：平台规范化；`(platform, kol_uid)` 去重；冲突字段 limitation；从受众分布/内容标签/互动/粉丝确定性派生 `CandidateInputV3`；调用 `score_and_rank_candidates_v3`；生成 fulfillment；把 `KolProjectionScopeV1` 确定性映射到最终 `KolSelectionScopeV3`，历史兼容字段 `campaign` 固定为 `None`。缺评分器版本/类型不符抛 `KolScoreContractUnavailable`，不返回默认分。

`limit=None` 表示返回全部唯一候选，仍受技术 payload 上限；标准 KOL 调用传 `limit=20`，通用报告传 `None`。

同时用 Task 2 的 register 脚本登记并锁定 `kol-selection-server-score-v1` bundle digest；`0051` 只插入 candidate Revision，initializer 不设置其指针。

- [ ] **Step 4: 收紧 direct KOL input**

保留 `KolSelectionV3InputV1`，新增：

```python
class KolSelectionV3InputV2(BaseModel):
    scope: KolProjectionScopeV1
    sources: tuple[McpResultSourceV1, ...]
    requested_min: int = Field(ge=1)
    preference: Preference = "balanced"
    narrative: KolSelectionNarrative
    limitations: tuple[Limitation, ...] = ()
    methodology_input: KolSelectionMethodologyInput
```

`BuildArtifactDraftTool` 从 Run Snapshot 的 `artifact_input_contract_versions["kol_selection_v3"]` 选择 V1/V2 validator，模型参数不能指定版本。V2 resolver + assembler 调共享投影并填 server-owned `data/availability/data_status/fulfillment/lineage`。预扫描候选数值、字段映射、去重键与旧形状 server-owned paths，返回字段级 `kol_score_server_owned_field_rejected`；请求大于 20 时返回 `kol_selection_standard_limit_exceeded`，让 Pi 自主改用通用报告。V1 仅服务冻结旧 Run，不能被 V2 Skill 调用。

Evidence builder 把既有可信 Evidence 转为 internal `BoundSourceRows`；v2 `rank_kols` 接来源引用并调用同一 resolver/projection，v1 `rank_kols` 只为旧 Snapshot 保留且其输出不能进入 v2 Artifact。现有 `score_and_rank_candidates_v3` 仍是唯一公式真源。

- [ ] **Step 5: GREEN 与受影响验证**

```bash
(cd backend && .venv/bin/pytest -q tests/marketing_skills/test_kol_selection_skill_bundle.py tests/marketing_skills/test_migration_0051.py tests/selection/test_scoring_v3.py tests/selection/test_projection.py tests/agent_runtime/tools/test_source_binding.py tests/agent_runtime/tools/test_calculation.py tests/agent_runtime/tools/test_direct_artifact_builder.py tests/agent_artifacts/test_kol_selection_builder.py tests/pi_gateway/test_mcp_result_envelope.py tests/pi_gateway/test_direct_mcp_architecture.py && .venv/bin/ruff check app/selection app/agent_artifacts/model_inputs app/agent_runtime/tools app/agent_runtime/evidence.py app/pi_gateway/accounting.py tests/marketing_skills/test_kol_selection_skill_bundle.py tests/marketing_skills/test_migration_0051.py tests/selection tests/agent_runtime/tools/test_source_binding.py tests/agent_runtime/tools/test_direct_artifact_builder.py tests/agent_artifacts/test_kol_selection_builder.py)
(cd pi-gateway && npm test -- tests/mcp-accounting-extension.test.ts tests/direct-mcp-result.test.ts -t "post-brand source binding" && npm run typecheck)
```

Expected: 所有官方事实可追到 hash 匹配的 settled 调用，score/rank 与纯 scorer 精确一致；无模型污染/跨 Run/合同串版路径；缺绑定或合同输出 error/restricted 且无假分。正常 Tool Result 与交给 Pi 的对象 deep-equal，finalize 仍为 metadata-only。

- [ ] **Step 6: 提交**

```bash
git add backend/app/marketing_capability_pack/packs/marketing-v2/bootstrap/kol-selection-server-score-v1.json backend/app/marketing_capability_pack/packs/marketing-v2/manifest.json backend/migrations/versions/0051_kol_selection_skill_contract.py backend/app/selection/projection.py backend/app/agent_artifacts/model_inputs backend/app/agent_runtime/evidence.py backend/app/agent_runtime/tools/source_binding.py backend/app/agent_runtime/tools/builders.py backend/app/agent_runtime/tools/calculation.py backend/app/agent_artifacts/builders/kol_selection.py backend/app/agent_artifacts/payloads/kol_selection.py backend/app/pi_gateway/accounting.py backend/tests/marketing_skills/test_kol_selection_skill_bundle.py backend/tests/marketing_skills/test_migration_0051.py backend/tests/selection/test_projection.py backend/tests/agent_runtime/tools/test_source_binding.py backend/tests/agent_runtime/tools/test_direct_artifact_builder.py backend/tests/agent_runtime/tools/test_calculation.py backend/tests/agent_artifacts/test_kol_selection_builder.py backend/tests/pi_gateway/test_mcp_result_envelope.py backend/tests/pi_gateway/test_direct_mcp_architecture.py pi-gateway/src/mcp-accounting-extension.ts pi-gateway/tests/mcp-accounting-extension.test.ts pi-gateway/tests/direct-mcp-result.test.ts
git commit -m "feat(kol): enforce server-owned scoring"
```

---

### Task 4：达人报告、BI 与 Excel

**Files:**

- Modify: `backend/app/agent_artifacts/payloads/kol_selection.py`
- Modify: `backend/app/agent_artifacts/exporters/kol_selection.py`
- Modify: `backend/tests/agent_artifacts/test_kol_selection_builder.py`
- Modify: `backend/tests/agent_artifacts/test_kol_selection_export.py`
- Modify: `src/api/agentArtifacts.ts`
- Modify: `src/components/artifacts/KolSelectionArtifactView.tsx`
- Modify: `src/components/artifacts/KolSelectionArtifactView.test.tsx`
- Modify: `src/components/artifacts/ArtifactWorkspace.test.tsx`
- Modify: `e2e/artifact-workspace.spec.ts`

**Inputs:** Task 3 的 `KolScoringProjection` 与已发布 `kol_selection_v3` Version。

**Outputs:** 标准 Top20 BI/Excel 显示同一 score snapshot、主页/内容链接、缺失字段和 fulfillment；历史 Version 无新增字段仍兼容。

**Interfaces:**

- Add: `KolSelectionFulfillment(requested_min: int, actual_count: int, status, reason)`。
- Add optional: `KolSelectionSummary.fulfillment: KolSelectionFulfillment | None = None`。
- Add optional: `KolSelectionItem.content_urls: tuple[OptionalHttpUrl, ...] = ()`；新 Builder 从候选真实 URL 填充，历史 Version 缺字段仍可读。
- Frontend: `KolSelectionPayload.data.summary.fulfillment?: KolSelectionFulfillment`。

- [ ] **Step 1: 写 RED**

后端断言 20 请求/12 实际时 payload restricted、fulfillment=12/20；Excel 有“结果完整性”且数据行、URL、评分 Version 与 payload 一致。前端断言显示 `12/20`、数据受限、score version、missing reason，并在选 v1 时调用 `exportArtifact(id, 1)`。新增用例统一以 `post_brand`（pytest）或 `[post-brand]`（Vitest）命名，便于定向运行且不带入活动场景。

- [ ] **Step 2: 运行 RED**

```bash
(cd backend && .venv/bin/pytest -q tests/agent_artifacts/test_kol_selection_builder.py tests/agent_artifacts/test_kol_selection_export.py -k "kol and fulfillment")
npm run test -- src/components/artifacts/KolSelectionArtifactView.test.tsx -t "post-brand"
npm run test -- src/components/artifacts/ArtifactWorkspace.test.tsx -t "post-brand KOL version binding"
```

Expected: summary 无 fulfillment，BI/Excel 无完整性呈现。

- [ ] **Step 3: 实现 additive payload/export/UI**

Pydantic fulfillment 新字段默认 `None`、content URLs 默认空 tuple；新 Builder 必填 fulfillment 并保留通过校验的主页/内容 URL，历史读取可缺。Exporter 从 Version payload 写 score snapshot、主页/内容链接和 fulfillment，不重跑 scorer。URL 用 HTTP(S) hyperlink；null 用“未采集”；跨平台身份按两列 platform/kol_uid 展示。

前端 Version selector 的选中值必须同时传给 payload GET 和 export；切换 Version 时清空旧 payload loading state，避免 v1 UI/v2 下载竞态。

- [ ] **Step 4: GREEN 与受影响验证**

```bash
(cd backend && .venv/bin/pytest -q tests/agent_artifacts/test_kol_selection_builder.py tests/agent_artifacts/test_kol_selection_export.py -k "kol" && .venv/bin/ruff check app/agent_artifacts/payloads/kol_selection.py app/agent_artifacts/exporters/kol_selection.py tests/agent_artifacts/test_kol_selection_builder.py tests/agent_artifacts/test_kol_selection_export.py)
npm run test -- src/components/artifacts/KolSelectionArtifactView.test.tsx -t "post-brand"
npm run test -- src/components/artifacts/ArtifactWorkspace.test.tsx -t "post-brand KOL version binding" && npm run lint
```

Expected: 新/历史 payload 均可读；BI 与导出断言同一 Version/score snapshot；TypeScript 0 error。

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent_artifacts/payloads/kol_selection.py backend/app/agent_artifacts/exporters/kol_selection.py backend/tests/agent_artifacts/test_kol_selection_builder.py backend/tests/agent_artifacts/test_kol_selection_export.py src/api/agentArtifacts.ts src/components/artifacts/KolSelectionArtifactView.tsx src/components/artifacts/KolSelectionArtifactView.test.tsx src/components/artifacts/ArtifactWorkspace.test.tsx e2e/artifact-workspace.spec.ts
git commit -m "feat(kol): expose fulfillment in reports"
```

---

### Task 5：自由组合 `analysis_report_v1` / `workbook_v1`

**Files:**

- Create: `backend/app/marketing_capability_pack/packs/marketing-v2/bootstrap/analysis-report-server-fulfillment-v1.json`
- Create: `backend/migrations/versions/0052_analysis_report_skill_contract.py`
- Create: `backend/tests/marketing_skills/test_analysis_report_skill_bundle.py`
- Create: `backend/tests/marketing_skills/test_migration_0052.py`
- Modify: `backend/app/marketing_capability_pack/packs/marketing-v2/manifest.json`
- Modify: `backend/app/agent_artifacts/model_inputs/analysis_report.py`
- Modify: `backend/app/agent_artifacts/model_inputs/__init__.py`
- Modify: `backend/app/agent_artifacts/payloads/analysis_report.py`
- Modify: `backend/app/agent_artifacts/exporters/workbook.py`
- Modify: `backend/app/agent_artifacts/exporters/errors.py`
- Modify: `backend/app/agent_artifacts/router.py`
- Modify: `backend/tests/agent_artifacts/test_analysis_report_payload.py`
- Modify: `backend/tests/agent_artifacts/test_analysis_report_export.py`
- Modify: `backend/tests/agent_artifacts/test_export_cache.py`
- Modify: `backend/tests/agent_artifacts/test_api.py`
- Modify: `backend/tests/agent_runtime/tools/test_direct_artifact_builder.py`
- Modify: `src/api/agentArtifacts.ts`
- Modify: `src/components/artifacts/AnalysisReportView.tsx`
- Modify: `src/components/artifacts/AnalysisReportView.test.tsx`
- Modify: `src/components/artifacts/ArtifactWorkspace.test.tsx`

**Inputs:** Task 3 的 `McpResultSourceV1/resolve_bound_mcp_sources/project_kol_candidates`，现有 `analysis_report_v1` Block 与 `workbook_v1`。

**Outputs:** `analysis-report@3` 不可变 candidate + seed bundle；跨品牌/KOL/自定义列的组合主报告；牛霸霸蓝本产生一个共同表头、一个数据 Sheet、平台列和 20/40 fulfillment；结构化 Workbook limit error。

**Interfaces:**

```python
class AnalysisReportKolProjectionInput(BaseModel):
    target_block_id: str
    scope: KolProjectionScopeV1
    sources: tuple[McpResultSourceV1, ...]
    requested_min: int
    preference: Preference

class AnalysisReportViralPostProjectionInput(BaseModel):
    target_block_id: str
    sources: tuple[McpResultSourceV1, ...]

class AnalysisReportFulfillmentRequest(BaseModel):
    key: Literal["viral_posts", "kol_links"]
    requested_min: int
    target_block_id: str
```

保留 `AnalysisReportV1InputV1` 服务旧 Snapshot；新增 `AnalysisReportV1InputV2`，在其他模型自有字段之外删除模型可写的 `fulfillment`，增加 `kol_projections`、`viral_post_projections` 与 `fulfillment_requests`。服务端 `FULFILLMENT_RULES_V1` 固定：

```python
FULFILLMENT_RULES_V1 = {
    "viral_posts": FulfillmentRule(
        projection_kind="viral_posts_v1",
        record_type="viral_post",
        required_non_null_columns=("platform", "external_id", "viral_rate", "url"),
        unique_key="platform_post_id_or_permalink_hash_v1",
    ),
    "kol_links": FulfillmentRule(
        projection_kind="kol_candidates_v1",
        record_type="kol",
        required_non_null_columns=("platform", "external_id", "url"),
        unique_key="platform_kol_uid_v1",
    ),
}
```

`assemble_analysis_report_payload_v2` 只把 resolver 生成的 internal `BoundProjectionRow` 追加进目标 `typed_table`，并仅从这些带来源标记的行按 registry 规则计算 fulfillment。模型普通 Block 中即使出现相同 `record_type/external_id` 也不计数；模型不能提交 `record_type/required_non_null_columns/unique_by/actual_count/status/reason`。最终 payload 不保存输入 DTO 或内部标记，只保存服务器组装后的行、lineage 与 fulfillment。

`analysis-report@3` 正文主体固定为：

```markdown
# 通用营销分析报告

当标准 Artifact 不能无损表达用户的跨域组合、自定义列、长尾数量或 Workbook 布局时，可以使用 analysis_report_v1；何时选择它由你基于用户目标自主决定，Runtime 不按关键词路由。既有 campaign subject_type 只作共享合同兼容，本 Revision 不增加活动分析阶段、口径或验收。

构建时只提交 title、subject_type、scope、typed blocks、kol_projections、viral_post_projections、fulfillment_requests、availability、limitations、methodology_input 和可选 workbook。两类 projection 只引用当前 Run settled Tool Result 的完整副本与行指针；fulfillment_requests 只声明受审核 key、目标表与 requested_min，不得提交记录类型、列规则、去重键、actual_count、status 或 reason。服务器完成来源绑定和投影后，只从可信投影行确定性计算结果。

跨平台同一 Sheet 必须共用同一列集合并保留 platform 列。链接只能来自真实返回且为 http/https；不得提交公式、宏、脚本或本机路径。数据不足时保留全部真实唯一行并输出 restricted/limitation，不造数、不把 null 当 0、不静默截断。

Workbook 与 BI 只读取同一不可变 Version。浏览器下载不代表服务器获得用户桌面文件系统权限；技术上限错误应保留主报告并如实说明。
```

frontmatter name 固定 `analysis-report`，required_tools 保持 `load_marketing_skill/build_artifact_draft/publish_artifacts`，artifact_contract 保持 `analysis_report_v1`，`model_input_contract_version` 固定 `source_bound_input_v2`。`0052` 只插入 global Revision 3 candidate；不移动 Activation、不修改 campaign Skill/Schema/专属视图。initializer 只登记/校验 candidate，不激活；既有/新环境都在单独真实 UAT 通过后才推广。

- [ ] **Step 1: 写牛霸霸 acceptance RED**

构造当前 Run 的两组 settled Tool Result，adapter commitment 分别绑定两平台、18 条爆文与 27 个唯一达人；模型输入只含完整结果副本、行指针和一个 `cross_platform_details` Block/layout。新增 pytest 名称统一以 `test_post_brand_` 开头、Vitest describe 以 `[post-brand] mixed report` 开头，测试断言：

```python
assert report.schema_version == "analysis_report_v1"
assert report.data_status == "restricted"
assert [(f.key, f.requested_min, f.actual_count, f.status) for f in report.fulfillment] == [
    ("viral_posts", 20, 18, "partial"),
    ("kol_links", 40, 27, "partial"),
]
assert workbook.sheetnames == ["跨平台明细"]
assert headers == ["平台", "记录类型", "名称", "外部ID", "爆文率", "粉丝数", "互动量", "链接", "备注"]
assert {row[0] for row in rows} == {"小红书", "抖音"}
```

再写：模型伪造 40 行或自定义 `unique_by/required_non_null_columns/record_type` 被拒且不改变 actual_count；同平台重复 post ID 去重；无 post ID 只在来源有安全 permalink 时使用其 canonical hash；无两者的行排除并 limitation；普通 typed table 使用 `value_score` 等保留 key 被拒；javascript/file URL 被拒；`=cmd` 被拒或写入时前缀 `'`；超行/列/cell/bytes 返回结构化 code/limit/actual/maximum；cache key 随 Version/layout/exporter 改变。合同共存 RED 断言 v1 Run 仍接旧输入、v2 Run 只接 source refs。bundle/migration RED 断言 Revision 3 正文/digest/contract metadata、migration 不移动 Activation、initializer 不激活 candidate、campaign Skill/Schema/指针无 diff。

- [ ] **Step 2: 运行 RED**

```bash
(cd backend && .venv/bin/pytest -q tests/marketing_skills/test_analysis_report_skill_bundle.py tests/marketing_skills/test_migration_0052.py tests/agent_artifacts/test_analysis_report_payload.py tests/agent_artifacts/test_analysis_report_export.py tests/agent_artifacts/test_export_cache.py tests/agent_artifacts/test_api.py tests/agent_runtime/tools/test_direct_artifact_builder.py -k "post_brand")
```

Expected: bundle/migration 缺失；无 `kol_projections/fulfillment_requests`；现有 limit error 只有字符串 code；组合表断言失败。

- [ ] **Step 3: 实现 server projection 与共同表头**

`BuildArtifactDraftTool` 按冻结 `artifact_input_contract_versions["analysis_report_v1"]` 分派 V1/V2；模型不能选版本。V2 Assembler 先验证模型 Block ID/fulfillment request key 唯一、target 存在且是 typed table，再分别调用 Task 3 的 `kol_candidates_v1/viral_posts_v1` resolver；普通模型表禁止官方评分保留 key且不参与 fulfillment。fulfillment 只在可信投影完成后按 `FULFILLMENT_RULES_V1` 计数；模型无法通过字段或 `unique_by` 扩大结果。牛霸霸 Block 的固定 acceptance 列 key 是：

```python
("platform", "record_type", "name", "external_id", "viral_rate", "followers", "engagement", "url", "note")
```

这不是 Runtime 固定业务模板：它只是该验收请求由 Pi 选择并冻结在 Version 的布局。其他请求可由 Pi 选择不同安全列；同一 Sheet 内被选中的两个平台必须复用同一列集合。

同时用 Task 2 的 register 脚本登记并锁定 `analysis-report-server-fulfillment-v1` bundle digest；`0052` 只插入 candidate Revision，initializer 不设置其指针。

- [ ] **Step 4: 实现结构化技术错误和同版 cache**

`WorkbookTechnicalLimitExceeded` 保存 `limit/actual/maximum`；router 返回：

```python
raise HTTPException(status_code=409, detail={
    "code": error.code,
    "limit": error.limit,
    "actual": error.actual,
    "maximum": error.maximum,
})
```

Exporter 只收 `AgentArtifactVersion.payload_json`；缓存 identity 精确为 `sha256(version_id + exporter_version + layout_digest)`；失败重建只重做 Excel，不调用模型/MCP。

- [ ] **Step 5: 前端呈现**

`AnalysisReportView` 显示 fulfillment status 文案、restricted limitations、安全链接、共同 typed table；下载失败解析结构化错误并显示“工作簿超过技术上限”，不声称已保存到桌面。

- [ ] **Step 6: GREEN 与受影响验证**

```bash
(cd backend && .venv/bin/pytest -q tests/marketing_skills/test_analysis_report_skill_bundle.py tests/marketing_skills/test_migration_0052.py tests/agent_artifacts/test_analysis_report_payload.py tests/agent_artifacts/test_analysis_report_export.py tests/agent_artifacts/test_export_cache.py tests/agent_artifacts/test_api.py tests/agent_runtime/tools/test_direct_artifact_builder.py -k "post_brand" && .venv/bin/ruff check app/agent_artifacts tests/marketing_skills/test_analysis_report_skill_bundle.py tests/marketing_skills/test_migration_0052.py tests/agent_artifacts tests/agent_runtime/tools/test_direct_artifact_builder.py)
npm run test -- src/components/artifacts/AnalysisReportView.test.tsx src/components/artifacts/ArtifactWorkspace.test.tsx -t "post-brand mixed report" && npm run lint
```

Expected: 牛霸霸样例的 BI/Workbook 使用同一 payload/Version；只有 hash 绑定行计入 18/20、27/40 restricted；模型伪造行/去重规则不改变计数；v1/v2 不串版；安全/limit/cache 全绿。

- [ ] **Step 7: 提交**

```bash
git add backend/app/marketing_capability_pack/packs/marketing-v2/bootstrap/analysis-report-server-fulfillment-v1.json backend/app/marketing_capability_pack/packs/marketing-v2/manifest.json backend/migrations/versions/0052_analysis_report_skill_contract.py backend/app/agent_artifacts/model_inputs backend/app/agent_artifacts/payloads/analysis_report.py backend/app/agent_artifacts/exporters backend/app/agent_artifacts/router.py backend/tests/marketing_skills/test_analysis_report_skill_bundle.py backend/tests/marketing_skills/test_migration_0052.py backend/tests/agent_artifacts backend/tests/agent_runtime/tools/test_direct_artifact_builder.py src/api/agentArtifacts.ts src/components/artifacts/AnalysisReportView.tsx src/components/artifacts/AnalysisReportView.test.tsx src/components/artifacts/ArtifactWorkspace.test.tsx
git commit -m "feat(reports): support mixed workbook projections"
```

---

### Task 6：澄清、取消、暂停与继续

**Files:**

- Modify: `backend/app/agent_runtime/tools/pi_internal_tools.py`
- Modify: `backend/app/agent_runtime/repository.py`
- Modify: `backend/app/agent_runtime/engine.py`
- Modify: `backend/app/agent_runtime/router.py`
- Modify: `backend/app/pi_gateway/completion.py`
- Modify: `backend/app/runtime_config/schemas.py`
- Modify: `backend/app/runtime_config/service.py`
- Modify: `backend/tests/pi_gateway/test_internal_tools.py`
- Modify: `backend/tests/pi_gateway/test_terminal_gate.py`
- Modify: `backend/tests/pi_gateway/test_completion_validator.py`
- Modify: `backend/tests/agent_runtime/test_engine.py`
- Modify: `backend/tests/agent_runtime/test_api.py`
- Modify: `backend/tests/runtime_config/test_service.py`
- Modify: `pi-gateway/tests/cancel.test.ts`
- Modify: `src/components/agent/AgentRunCard.tsx`
- Modify: `src/components/agent/AgentRunCard.test.tsx`
- Modify: `src/components/ChatArea.tsx`
- Modify: `src/components/ChatArea.test.tsx`
- Modify: `src/hooks/useAgentWorkspace.ts`
- Modify: `src/hooks/useAgentWorkspace.test.tsx`

**Inputs:** 当前 `request_clarification`、`cancel_requested`、run/attempt/snapshot 状态机。

**Outputs:** clarification 前零副作用；thinking/in-flight cancel 后零新 dispatch、唯一 terminal；paused/resume 同 Snapshot；准确前端按钮/错误。

**Interfaces:**

- Add: `async def assert_clarification_has_no_side_effects(db: AsyncSession, run: AgentRun) -> None`
- Error: `clarification_after_side_effect_not_allowed`
- Add Snapshot field: `completion_requirement: Literal["main_report", "read_only"] | None = None`；所有新 Snapshot 由服务端入口强制写非空值，`None` 仅兼容历史行。
- Preserve: `AgentRunRepository.request_cancel` 只落 durable signal；`cancel` 做最终迁移。

- [ ] **Step 1: 写 clarification RED**

分别创建：只有 `load_skill/get_context` 内部只读调用的 Run、settled 外部 MCP ToolCall Run、reserve ledger Run、DraftRevision Run。只允许第一种进入 clarification；其余返回稳定错误且不写 clarification message/Memory/状态。内部只读调用不等于 MCP 副作用，不能误伤正常的首轮 Skill/Context 装载。

本 Task 新增的 pytest 名称统一以 `test_post_brand_` 开头，Pi Gateway/Vitest 标题统一含 `[post-brand]`，供 Task 9 精确选择。

- [ ] **Step 2: 写取消/恢复 RED**

覆盖：thinking cancel；模型 decide 返回后 cancel；MCP running cancel；reserved-but-never-sent cancel；重复 cancel；paused resume；cancelled resume；terminal 后事件。关键断言：

```python
assert dispatch_spy.await_count == count_at_cancel
assert terminal_types == ["run.cancelled"]
assert running_call.status == "unknown"
assert running_call.points_reserved == 10
assert reserved_unsent.status == "failed"
assert reserved_unsent.error_type == "definitely_not_sent"
assert resumed_run.id == paused_run.id
assert resumed_run.runtime_config_snapshot_json["skill_manifest"] == original_manifest
```

- [ ] **Step 3: 运行 RED**

```bash
(cd backend && .venv/bin/pytest -q tests/pi_gateway/test_internal_tools.py tests/pi_gateway/test_terminal_gate.py tests/pi_gateway/test_completion_validator.py tests/agent_runtime/test_engine.py tests/agent_runtime/test_api.py tests/runtime_config/test_service.py -k "clarification or cancel or resume or completion_requirement")
(cd pi-gateway && npm test -- tests/cancel.test.ts)
```

Expected: clarification side-effect guard 缺失；至少一个跨阶段取消/事件断言失败。

- [ ] **Step 4: 实现可信门和安全点**

clarification guard 只把 `AgentToolCall.service != "internal"` 的外部 MCP 调用、与该 Run 关联的钱包预留/结算/释放、DraftRevision/PublishAttempt/Version 视为副作用；任一存在即拒绝。`load_skill/get_context` 等零积分内部只读调用可以存在。不要检查用户关键词。

取消维持 durable-before-send：外发前先把 reserved→running 提交；因此 cancelled cleanup 仅释放仍可证明未外发的 reserved，running→unknown。所有 dispatch 入口在获取槽位/prepare 之前和真正 send 之前检查 cancel。

`RuntimeConfigSnapshot` 和创建服务新增 server-owned `completion_requirement`：普通消息入口固定 `main_report`，Task 8 的显式 Version 钻取入口固定 `read_only`；模型、prompt 和用户文本不能赋值。CompletionValidator 对新 Snapshot 使用该字段，历史 Snapshot 继续映射 `completion_mode`；clarification/cancel/failed 不走正常 main-report 成功门。

- [ ] **Step 5: 前端按钮与错误**

running/reviewing/thinking 显示“取消分析”，请求后显示“取消中…”并禁用；paused 显示“继续”；clarification 显示答案 chips/input 与“取消本次分析”；terminal 不显示动作。409/404 使用服务端 detail，不创建 fresh Run。

- [ ] **Step 6: GREEN 与受影响验证**

```bash
(cd backend && .venv/bin/pytest -q tests/pi_gateway/test_internal_tools.py tests/pi_gateway/test_terminal_gate.py tests/pi_gateway/test_completion_validator.py tests/agent_runtime/test_engine.py tests/agent_runtime/test_api.py tests/runtime_config/test_service.py -k "clarification or cancel or resume or completion_requirement" && .venv/bin/ruff check app/agent_runtime app/pi_gateway app/runtime_config tests/agent_runtime tests/pi_gateway tests/runtime_config)
(cd pi-gateway && npm test -- tests/cancel.test.ts && npm run typecheck)
npm run test -- src/components/agent/AgentRunCard.test.tsx src/components/ChatArea.test.tsx src/hooks/useAgentWorkspace.test.tsx && npm run lint
```

Expected: 零副作用 clarification、唯一 cancelled、unknown 预留、同 Snapshot resume 和 UI 状态全绿。

- [ ] **Step 7: 提交**

```bash
git add backend/app/agent_runtime backend/app/pi_gateway/completion.py backend/app/runtime_config/schemas.py backend/app/runtime_config/service.py backend/tests/agent_runtime backend/tests/pi_gateway backend/tests/runtime_config/test_service.py pi-gateway/tests/cancel.test.ts src/components/agent/AgentRunCard.tsx src/components/agent/AgentRunCard.test.tsx src/components/ChatArea.tsx src/components/ChatArea.test.tsx src/hooks/useAgentWorkspace.ts src/hooks/useAgentWorkspace.test.tsx
git commit -m "fix(runtime): enforce clarification and cancel boundaries"
```

---

### Task 7：Skill 管理工作台真实验收与 Snapshot 不变性

**Files:**

- Modify: `backend/app/marketing_skills/schemas.py`
- Modify: `backend/app/marketing_skills/service.py`
- Modify: `backend/app/marketing_skills/router.py`
- Modify: `backend/tests/marketing_skills/test_service.py`
- Modify: `backend/tests/marketing_skills/test_api.py`
- Modify: `backend/tests/marketing_skills/test_snapshot.py`
- Modify: `backend/tests/runtime_config/test_snapshots.py`
- Modify: `src/api/contracts.ts`
- Modify: `src/api/skills.ts`
- Modify: `src/api/skills.test.ts`
- Modify: `src/components/admin/SkillAdmin.tsx`
- Modify: `src/components/admin/SkillAdmin.test.tsx`
- Create: `e2e/skill-admin.spec.ts`

**Inputs:** 现有 `AdminAuditLog`、Skill Revision/Activation/rollback/idempotency、Task 2 DB-only Snapshot。

**Outputs:** Active/Previous digest、只读审计时间线、scope 安全 UI、old/new/resume digest 浏览器验收。

**Interfaces:**

```python
class SkillAuditRead(BaseModel):
    id: str
    action: Literal["skill.revision_create", "skill.activate", "skill.rollback"]
    actor_id: str
    target_type: str
    target_id: str
    skill_name: str
    tenant_id: str | None
    revision: int | None
    content_digest: str | None
    rollout_percent: int | None
    created_at: datetime

class SkillAuditListRead(BaseModel):
    items: list[SkillAuditRead]
    total: int
```

- Add: `GET /api/v1/admin/skills/{skill_name}/audit-logs?tenant_id=&limit=100`
- Add to Activation read: `active_content_digest`、`previous_content_digest`。

- [ ] **Step 1: 写后端 RED**

覆盖 create/activate/rollback 各一条审计；相同 Idempotency-Key 重放不增加行；audit list 只包含该 Skill 的 target IDs；tenant filter 只含 global + 指定 tenant；非管理员 403；Activation response 的 active/previous digest 与 Revision 行一致。新增 pytest 统一以 `test_post_brand_skill_admin_` 命名，并使用品牌/KOL 中性 fixture，不复用现有活动命名 fixture。

- [ ] **Step 2: 写 UI/E2E RED**

Vitest `[post-brand] skill admin` 断言：展示 Active/Previous Revision+digest；创建→validate→diff→global/tenant activate→rollout→rollback；审计时间线三类动作；同指纹重试复用 key；Root Policy、Tool Contract Schema、credential 输入不存在。

Playwright `skill-admin.spec.ts` 使用本地测试后端/route fixtures，覆盖管理员完整流程和非管理员 403 页面；不要连接真实模型/DataTap。

- [ ] **Step 3: 运行 RED**

```bash
(cd backend && .venv/bin/pytest -q tests/marketing_skills/test_service.py tests/marketing_skills/test_api.py tests/marketing_skills/test_snapshot.py tests/runtime_config/test_snapshots.py -k "post_brand_skill_admin")
npm run test -- src/api/skills.test.ts src/components/admin/SkillAdmin.test.tsx -t "post-brand"
```

Expected: audit endpoint/type和 digest 字段缺失；UI 时间线断言失败。

- [ ] **Step 4: 实现 API 与 UI**

Service 先解析该 Skill 的 Revision/Activation target IDs，再查询 `AdminAuditLog.target_id IN (...)`；不以未索引 JSON 模糊扫描全表。返回 detail 白名单字段，绝不返回正文。

UI tenant 选择后只允许 global + 当前 tenant Revision 参与 diff/activate；服务端仍按 Revision ID 做最终 scope 校验。Active/Previous 卡片显示 digest 前 12 位并允许复制完整 digest，不显示 secret refs。

- [ ] **Step 5: Snapshot 不变性 GREEN**

在测试中创建 old Run digest A → 激活 B → new Run digest B → old Run resume/recovery digest A。断言 Activation rollback 后新 Run 回到 A，但已创建的 B Run 仍为 B。不要断言模型工具顺序或 Attempt 固定为 1。

- [ ] **Step 6: 受影响验证**

```bash
(cd backend && .venv/bin/pytest -q tests/marketing_skills/test_service.py tests/marketing_skills/test_api.py tests/marketing_skills/test_snapshot.py tests/runtime_config/test_snapshots.py -k "post_brand_skill_admin" && .venv/bin/ruff check app/marketing_skills tests/marketing_skills tests/runtime_config)
npm run test -- src/api/skills.test.ts src/components/admin/SkillAdmin.test.tsx -t "post-brand" && npm run lint
```

Expected: API/UI/快照断言全绿；无 corpus/eval/真实服务调用。

- [ ] **Step 7: 提交**

```bash
git add backend/app/marketing_skills backend/tests/marketing_skills backend/tests/runtime_config src/api/contracts.ts src/api/skills.ts src/api/skills.test.ts src/components/admin/SkillAdmin.tsx src/components/admin/SkillAdmin.test.tsx e2e/skill-admin.spec.ts
git commit -m "feat(admin): expose audited skill lifecycle"
```

---

### Task 8：Artifact SSE、Version、未读与历史钻取

**Files:**

- Create: `backend/app/agent_runtime/drilldown.py`
- Modify: `backend/app/agent_runtime/profiles.py`
- Modify: `backend/app/agent_runtime/prompts.py`
- Modify: `backend/app/agent_runtime/tools/registry.py`
- Modify: `backend/app/agent_runtime/tools/contracts.py`
- Modify: `backend/app/agent_runtime/tools/history.py`
- Modify: `backend/app/runtime_config/schemas.py`
- Modify: `backend/app/runtime_config/service.py`
- Modify: `backend/app/agent_artifacts/schemas.py`
- Modify: `backend/app/agent_artifacts/router.py`
- Modify: `backend/app/pi_gateway/completion.py`
- Modify: `backend/app/pi_gateway/internal_tools.py`
- Modify: `backend/tests/pi_gateway/test_internal_tool_artifact_events.py`
- Modify: `backend/tests/pi_gateway/test_completion_validator.py`
- Modify: `backend/tests/agent_artifacts/test_api.py`
- Modify: `backend/tests/agent_artifacts/test_read_state.py`
- Create: `backend/tests/agent_runtime/test_drilldown.py`
- Modify: `backend/tests/agent_runtime/test_profiles.py`
- Modify: `backend/tests/agent_runtime/test_prompts.py`
- Modify: `backend/tests/agent_runtime/tools/test_registry.py`
- Modify: `backend/tests/agent_runtime/tools/test_history.py`
- Modify: `src/api/agentArtifacts.ts`
- Modify: `src/hooks/useAgentWorkspace.ts`
- Modify: `src/state/agentEvents.ts`
- Modify: `src/state/agentEvents.test.ts`
- Modify: `src/components/artifacts/ArtifactWorkspace.tsx`
- Modify: `src/components/artifacts/ArtifactWorkspace.test.tsx`
- Modify: `e2e/artifact-workspace.spec.ts`

**Inputs:** Task 6 `completion_requirement`；已发布 Version；现有 Pi SSE bridge/read-state/export API。

**Outputs:** 显式 read-only drilldown Run（0 DataTap、0 Artifact 写入）；Pi/agent Artifact SSE 各一次；未读、Version 选择与下载严格同步。

**Interfaces:**

- Add profile: `artifact_drilldown_v1`，allowed actions 仅 `call_tool/complete`，allowed categories 仅 HISTORY，`internal_tool_allowlist=frozenset({"read_artifact", "read_tool_result"})`；禁止 `ask_user`、`search_evidence`、`remember_scope`、CALCULATION、MCP external 与 ARTIFACT tools。
- Extend profile: `AgentProfile.internal_tool_allowlist: frozenset[str] | None = None`；`ToolRegistry.visible_tools/execute` 对所有非 MCP 工具按该名单再次过滤，`None` 保持既有 Profile 行为。
- Add Snapshot DTO: `ArtifactVersionReadScope(artifact_id, version, version_id, payload_hash, allowed_evidence: tuple[ScopedEvidenceSource, ...])`；`ScopedEvidenceSource` 固定 `evidence_id/payload_hash/source_paths`。字段只由入口从 Version `lineage_snapshot_json` 生成。
- Extend `ToolContext.artifact_version_scope: ArtifactVersionReadScope | None`，由 Registry 根据 Run Snapshot 注入并加入服务端保留边界，模型不能覆盖。
- Add prompt: `artifact_drilldown_v1` 明确只能解释冻结 Version、不得澄清逃逸、不得声称刷新数据或创建报告；证据不足时直接说明限制并 complete。
- Add request: `ArtifactVersionDrilldownCreate(question: str, idempotency_key: str)`。
- Add endpoint: `POST /api/v1/agent/artifacts/{artifact_id}/versions/{version}/drilldowns`。
- Add service: `async def create_version_drilldown(db, *, user, artifact_id, version, question, idempotency_key) -> AgentRunRead`。

- [ ] **Step 1: 写只读钻取 RED**

断言指定 v1 创建 user-visible Run，Snapshot 包含 `completion_requirement=read_only` 与精确 `ArtifactVersionReadScope`；scope 的 Evidence ID/hash/path 恰好来自 v1 lineage，不包含同 Artifact v2 或同 Session 其他 Artifact。profile 只看到 `read_artifact/read_tool_result`，`ask_user/search_evidence/remember_scope`/DataTap/build/publish/calculation 均不可见且直接执行也被拒绝；text completion 无主报告可通过；没有 child clarification Run、外部 `AgentToolCall`、MemoryEntry、DraftRevision、PublishAttempt、Version 新行。普通消息 Run 无主报告仍被 `pi_gateway_main_artifact_missing` 拒绝。新增 pytest 统一以 `test_post_brand_` 命名。

`test_history.py` 增加矩阵：`read_artifact` 不传 version、传 latest、同 Artifact v2、同 Session 其他 Artifact、跨 Session 全部拒绝；只有 scope 指定 v1 成功。`read_tool_result` 对 v1 allowlist 成功，对 v2/其他 Artifact/同 Session 未绑定 Evidence 拒绝，并复核 payload hash/source path；错误不泄漏对象存在性。v1 无 Evidence 时仍可读 payload，但不能调用任意 Evidence 搜索。

- [ ] **Step 2: 写 SSE/未读/同版 RED**

Pi bridge 成功 build/update/publish 各发恰好一条；失败不发；同 result 重放按 sequence/idempotency 不增加。ToolRegistry 直接执行旧 agent 路径不触发 Pi bridge。前端收到 published 后重拉目录、出现未读、选 v1 后 GET/export 都带 v1；打开模块推进 read state 后圆点消失。

- [ ] **Step 3: 运行 RED**

```bash
(cd backend && .venv/bin/pytest -q tests/agent_runtime/test_drilldown.py tests/agent_runtime/test_profiles.py tests/agent_runtime/test_prompts.py tests/agent_runtime/tools/test_registry.py tests/agent_runtime/tools/test_history.py tests/pi_gateway/test_internal_tool_artifact_events.py tests/pi_gateway/test_completion_validator.py tests/agent_artifacts/test_api.py tests/agent_artifacts/test_read_state.py -k "post_brand")
npm run test -- src/state/agentEvents.test.ts src/hooks/useAgentWorkspace.test.tsx src/components/artifacts/ArtifactWorkspace.test.tsx -t "post-brand"
```

Expected: drilldown module/profile/endpoint 缺失；至少一个 Version/未读 UI 断言失败。

- [ ] **Step 4: 实现显式入口与 completion**

入口先用 tenant+user+session 归属加载 Artifact/Version；校验并解析该 Version 的 lineage snapshot，再冻结 artifact/version/version ID/payload hash 与精确 Evidence allowlist。`ToolRegistry` 只在 profile 为 drilldown 时注入该 scope；`ReadArtifactTool` 禁止 Draft/latest/其他 ID，`ReadToolResultTool` 只接受 allowlist 且复核 hash/path，`SearchEvidenceTool` 完全不可见。drilldown 不支持 `ask_user`；不能回答时原 Run带 limitation 完成。点击“生成新报告”不复用 drilldown Run，而是调用普通消息入口并显式附带 parent Version context；该新 Run 恢复 main_report 完成要求。

- [ ] **Step 5: 固化 SSE/Version UI**

`append_artifact_tool_events` 仍只在 Pi router 调用，保持 commit 后 broker publish；不要移到 ToolRegistry。Reducer 以 sequence 去重，published payload 的 `version` 更新对应 draft。ArtifactWorkspace 将 `selectedVersion` 作为 payload/export/drilldown 的共同参数。

- [ ] **Step 6: GREEN 与受影响验证**

```bash
(cd backend && .venv/bin/pytest -q tests/agent_runtime/test_drilldown.py tests/agent_runtime/test_profiles.py tests/agent_runtime/test_prompts.py tests/agent_runtime/tools/test_registry.py tests/agent_runtime/tools/test_history.py tests/pi_gateway/test_internal_tool_artifact_events.py tests/pi_gateway/test_completion_validator.py tests/agent_artifacts/test_api.py tests/agent_artifacts/test_read_state.py -k "post_brand" && .venv/bin/ruff check app/agent_runtime/drilldown.py app/agent_runtime/profiles.py app/agent_runtime/prompts.py app/agent_runtime/tools/registry.py app/agent_runtime/tools/contracts.py app/agent_runtime/tools/history.py app/runtime_config app/agent_artifacts app/pi_gateway/internal_tools.py app/pi_gateway/completion.py tests/agent_runtime/test_drilldown.py tests/agent_runtime/test_profiles.py tests/agent_runtime/test_prompts.py tests/agent_runtime/tools/test_registry.py tests/agent_runtime/tools/test_history.py tests/pi_gateway/test_internal_tool_artifact_events.py tests/agent_artifacts)
npm run test -- src/state/agentEvents.test.ts src/hooks/useAgentWorkspace.test.tsx src/components/artifacts/ArtifactWorkspace.test.tsx -t "post-brand" && npm run lint
```

Expected: read-only 只可见冻结 vN payload/lineage、无 ask/search 逃逸、0 DataTap/0 Version；Pi/agent 不双发；未读/Version/导出/钻取同 vN。

- [ ] **Step 7: 提交**

```bash
git add backend/app/agent_runtime/drilldown.py backend/app/agent_runtime/profiles.py backend/app/agent_runtime/prompts.py backend/app/agent_runtime/tools/registry.py backend/app/agent_runtime/tools/contracts.py backend/app/agent_runtime/tools/history.py backend/app/runtime_config/schemas.py backend/app/runtime_config/service.py backend/app/agent_artifacts backend/app/pi_gateway/completion.py backend/app/pi_gateway/internal_tools.py backend/tests/agent_runtime/test_drilldown.py backend/tests/agent_runtime/test_profiles.py backend/tests/agent_runtime/test_prompts.py backend/tests/agent_runtime/tools/test_registry.py backend/tests/agent_runtime/tools/test_history.py backend/tests/pi_gateway/test_internal_tool_artifact_events.py backend/tests/pi_gateway/test_completion_validator.py backend/tests/agent_artifacts src/api/agentArtifacts.ts src/hooks/useAgentWorkspace.ts src/state/agentEvents.ts src/state/agentEvents.test.ts src/components/artifacts/ArtifactWorkspace.tsx src/components/artifacts/ArtifactWorkspace.test.tsx e2e/artifact-workspace.spec.ts
git commit -m "feat(artifacts): add version-bound drilldown"
```

---

### Task 9：定向验证与一次性最终离线验证

**Files:**

- Create: `backend/tests/integration/test_post_brand_functional_offline.py`
- Create: `docs/qa/2026-08-22-post-brand-functional-offline-verification.md`
- Modify: `changelog/2026-08-22.md`

**Inputs:** Task 1–8 的已评审提交。

**Outputs:** 一份只运行一次的离线综合证据，含精确命令、通过/失败/跳过计数、钱包/事件/Version 断言和未运行项。

**Interfaces:** integration test 使用 fake provider/reviewed local MCP fixture；不得读取真实 `.env` 凭证，不得连接 DataTap、真实模型或开发库。

- [ ] **Step 1: 写综合 RED**

测试建立最小本地拓扑并覆盖：

1. 模糊牛霸霸请求先 clarification，0 ToolCall/0 Artifact/0 wallet delta；
2. 回答后由脚本化模型自主选择一组非固定工具序列，发布 restricted analysis report；
3. Workbook 一个 Sheet/共同表头/platform；模型伪造行或 unique rule 不改变来源绑定的 18/20+27/40；
4. KOL 原始事实来自当前 Run settled Result hash/pointer，官方分数与 `project_kol_candidates` 相等；
5. published → message.completed → terminal 顺序；
6. selected Version BI/export hash 一致；
7. read-only drilldown 只能读选定 Version/lineage，无 ask_user/search_evidence，0 MCP/0新 Version；
8. in-flight cancel → unknown/reserved/唯一 cancelled；
9. Skill old/new/resume digest A/B/A，model input contract v1/v2/v1 同步不串版。

Task 1–8 所有新增 pytest 名称必须含 `post_brand`，Vitest/Playwright 标题必须含 `[post-brand]`；Task 9 只用这些选择器汇总，现存活动场景即使位于同一文件也必须被 deselect。

- [ ] **Step 2: 先运行该 integration RED**

```bash
(cd backend && .venv/bin/pytest -q tests/integration/test_post_brand_functional_offline.py)
```

Expected: 若 Task 1–8 有遗漏，失败信息精确指向上述合同；只修对应 Task 的最小代码/测试，再重跑该 integration 文件一次。不要先跑全仓。

- [ ] **Step 3: 受影响 suites 一次汇总**

```bash
(cd backend && .venv/bin/pytest -q tests/marketing_capability_pack/test_loader.py tests/marketing_skills tests/selection/test_projection.py tests/agent_runtime/test_drilldown.py tests/agent_runtime/test_profiles.py tests/agent_runtime/test_prompts.py tests/agent_runtime/test_engine.py tests/agent_runtime/test_api.py tests/agent_runtime/tools/test_registry.py tests/agent_runtime/tools/test_history.py tests/agent_runtime/tools/test_source_binding.py tests/agent_runtime/tools/test_direct_artifact_builder.py tests/agent_runtime/tools/test_calculation.py tests/runtime_config tests/agent_artifacts tests/pi_gateway/test_mcp_result_envelope.py tests/pi_gateway/test_direct_mcp_architecture.py tests/pi_gateway/test_completion_validator.py tests/pi_gateway/test_internal_tool_artifact_events.py tests/pi_gateway/test_internal_tools.py tests/pi_gateway/test_terminal_gate.py tests/integration/test_post_brand_functional_offline.py -k "post_brand")
(cd backend && .venv/bin/ruff check app tests)
(cd pi-gateway && npm test -- tests/main.test.ts tests/skill-snapshot.test.ts tests/cancel.test.ts tests/mcp-accounting-extension.test.ts tests/direct-mcp-result.test.ts -t "post-brand" && npm run typecheck && npm run build)
npm run test -- src/api/skills.test.ts src/components/admin/SkillAdmin.test.tsx src/components/artifacts/KolSelectionArtifactView.test.tsx src/components/artifacts/AnalysisReportView.test.tsx src/components/artifacts/ArtifactWorkspace.test.tsx src/components/agent/AgentRunCard.test.tsx src/components/ChatArea.test.tsx src/hooks/useAgentWorkspace.test.tsx src/state/agentEvents.test.ts -t "post-brand"
npm run lint && npm run build
npm run test:e2e -- e2e/skill-admin.spec.ts e2e/artifact-workspace.spec.ts --grep "\\[post-brand\\]"
```

Expected: 所有被选择的 post-brand 用例通过；记录 passed/failed/skipped/deselected 精确计数；真实服务 marker 未启用；campaign 用例全部 deselected，未执行任何活动专项场景。

- [ ] **Step 4: 静态安全与 Git 检查**

```bash
git diff --check
git log --oneline --decorate origin/main..HEAD
git diff --name-only origin/main...HEAD
```

对 diff 扫描 secret/DSN/Bearer/API key/private key；确认 `.env`、输出缓存、测试数据库转储、真实 MCP content 均未进入 Git。确认迁移只有线性的 `0050`–`0052`：`0050` 只 additive 增加向后兼容合同列并插 Revision，`0051/0052` 只插不可变 candidate Revision；三者都不移动 Activation、不 drop 旧表。确认 initializer 的默认集合仍是成功 B Snapshot、所有 candidate 均 inactive，并且没有 campaign Skill/Schema/专属视图 diff。

- [ ] **Step 5: 写验证记录并提交**

QA 文档记录每条命令的精确计数，明确“未运行真实模型/DataTap/钱包/Web UAT/部署”。

```bash
git add backend/tests/integration/test_post_brand_functional_offline.py docs/qa/2026-08-22-post-brand-functional-offline-verification.md changelog/2026-08-22.md
git commit -m "test(marketing): verify post-brand functions offline"
```

---

### Task 10：真实 UAT 授权包与后续 main 集成边界

**Files:**

- Create: `docs/runbooks/post-brand-functional-uat.md`
- Create: `docs/qa/post-brand-functional-uat-authorization-template.md`
- Create: `backend/tests/integration/post_brand_uat_scenarios.json`
- Create: `backend/tests/integration/test_post_brand_uat_manifest.py`
- Modify: `docs/runbooks/pi-agent-gateway.md`
- Modify: `docs/runbooks/phase-2-runtime.md`

**Inputs:** Task 9 全绿的 HEAD、独立代码审查 C0/I0、用户新授权。

**Outputs:** 只生成授权/执行清单，不运行 UAT；每个场景分别固定 data-bearing Run 与全部用户 Run 上限、预算、停止条件、证据、回滚和明示排除。

**Interfaces:** `backend/tests/integration/post_brand_uat_scenarios.json` 每项字段：

```json
{
  "id": "brand-single-platform",
  "category": "brand",
  "max_data_bearing_runs": 1,
  "max_total_user_runs": 1,
  "requires_explicit_authorization": true,
  "dispatch_budget": 0,
  "model_decision_budget": 0,
  "points_exposure_budget": 0,
  "expected_artifact": "standard_or_analysis_report_v1",
  "forbidden": ["fresh_run_retry", "result_unknown_replay", "campaign"]
}
```

`category` 枚举为 `brand/kol/mixed/interaction/admin/artifact`，`expected_artifact` 枚举为 `standard_or_analysis_report_v1/none`。`data-bearing Run` 指允许 MCP/钱包/Artifact 的分析 Run；0 MCP/0 Artifact/0 wallet delta 的 clarification parent 不计入它，但计入 total。实际预算数值必须在授权模板中由用户批准后写入一次性的执行副本；仓库默认 manifest 的三个 budget 永久为 0，未授权执行必须 fail-closed。

- [ ] **Step 1: 写 manifest RED**

测试要求场景集合恰好覆盖：品牌单平台、品牌双平台/部分、品牌高量/自定义、品牌模糊澄清、KOL 标准评分、KOL 40→实际、KOL 评分不可得、牛霸霸 mixed、thinking cancel、in-flight cancel、Skill global/tenant/rollback、Artifact SSE/Version/read-only drilldown。普通分析/取消场景 `max_data_bearing_runs=1,max_total_user_runs=1`；品牌模糊与牛霸霸 mixed clarification 场景 `max_data_bearing_runs=1,max_total_user_runs=2`；纯 Skill admin 与只读 Artifact 场景 `max_data_bearing_runs=0,max_total_user_runs<=1`。所有项授权 required、默认 budget=0、forbidden 含三项，且 category 不允许 campaign。

- [ ] **Step 2: 运行 RED**

```bash
(cd backend && .venv/bin/pytest -q tests/integration/test_post_brand_uat_manifest.py)
```

Expected: manifest 文件缺失。

- [ ] **Step 3: 写授权包/Runbook**

Runbook 明确：

- UAT 前核验 migration head、服务 import path/cwd/python/sys.path、端口、test tenant、wallet baseline reserved=0、active Skill/Runtime digest；
- 每场景最多一个 data-bearing Run，不 retry/resume/fresh Run 隐藏失败；只有声明 clarification 的场景可先创建一个 0 MCP/0 Artifact/0 wallet delta parent，全部用户 Run 不超过 2；resume 只用于专门的 pause/resume 场景；
- unknown 立即停止该场景，保留预留，走恢复/人工核对；
- 采集 Run/Attempt/ToolCall/ledger/Artifact Version/SSE/BI/Excel hash/浏览器证据；
- KOL/mixed 场景额外核对 ToolCall settled、ledger `response_hash`、Version lineage Evidence hash 与 server projection 行数；finalize 请求仍不含业务 content；
- 管理端场景使用一次性 Revision/Idempotency-Key，结束按授权 rollback；
- UAT tenant 才可按授权临时激活 Revision 4/KOL v2/analysis v2 candidates；新环境 initializer 与 production 默认在 UAT 期间保持已验收 B Snapshot；
- 不触碰历史 Run/Evidence/Version；不部署、不 push、不 main 合并；
- 全部 campaign 场景缺席。

- [ ] **Step 4: GREEN 与文档检查**

```bash
(cd backend && .venv/bin/pytest -q tests/integration/test_post_brand_uat_manifest.py)
git diff --check
```

Expected: manifest 合同通过；文档不含真实密钥/DSN/Bearer/账户凭证。

- [ ] **Step 5: 独立审查与提交**

先由独立只读 reviewer 检查：Pi 自主、来源/hash/评分可信、unknown 账务、Snapshot/输入合同版本、同版、取消、Version-lineage drilldown、活动排除和 data-bearing/total Run 双上限。C0/I0 后：

```bash
git add docs/runbooks/post-brand-functional-uat.md docs/qa/post-brand-functional-uat-authorization-template.md backend/tests/integration/post_brand_uat_scenarios.json backend/tests/integration/test_post_brand_uat_manifest.py docs/runbooks/pi-agent-gateway.md docs/runbooks/phase-2-runtime.md
git commit -m "docs(uat): gate post-brand functional acceptance"
```

- [ ] **Step 6: 停止边界**

提交后状态只能是“等待用户审核并授权单次真实 UAT”。不得自动启动服务、读取真实凭证、执行模型/DataTap/钱包调用、push、部署或合并 main。真实 UAT 通过后也不自动推广：须另开授权任务创建不可变 `post-brand-default-v2` 与 additive 默认迁移，或对既有环境经管理 API 激活；再取得 main 集成授权和当时 HEAD 的 CI/独立审查。失败则回滚 UAT tenant 指针、保留现场，不新建 fresh Run 掩盖。

---

## 计划自审映射

| 规格要求 | Task |
|---|---|
| 当前事实/成功 Snapshot/全 entry Revision ID+scope 映射 | 1 |
| DB 单一事实源、输入合同版本共存、已验收默认/candidate、rollback、新环境 | 2 |
| Tool Result hash 绑定、KOL server-owned score/去重、缺合同 fail-closed | 3 |
| KOL 标准 BI/Excel/fulfillment/同版 | 4 |
| mixed report、牛霸霸、server-owned fulfillment、共同表头、安全 URL、技术错误 | 5 |
| clarification 0 副作用、cancel/unknown、pause/resume | 6 |
| Skill admin、audit、global/tenant、Snapshot A/B/A | 7 |
| Artifact SSE、未读、Version、同版导出、Version-lineage-bound drilldown | 8 |
| 最小 RED、受影响测试、一次综合离线验证 | 1–9 |
| 独立授权、data-bearing/total Run 双上限、无 campaign/main/deploy | 10 |

## 实施交接边界

本计划已拆成 10 个线性依赖 Task。执行必须发生在新的、用户明确授权的实施 Goal/worktree 中；本设计 Goal 到此不选择执行模式、不启动实现。
