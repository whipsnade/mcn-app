# Marketing Capability Pack B0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可版本化的营销能力包，并以纯本地、可追溯的 Builder/Publication/Gate 取代 Pi POC 的业务语义漏洞。

**Architecture:** `marketing_capability_pack` 提供受限的 Pack manifest 与 Skill 加载，并向 Pi Run 提供只读快照。现有 Artifact Builder 继续生成确定性 payload，但新增 canonical lineage 与 publication policy；Gate 迁移为纯数据模块并以离线 fixture 回放验证六案例。

**Tech Stack:** Python 3.11、Pydantic、JSON Schema、pytest、TypeScript/Vitest、openpyxl。

## Global Constraints

- 不调用真实模型、DataTap、钱包或积分；不重跑或修改历史 round。
- 不开放 Pi 通用文件、shell、任意 HTTP 或 builtin tools。
- 不修改数据库结构、前端 API、历史 Artifact Version 或历史输出。
- 每项先写红灯并运行，再最小实现、定向绿灯、范围回归、审查与独立提交。
- 正式数值只经结构化 supporting_paths + canonical lineage 校验；Markdown 正则不是主校验。

---

### Task 1: Capability Pack manifest 与受限文件加载

**Files:** Create `backend/app/marketing_capability_pack/{__init__.py,models.py,loader.py,packs/marketing-v1/**}`; Test `backend/tests/marketing_capability_pack/test_loader.py`.

**Interfaces:** `CapabilityPackLoader.load_manifest(pack_name: str) -> CapabilityPackSnapshot`; `CapabilityPackLoader.load_skill(snapshot, skill_name, requested_version=None) -> LoadedMarketingSkill`。

- [ ] **Step 1: 写红灯测试**：断言同一 manifest digest 稳定；`../`、符号链接、未知文件、digest 失配和包含 `api_key` 的 manifest 均抛 `CapabilityPackError`。
- [ ] **Step 2: 运行红灯**：`cd backend && .venv/bin/pytest tests/marketing_capability_pack/test_loader.py -q`，预期 ImportError。
- [ ] **Step 3: 最小实现**：以 `Path.resolve(strict=True)`、`relative_to(pack_root)`、`is_symlink()` 与 canonical JSON SHA-256 加载 manifest；限定 root policy、6 skills、3 contracts 和版本字段。
- [ ] **Step 4: 运行绿灯与 Ruff**：执行同一 pytest 与 `.venv/bin/ruff check app/marketing_capability_pack tests/marketing_capability_pack`。
- [ ] **Step 5: Commit**：`git add -f backend/app/marketing_capability_pack backend/tests/marketing_capability_pack && git commit -m "feat: add versioned marketing capability pack"`。

### Task 2: Root policy 注入与 `load_marketing_skill`

**Files:** Modify `backend/app/pi_runtime_poc/{comparison.py,runner.py,internal_tools.py}` and `pi-runtime/src/extensions/{internal-tools.ts,poc-runtime.ts}`; Test `backend/tests/pi_runtime_poc/test_marketing_capability_runtime.py`, `pi-runtime/tests/marketing-capability-tools.test.ts`.

**Interfaces:** `build_marketing_system_context(snapshot) -> str`; `LoadMarketingSkillTool.execute({skill_name, requested_version}) -> dict`.

- [ ] **Step 1: 写红灯测试**：捕获 Pi prompt，断言完整 root policy 正文存在；合法 Skill 返回六字段；未知/禁用/穿越/错 digest/跨 Run 和重复加载分别 fail-closed 或幂等；registry 不出现 read/bash/edit/write/grep/find/ls。
- [ ] **Step 2: 运行红灯**：分别运行 Python 与 `cd pi-runtime && npm test -- marketing-capability-tools.test.ts`，预期工具与正文缺失。
- [ ] **Step 3: 最小实现**：在 Run 创建时持久化 Pack snapshot，将正文放到 system context；只向 extension 注册 `load_marketing_skill` 并从 snapshot 返回已校验值，不返回路径、endpoint 或凭证。
- [ ] **Step 4: 运行绿灯与回归**：`pytest -q tests/pi_runtime_poc`、`npm test`、`npm run typecheck`。
- [ ] **Step 5: Commit**：提交信息 `feat: enforce marketing policy and skill loading`。

### Task 3: canonical Evidence 到品牌/活动章节

**Files:** Modify `backend/app/agent_runtime/{normalization.py,evidence.py}` and `backend/app/agent_artifacts/builders/{brand.py,campaign.py,sections.py,raw_rows.py}`; Create `backend/tests/agent_artifacts/test_marketing_canonical_sections.py` and synthetic fixtures.

**Interfaces:** `CanonicalField(path, value, availability, evidence_ids, unit)`; Builder payload carries `canonical_data` and `field_lineage`.

- [ ] **Step 1: 写红灯测试**：以脱敏 shape fixture 断言声量→overview、情感→sentiment、趋势→timestamp/unit/lineage、热帖→可用字段；缺失字段保持 unavailable 而非 0。
- [ ] **Step 2: 运行红灯**：`pytest tests/agent_artifacts/test_marketing_canonical_sections.py -q`，预期字段没有 Evidence lineage。
- [ ] **Step 3: 最小实现**：按工具名与 shape 提取 canonical fields；Builder 仅从 canonical fields 构造 section、availability、limitations 和 field_lineage。
- [ ] **Step 4: 运行绿灯与 Builder 回归**：定向测试、`pytest tests/agent_artifacts -q`、范围 Ruff。
- [ ] **Step 5: Commit**：`feat: map marketing evidence to canonical sections`。

### Task 4: 结构化数值 lineage publication 门禁

**Files:** Modify `backend/app/agent_artifacts/{lineage.py,validation.py,publishing.py,service.py,payloads/{brand.py,campaign.py,kol_selection.py}}`; Test `backend/tests/agent_artifacts/test_publication_lineage.py`.

**Interfaces:** `validate_structured_claims(payload, artifact_version_id, evidence_scope) -> list[ValidationIssue]`; failures prevent `publish_batch`.

- [ ] **Step 1: 写红灯测试**：覆盖无 path、不存在 path、其他 version、unavailable、跨 Run/Session Evidence、narrative 与 canonical 冲突及 partial 无 limitations。
- [ ] **Step 2: 运行红灯**：`pytest tests/agent_artifacts/test_publication_lineage.py -q`，预期错误 Artifact 仍可发布。
- [ ] **Step 3: 最小实现**：对结构化 claims 的 exact JSON Pointer 逐一解析；要求 pointer 指向本 Version available field 与同 Scope Evidence，且 narrative claim 的 value 与 canonical value 相等。
- [ ] **Step 4: 运行绿灯与回归**：定向测试、`pytest tests/agent_artifacts -q`、Ruff。
- [ ] **Step 5: Commit**：`fix: block ungrounded marketing artifact publication`。

### Task 5: KOL scope 与候选发布有效性

**Files:** Modify `backend/app/agent_artifacts/{builders/kol_selection.py,payloads/kol_selection.py,validation.py}` and `backend/app/agent_artifacts/exporters/kol_selection.py`; Test `backend/tests/agent_artifacts/test_kol_publication_validity.py`.

**Interfaces:** `KolSelectionScopeV3`; `validate_kol_candidates(payload) -> list[ValidationIssue]`。

- [ ] **Step 1: 写红灯测试**：空 nickname、unknown platform、无身份、全空评分、scope 丢失、叙事达人不在 items、预算不可回溯和空名单伪发布全部失败。
- [ ] **Step 2: 运行红灯**：`pytest tests/agent_artifacts/test_kol_publication_validity.py -q`，预期无效 item 被接受。
- [ ] **Step 3: 最小实现**：完整保存十一项 scope；只接受允许平台及可验证身份的 candidate；空结果转为 gaps，Exporter/BI 仅消费已发布 Version。
- [ ] **Step 4: 运行绿灯与导出回归**：定向测试、现有 KOL exporter 测试、Ruff。
- [ ] **Step 5: Commit**：`fix: preserve kol scope and reject empty candidates`。

### Task 6: 纯本地 Gate/finalizer 与业务 hard checks

**Files:** Create `backend/app/pi_runtime_poc/gate.py`; Modify `backend/app/pi_runtime_poc/comparison.py`, `backend/scripts/finalize_pi_runtime_poc.py`; Test `backend/tests/pi_runtime_poc/{test_gate.py,test_finalizer_isolation.py}`.

**Interfaces:** `evaluate_case(result, fixture) -> dict[str, bool]`; `finalize_execution(execution, fixture, review) -> Summary`。

- [ ] **Step 1: 写红灯测试**：对十项 hard check 的语义失败断言 `EVALUATED_FAIL`；新 Python 子进程清空 MySQL/模型/DataTap 环境仍能完成 finalizer，且导入列表没有 Settings/SQLAlchemy/Exporter/FastAPI。
- [ ] **Step 2: 运行红灯**：`pytest tests/pi_runtime_poc/test_gate.py tests/pi_runtime_poc/test_finalizer_isolation.py -q`，预期旧 `comparison.py` 依赖或弱检查失败。
- [ ] **Step 3: 最小实现**：迁移纯 dataclass、JSON 解析和 Gate 逻辑；精确检查 expected artifact、Version、Evidence、scope、行为计数与 limitations；summary 只 append-once。
- [ ] **Step 4: 运行绿灯与 POC 回归**：`pytest -q tests/pi_runtime_poc`、`ruff check app/pi_runtime_poc tests/pi_runtime_poc`。
- [ ] **Step 5: Commit**：`refactor: make pi poc finalizer local and strict`。

### Task 7: 六案例离线业务回放与 B0 验收

**Files:** Create `backend/fixtures/pi_runtime_poc/marketing_b0/**`, `backend/tests/pi_runtime_poc/test_marketing_b0_replay.py`; Modify `docs/qa/pi-runtime-poc-rounds.md`, `changelog/2026-08-08.md`.

**Interfaces:** `run_offline_marketing_replay(fixtures) -> Execution`。

- [ ] **Step 1: 写红灯测试**：fake/recorded Pi events 生成品牌、活动、KOL、钻取、澄清、拒答六结果，断言三个正式产物全 hard checks、后三者 0 DataTap 且符合精确 Version/拒答行为。
- [ ] **Step 2: 运行红灯**：`pytest tests/pi_runtime_poc/test_marketing_b0_replay.py -q`，预期 fixture/replay 缺失。
- [ ] **Step 3: 最小实现**：只接入 synthetic Evidence shape、Capability Pack、确定性 Builder/Validator/Exporter 和 fake Pi events，不创建数据库/网络客户端。
- [ ] **Step 4: 完整验证**：按用户要求运行后端 POC pytest/Ruff、Pi `npm test`/typecheck、`git diff --check`、`rg -n agent_settled backend pi-runtime`、密钥扫描、digest 稳定性与 finalizer 无环境子进程测试；如触及 exporter，再运行对应 Builder/Validator/Exporter 测试。
- [ ] **Step 5: Commit**：`test: add offline marketing capability pack gate replay`。

## Plan Self-Review

七项任务分别覆盖 manifest、强制 root policy、受控 Skill、证据映射、数值 lineage、KOL scope、hard checks/finalizer 与六案例离线回放。每一项都有明确文件、接口、红灯、绿灯、回归和提交。全文不含占位接口或未决实现；B1–B7/C 不在任务列表。
