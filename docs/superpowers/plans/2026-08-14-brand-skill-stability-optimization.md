# 品牌 Skill 稳定性优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** 建立可重复的“真实模型 + 冻结原始 MCP Tool Result”评测体系，完成
`brand-research-report` 的稳定性优化，并在严格 A/B Gate 与一次真实 DataTap 端到端验收通过后，
把 `marketing-v2` 晋级到 `1.2.0`。

**Architecture:** 默认测试只验证评测基础设施，不调用外部服务；批量 A/B 使用真实模型与本地
MCP recording/replay gateway，完整原始 Tool Result 不脱敏、不摘要、不裁剪、不改写。生产
FastAPI、Pi Gateway、`load_marketing_skill`、`build_artifact_draft`、
`publish_artifacts`、Publication/Version、BI/Excel 链路保持真实。候选以冻结的 1.1.0
Capability Snapshot 为对照；只有自动 Gate、独立审查和单次真实端到端均通过，才把候选字节复制到
生产 Pack。活动分析与达人圈选不在本计划中实施，品牌 Gate 后分别另写计划。

**Tech Stack:** Python 3.11/3.12、Pydantic v2、pytest/pytest-asyncio、FastAPI、MCP Python SDK、
SQLAlchemy Async/MySQL 8、Node.js/TypeScript、Pi Gateway、Vitest、React/Vite、SHA-256 canonical
JSONL。

**Global Constraints:**

- 从 `main@03a3b4a4c064426a3c5e22bbc4985f7615921255` 新建隔离 worktree 和分支
  `codex/brand-skill-stability-eval`；禁止在脏的主工作树上开发。
- 开发与默认测试阶段不得调用真实模型、DataTap 或真实钱包；外部调用只允许发生在 Task 8、
  Task 9 和 Task 12 的独立授权后。
- 第一轮只改 Capability Pack 文本、reference、评测基础设施与测试；不得修改模型输入 DTO、
  Artifact Schema、Builder/Exporter、Pi Gateway 业务协议、内部工具、Runtime 完成门禁、计费语义
  或数据库迁移。
- 不恢复 Evidence Bridge、`mcp_result_v1`、数据库 Evidence 必经链路、固定 MCP 工具顺序、固定
  工具数量或服务器 required-artifact 门禁。
- Skill 与模型接收完整原始业务 Tool Result。凭证、Authorization header、token、DSN 不属于
  Tool Result，禁止写入 corpus、日志、结果记录或模型上下文。
- 关键范围歧义允许先澄清；澄清轮必须 0 DataTap、0 Artifact，且不算正式执行。范围明确并进入
  品牌专项 Skill 后，本次执行必须发布 `brand_report_v3`；仅文字完成由评测判失败，但不新增
  Runtime 门禁。
- complete 数据发布 complete 报告；空结果、部分缺失或工具失败仍发布合法 restricted 报告。
- 真实模型评测与普通 pytest 不得并行；共享 `kol_insight_test` 时一次只允许一个 pytest/runner。
- 每个任务先写红灯、观察预期失败，再做最小实现、跑绿灯、独立提交；禁止 amend/rebase/squash。
- 每天的实现事实追加到 `changelog/2026-08-14.md` 或执行日对应 changelog，不改写历史记录。

---

## 0. 交付边界与停止状态

本计划只交付两层：

1. 可复用的 Marketing Skill 评测基础设施；
2. 品牌 Skill 的 1.1.0 基线、候选、A/B 结果、1.2.0 晋级和一次真实端到端确认。

本计划不得自动进入活动或达人优化。最终状态只能是以下之一：

- `BRAND_CORPUS_INCOMPLETE`：真实 corpus 未覆盖十类场景，停止；
- `BRAND_SKILL_EVAL_BLOCKED`：评测基础设施、模型或 corpus 身份不满足门禁，停止；
- `BRAND_SKILL_GATE_FAIL`：候选未达到 A/B Gate，停止，不晋级 Pack；
- `BRAND_SKILL_1_2_FUNCTIONAL_FAIL`：冻结评测已过但真实端到端失败，停止，不合并；
- `BRAND_SKILL_1_2_FUNCTIONAL_PASS_WITH_ACCOUNTING_WARNINGS`：功能通过，但存在独立 accounting
  warning；可进入品牌架构审核，不代表 B7 或生产就绪；
- `BRAND_SKILL_1_2_READY_FOR_REVIEW`：功能与账务收口均通过，等待品牌架构审核。

品牌架构审核通过后，下一份计划是活动分析 Skill；不得在本分支顺手修改活动或达人 Skill。

---

## 1. 文件级映射

### 1.1 新增：评测基础设施

- `backend/scripts/marketing_skill_eval/__init__.py`
  - 评测包的公开类型导出；不得在 import 时读取环境变量或连接外部服务。
- `backend/scripts/marketing_skill_eval/contracts.py`
  - `ScenarioMatrix`、`ScenarioDefinition`、`CorpusIndex`、`CorpusEntry`、
    `CapabilityVariantSpec`、`RoundObservation`、`GateSummary` 等严格 DTO。
- `backend/scripts/marketing_skill_eval/canonical.py`
  - canonical JSON、参数规范化、SHA-256、路径边界校验、append-only JSONL hash-chain writer。
- `backend/scripts/marketing_skill_eval/corpus.py`
  - corpus/lock 校验、完整 raw `CallToolResult` 读取、`CORPUS_MISS` 记录；不做业务转换。
- `backend/scripts/marketing_skill_eval/replay_mcp.py`
  - 低层 MCP recording/replay gateway；replay 模式绝不联网，record 模式只在显式授权下转发。
- `backend/scripts/marketing_skill_eval/variants.py`
  - 从冻结 1.1.0 Capability Snapshot 构造 baseline/candidate；只替换允许的 Root/Social/Brand
    文本并重算内容哈希。
- `backend/scripts/marketing_skill_eval/scoring.py`
  - 十类品牌断言、正式执行/澄清判定、首次 Draft、Publication/Version、数据状态、口径和安全
    检查，以及 90%/100%/20% Gate。
- `backend/scripts/marketing_skill_eval/runner.py`
  - 三次重复、baseline/candidate 交错调度、测试租户/会话/Run 生命周期、DB 观测和结果写入。
- `backend/scripts/freeze_marketing_skill_baseline.py`
  - 仅在 Pack 精确等于 1.1.0 已知 digest 时冻结 Capability Snapshot。
- `backend/scripts/lock_marketing_skill_corpus.py`
  - 从 Git-ignored raw corpus 生成可提交的文件清单和 SHA-256 lock。
- `backend/scripts/capture_marketing_skill_corpus.py`
  - 显式授权的真实模型 + recording MCP 采集入口。
- `backend/scripts/run_marketing_skill_eval.py`
  - 只允许真实模型 + replay MCP 的 A/B 入口；没有 capture/外网 fallback。
- `backend/scripts/run_marketing_skill_eval.sh`
  - 安全加载 `backend/.env`/根 `.env`，强制 `APP_ENV=test`、`MYSQL_DATABASE=kol_insight_test`，
    不回显密钥。

### 1.2 新增：冻结基线、候选和场景

- `backend/tests/marketing_skill_eval/fixtures/baseline/marketing-v2-1.1.0-capability.json`
  - 由冻结脚本生成的完整 1.1.0 Snapshot；已知 manifest digest 必须为
    `65d28bb1afdc20c51729f4222fbed929165d0412ad188f59d7d55d5c9e9931ce`。
- `backend/tests/marketing_skill_eval/fixtures/brand/scenarios.json`
  - 十类品牌场景、自然语言请求、formal/clarification 期望、named assertion profile。
- `backend/tests/marketing_skill_eval/fixtures/brand/corpus.lock.json`
  - Task 8 真实采集后生成并提交；只含文件清单、身份和 hash，不含 raw Tool Result。
- `backend/tests/marketing_skill_eval/fixtures/variants/brand-candidate-1/variant.json`
  - 候选允许修改的三个文本文件、候选身份和目标版本。
- `backend/tests/marketing_skill_eval/fixtures/variants/brand-candidate-1/root-policy.md`
- `backend/tests/marketing_skill_eval/fixtures/variants/brand-candidate-1/social-marketing-analyst.md`
- `backend/tests/marketing_skill_eval/fixtures/variants/brand-candidate-1/brand-research-report.md`
- `backend/tests/marketing_skill_eval/fixtures/variants/brand-candidate-1/references/metric-semantics.md`
- `backend/tests/marketing_skill_eval/fixtures/variants/brand-candidate-1/references/restricted-report.md`

### 1.3 新增：自动化测试

- `backend/tests/marketing_skill_eval/test_baseline.py`
- `backend/tests/marketing_skill_eval/test_contracts.py`
- `backend/tests/marketing_skill_eval/test_corpus.py`
- `backend/tests/marketing_skill_eval/test_replay_mcp.py`
- `backend/tests/marketing_skill_eval/test_variants.py`
- `backend/tests/marketing_skill_eval/test_scoring.py`
- `backend/tests/marketing_skill_eval/test_runner.py`
- `backend/tests/integration/test_marketing_skill_eval_topology.py`
  - 只用 loopback provider 测试装配，不作为候选晋级证据。

### 1.4 修改：复用离线生产拓扑

- `backend/tests/integration/pi_uat/harness.py`
  - 参数化模型 Runtime Secret、MCP service 集合、catalog approval 和 eval capability 注入；默认
    行为必须保持现有 fake UAT 完全兼容。
- `backend/tests/integration/pi_uat/test_harness_lifecycle.py`
  - 补外部模型配置不启动第二 fake provider、replay server 启停和失败回收测试。
- `.gitignore`
  - 新增 `/backend/.data/marketing-skill-evals/`；raw corpus、真实模型输出和评测详细记录不得入 Git。

### 1.5 候选通过后才修改：生产 Pack 与回归

- `backend/app/marketing_capability_pack/packs/marketing-v2/policies/root-policy.md`
- `backend/app/marketing_capability_pack/packs/marketing-v2/skills/social-marketing-analyst/SKILL.md`
- `backend/app/marketing_capability_pack/packs/marketing-v2/skills/brand-research-report/SKILL.md`
- `backend/app/marketing_capability_pack/packs/marketing-v2/skills/brand-research-report/references/metric-semantics.md`
- `backend/app/marketing_capability_pack/packs/marketing-v2/skills/brand-research-report/references/restricted-report.md`
- `backend/app/marketing_capability_pack/packs/marketing-v2/manifest.json`
- `backend/tests/marketing_capability_pack/test_loader.py`
- `backend/tests/pi_runtime_poc/test_marketing_capability_runtime.py`
- `docs/runbooks/pi-agent-gateway.md`
- `docs/qa/2026-08-14-brand-skill-evaluation.md`
- `changelog/2026-08-14.md`

不得修改 `campaign-evaluation-report/SKILL.md`、`kol-selection-report/SKILL.md`、模型输入 DTO、
Artifact payload、Builder/Exporter、Pi Gateway production TypeScript、Runtime Config production
service 或迁移文件。

---

## 2. 固定数据契约

### 2.1 Scenario 与 corpus

在 `contracts.py` 使用严格 DTO，字段名固定如下；实现时不得改为任意 dict：

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ScenarioDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(pattern=r"^brand-[a-z0-9-]+$")
    category: Literal[
        "single_platform_complete",
        "multi_platform_complete",
        "period_comparison",
        "partial_sections",
        "empty_result",
        "one_tool_error",
        "mixed_definitions",
        "high_volume",
        "ambiguous_scope",
        "sparse_or_failed",
    ]
    user_prompt: str
    formal_execution: bool
    expected_artifact_type: Literal["brand_report_v3"] | None
    assertion_profile: str
    corpus_namespace: str


class ScenarioMatrix(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["marketing_skill_eval_scenarios_v1"]
    matrix_id: Literal["brand-v1"]
    skill_name: Literal["brand-research-report"]
    repetitions: Literal[3]
    scenarios: tuple[ScenarioDefinition, ...]
```

Corpus index 只引用 Git-ignored 文件；`result_sha256` 对原始文件字节计算，不先 parse 再序列化：

```python
class CorpusEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_id: str
    scenario_id: str
    service: str
    remote_name: str
    normalized_arguments_json: str
    tool_definition_file: str
    result_file: str
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["tool_result", "captured_transport_error"]


class CorpusIndex(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["marketing_skill_eval_corpus_v1"]
    corpus_version: Literal["brand-v1"]
    capture_model: str
    entries: tuple[CorpusEntry, ...]
```

`normalized_arguments_json` 必须使用 UTF-8、`sort_keys=True`、紧凑分隔符和 `allow_nan=False`。
匹配键固定为：

```python
call_key = sha256(
    "\x00".join((scenario_id, service, remote_name, normalized_arguments_json)).encode("utf-8")
).hexdigest()
```

不得把参数值替换、脱敏、裁剪或映射为另一种业务口径。

### 2.2 Round Observation

评分器只读取可审计的运行事实，不读取隐藏 reasoning：

```python
class RoundObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    round_id: str
    variant_id: Literal["baseline-1.1.0", "brand-candidate-1"]
    scenario_id: str
    repetition: int = Field(ge=1, le=3)
    run_id: str | None
    infrastructure_valid: bool
    invalid_reason: str | None
    run_status: str | None
    model_requests: int = Field(ge=0)
    mcp_dispatches: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    draft_calls: int = Field(ge=0)
    first_draft_valid: bool
    publication_version_id: str | None
    artifact_payload: dict[str, object] | None
    corpus_misses: tuple[str, ...]
    safety_violations: tuple[str, ...]
    assertion_failures: tuple[str, ...]
```

详细 `artifact_payload`、Tool Result 和模型上下文只写入 Git-ignored round 目录；提交到
`docs/qa` 的摘要只含身份、hash、计数、Gate 和脱敏后的数据库 UUID 前缀，不复制 raw payload。
这不改变“模型接收数据不脱敏”的运行规则。

### 2.3 晋级 Gate

Gate 的计算规则固定，不允许执行会话临时放宽：

```python
PASS_RATE_MIN = 0.90
SAFETY_RATE_REQUIRED = 1.00
MAX_DRAFT_CALLS = 2
MAX_EFFICIENCY_RATIO = 1.20
REPETITIONS = 3
```

- baseline 和 candidate 各必须有 30 个有效轮次；基础设施无效轮次不进分母，但必须有界补跑，
  每个原始轮次最多补跑一次。
- candidate 通过数必须至少 27/30；澄清场景按“0 MCP、0 Artifact、Run 进入 clarification”评分。
- 9 类正式场景共 27 轮必须每轮都有当前 Run 的 `brand_report_v3` Publication/Version。
- 安全断言必须 30/30；任何服务器字段提交、缺失当零、口径混用、越 allowlist 或编造关键数值
  都直接使候选 Gate FAIL。
- 任一 category 三轮全失败，直接 FAIL。
- 每轮 `draft_calls <= 2`，即首次构造加最多一次字段级纠错；第二次仍失败则该轮失败。
- candidate 的首次 Draft 合法率必须严格高于 baseline。若 baseline 恰为 100%，按已确认设计无法
  满足“严格提高”，应输出 `BRAND_SKILL_GATE_FAIL:first_draft_ceiling`，不得擅自改为不下降。
- 模型请求、MCP dispatch、input token、output token、duration 五项分别以 30 个配对轮次总和计算；
  candidate/baseline 任一比例不得大于 1.20。baseline 为 0 时 candidate 也必须为 0。

---

## Task 1：创建隔离分支并冻结 1.1.0 基线

**Files:**

- Create: `backend/scripts/freeze_marketing_skill_baseline.py`
- Create: `backend/tests/marketing_skill_eval/test_baseline.py`
- Create: `backend/tests/marketing_skill_eval/fixtures/baseline/marketing-v2-1.1.0-capability.json`
- Modify: `.gitignore`

**Step 1: 建 worktree 并核验基线**

使用 `superpowers:using-git-worktrees`。只允许从以下身份创建：

```bash
git rev-parse HEAD
# Expected: 03a3b4a4c064426a3c5e22bbc4985f7615921255

git status --porcelain
# Expected: no output
```

创建 `codex/brand-skill-stability-eval`；若目标目录或分支已存在，先只读检查归属，不删除、不覆盖。

**Step 2: 写冻结基线红灯**

`test_baseline.py` 先写：

```python
from pathlib import Path

from app.marketing_capability_pack.runtime import MarketingRunCapability

EXPECTED_MANIFEST = "65d28bb1afdc20c51729f4222fbed929165d0412ad188f59d7d55d5c9e9931ce"


def test_frozen_marketing_v2_1_1_snapshot_is_self_validating() -> None:
    path = Path(__file__).parent / "fixtures/baseline/marketing-v2-1.1.0-capability.json"
    capability = MarketingRunCapability.model_validate_json(path.read_text(encoding="utf-8"))

    assert capability.pack_version == "1.1.0"
    assert capability.manifest_digest == EXPECTED_MANIFEST
    assert {skill.version for skill in capability.skills} == {"1.1.0"}
    assert capability.load_skill("brand-research-report", "1.1.0")["content"]
```

运行：

```bash
cd backend
.venv/bin/pytest -q tests/marketing_skill_eval/test_baseline.py
```

Expected: FAIL，fixture 尚不存在。

**Step 3: 实现只认已知基线的冻结脚本**

脚本必须先用生产 loader 读取当前 Pack，再执行双重身份校验：

```python
EXPECTED_PACK_VERSION = "1.1.0"
EXPECTED_MANIFEST_DIGEST = "65d28bb1afdc20c51729f4222fbed929165d0412ad188f59d7d55d5c9e9931ce"

capability = build_marketing_run_capability(model_version="eval-baseline-1.1.0")
if capability.pack_version != EXPECTED_PACK_VERSION:
    raise SystemExit("baseline_pack_version_mismatch")
if capability.manifest_digest != EXPECTED_MANIFEST_DIGEST:
    raise SystemExit("baseline_manifest_digest_mismatch")
```

输出用 `model_dump_json(indent=2)`，文件末尾一个换行。脚本只允许写入命令行明确指定且位于
`backend/tests/marketing_skill_eval/fixtures/baseline/` 的目标文件。

**Step 4: 忽略 raw corpus**

在根 `.gitignore` 追加精确规则：

```gitignore
/backend/.data/marketing-skill-evals/
```

不得忽略整个 `backend/.data`，避免掩盖其他受控文件。

**Step 5: 生成、验证、提交**

```bash
cd backend
.venv/bin/python scripts/freeze_marketing_skill_baseline.py \
  --output tests/marketing_skill_eval/fixtures/baseline/marketing-v2-1.1.0-capability.json
.venv/bin/pytest -q tests/marketing_skill_eval/test_baseline.py
.venv/bin/ruff check scripts/freeze_marketing_skill_baseline.py tests/marketing_skill_eval/test_baseline.py
git diff --check
```

Expected: PASS。

Commit:

```bash
git add .gitignore backend/scripts/freeze_marketing_skill_baseline.py \
  backend/tests/marketing_skill_eval/test_baseline.py \
  backend/tests/marketing_skill_eval/fixtures/baseline/marketing-v2-1.1.0-capability.json
git commit -m "test(marketing-skills): freeze capability pack 1.1.0 baseline"
```

---

## Task 2：实现严格 DTO、canonical bytes 和 corpus lock 校验

**Files:**

- Create: `backend/scripts/marketing_skill_eval/__init__.py`
- Create: `backend/scripts/marketing_skill_eval/contracts.py`
- Create: `backend/scripts/marketing_skill_eval/canonical.py`
- Create: `backend/scripts/marketing_skill_eval/corpus.py`
- Create: `backend/scripts/lock_marketing_skill_corpus.py`
- Create: `backend/tests/marketing_skill_eval/test_contracts.py`
- Create: `backend/tests/marketing_skill_eval/test_corpus.py`

**Step 1: 写 DTO 和越界红灯**

测试至少覆盖：

- 缺字段、多字段、非法 category、重复 scenario ID 被拒绝；
- 正常参数中中文、数组、嵌套对象保留，dict key 排序但数组顺序不变；
- `NaN`/`Infinity` 被拒绝；
- `../`、绝对路径、symlink 逃逸被拒绝；
- raw result 文件任意一个字节变化都会得到 `corpus_hash_mismatch`；
- loader 返回的 `raw_bytes` 与文件读取结果完全相等；
- PII/业务字段不被替换或遮盖；
- lock 缺文件、多文件、重复 entry 或 index hash 不一致均 fail-closed。

先运行：

```bash
cd backend
.venv/bin/pytest -q \
  tests/marketing_skill_eval/test_contracts.py \
  tests/marketing_skill_eval/test_corpus.py
```

Expected: collection FAIL，模块尚不存在。

**Step 2: 实现 canonical primitives**

核心函数签名固定：

```python
def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def normalize_arguments(arguments: dict[str, object]) -> str:
    return canonical_json_bytes(arguments).decode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
```

`safe_child(root, relative)` 必须 `resolve(strict=True)` 后执行 `relative_to(root.resolve())`，并拒绝
symlink。这里不扫描或改写业务数据；安全边界是路径与凭证不进入 corpus，而不是对 Tool Result 做
内容脱敏。

**Step 3: 实现 corpus lock**

`corpus.lock.json` 格式固定：

```json
{
  "schema_version": "marketing_skill_eval_corpus_lock_v1",
  "corpus_version": "brand-v1",
  "index_file": "index.json",
  "index_sha256": "64-lowercase-hex",
  "files": [
    {"path": "results/<entry-id>.json", "sha256": "64-lowercase-hex"}
  ]
}
```

数组按 path 排序。lock 脚本只读取 raw root 并输出 lock；不得复制 raw payload 到仓库。

**Step 4: 跑绿灯和提交**

```bash
cd backend
.venv/bin/pytest -q \
  tests/marketing_skill_eval/test_contracts.py \
  tests/marketing_skill_eval/test_corpus.py
.venv/bin/ruff check scripts/marketing_skill_eval scripts/lock_marketing_skill_corpus.py \
  tests/marketing_skill_eval/test_contracts.py tests/marketing_skill_eval/test_corpus.py
git diff --check
```

Commit:

```bash
git add backend/scripts/marketing_skill_eval backend/scripts/lock_marketing_skill_corpus.py \
  backend/tests/marketing_skill_eval/test_contracts.py \
  backend/tests/marketing_skill_eval/test_corpus.py
git commit -m "feat(marketing-skill-eval): verify immutable raw corpora"
```

---

## Task 3：实现 recording/replay MCP gateway

**Files:**

- Create: `backend/scripts/marketing_skill_eval/replay_mcp.py`
- Create: `backend/tests/marketing_skill_eval/test_replay_mcp.py`

**Step 1: 写 replay 红灯**

使用本地临时 corpus 和 MCP ClientSession，覆盖：

1. `list_tools` 原样返回捕获的 name/description/inputSchema/outputSchema；
2. 相同 service/tool/规范化参数返回可由 `CallToolResult.model_validate_json(raw_bytes)` 解析的
   完整 result；`content`、`structuredContent`、`isError` 和 `_meta` 不变；
3. dict key 顺序不同但语义相同能命中；数组顺序不同不能命中；
4. 未命中返回稳定 `CORPUS_MISS:<call_key>`，记录 miss，外部 forwarder 调用次数为 0；
5. replay 模式即使环境存在真实 token，也绝不实例化外部 http client；
6. record 模式只有显式 guard 为真才允许 forward；headers/token 不写入结果文件；
7. result 写入逐文件 flush+fsync，写完后再原子更新 index；中断时不得留下已入 index 的半文件；
8. `captured_transport_error` 不伪装成业务成功 result。

先运行：

```bash
cd backend
.venv/bin/pytest -q tests/marketing_skill_eval/test_replay_mcp.py
```

Expected: FAIL，模块尚不存在。

**Step 2: 用 low-level MCP Server 保留完整结果对象**

不得用会重建 output shape 的业务 normalizer。核心注册方式：

```python
from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, TextContent, Tool

server = Server("marketing-skill-eval-replay")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return corpus.tools_for_service(service_slug)


@server.call_tool(validate_input=True)
async def call_tool(name: str, arguments: dict[str, object]) -> CallToolResult:
    entry = corpus.lookup(scenario_id, service_slug, name, arguments)
    if entry is None:
        call_key = corpus.record_miss(scenario_id, service_slug, name, arguments)
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=f"CORPUS_MISS:{call_key}")],
        )
    return CallToolResult.model_validate_json(entry.raw_result_bytes)
```

Starlette 挂载路径保持生产契约：
`/api/gateway/<service-slug>/mcp`。使用 `StreamableHTTPSessionManager(stateless=True)`，生命周期
必须在 startup failure、cancel 和正常退出三条路径有界收口。

**Step 3: 实现 record forwarder**

record 模式的 forwarder 使用 MCP Python ClientSession 直接得到原始 `CallToolResult` 对象，再以
`model_dump_json(by_alias=True, exclude_none=True)` 写入 corpus。禁止经过
`DataTapTransport._classify_result`、Evidence writer 或业务 schema normalizer。真实 Authorization
header 只存在于内存中的 `httpx.AsyncClient`；logger 只能写 service/tool/call_key/status/duration。

**Step 4: 绿灯、协议回环和提交**

```bash
cd backend
.venv/bin/pytest -q tests/marketing_skill_eval/test_replay_mcp.py
.venv/bin/ruff check scripts/marketing_skill_eval/replay_mcp.py \
  tests/marketing_skill_eval/test_replay_mcp.py
git diff --check
```

Commit:

```bash
git add backend/scripts/marketing_skill_eval/replay_mcp.py \
  backend/tests/marketing_skill_eval/test_replay_mcp.py
git commit -m "feat(marketing-skill-eval): replay recorded mcp results"
```

---

## Task 4：实现 Capability Variant，保证 A/B 只差允许文本

**Files:**

- Create: `backend/scripts/marketing_skill_eval/variants.py`
- Create: `backend/tests/marketing_skill_eval/test_variants.py`

**Step 1: 写不可变差异红灯**

测试固定断言：

- baseline 从冻结 JSON 读取，不读当前 production pack；
- candidate 只允许替换 root policy、`social-marketing-analyst`、
  `brand-research-report` 三个 content；
- candidate 不得修改 skills 集合、required_tools、artifact_contract、contracts、builder/exporter
  version、campaign/KOL/其他 Skill；
- 所有替换文本 digest 重算，未替换条目逐字节相同；
- candidate 的 `manifest_digest` 是
  `sha256(canonical_json_bytes({baseline_manifest, variant_id, overlay_hashes}))`，可重复；
- runtime `model_version` 可按本轮真实模型身份更新，但不进入 variant diff；
- overlay 路径越界、额外文件或声明与实际文件不一致均拒绝。

先运行：

```bash
cd backend
.venv/bin/pytest -q tests/marketing_skill_eval/test_variants.py
```

Expected: FAIL。

**Step 2: 实现唯一构造入口**

```python
ALLOWED_OVERLAYS = {
    "root_policy",
    "social-marketing-analyst",
    "brand-research-report",
}


def build_variant(
    baseline: MarketingRunCapability,
    spec: CapabilityVariantSpec,
    *,
    model_version: str,
) -> MarketingRunCapability:
    """Return a fully validated immutable eval snapshot; never mutate baseline."""
```

baseline variant 直接复制冻结 Snapshot，只把 `model_version` 设为本轮模型；candidate variant
对三处内容做显式 replace，skill version 使用 `1.2.0-candidate.1`，pack version 使用
`1.2.0-candidate.1`。这些是评测身份，不是生产版本。

**Step 3: 绿灯和提交**

```bash
cd backend
.venv/bin/pytest -q tests/marketing_skill_eval/test_variants.py
.venv/bin/ruff check scripts/marketing_skill_eval/variants.py \
  tests/marketing_skill_eval/test_variants.py
git diff --check
```

Commit:

```bash
git add backend/scripts/marketing_skill_eval/variants.py \
  backend/tests/marketing_skill_eval/test_variants.py
git commit -m "feat(marketing-skill-eval): isolate capability variants"
```

---

## Task 5：实现可审计 scorer 与 append-only 结果

**Files:**

- Create: `backend/scripts/marketing_skill_eval/scoring.py`
- Create: `backend/tests/marketing_skill_eval/test_scoring.py`

**Step 1: 写 Gate 红灯**

构造纯内存 RoundObservation，至少覆盖：

- 27/30 通过恰好通过，26/30 失败；
- safety 29/30 即失败；
- formal round 无 Publication/Version 即失败，即使 Run completed；
- ambiguous round 有任何 MCP 或 Artifact 即失败；
- restricted 数据正确标状态/limitation 通过，把空结果写 0 失败；
- `draft_calls=2` 通过，3 失败；
- 首 Draft 未严格高于 baseline 失败；baseline 100% 输出 ceiling failure；
- 任一效率比例 1.2000 通过，1.2001 失败；
- 某 category 0/3 直接失败；
- `CORPUS_MISS` 和 provider transient 不进分数，但同一轮第二次仍无效时阻断整个 Gate；
- hash-chain 被改一字节时 verifier 报错；
- scorer 不读取 thinking_text/reasoning 字段。

先运行：

```bash
cd backend
.venv/bin/pytest -q tests/marketing_skill_eval/test_scoring.py
```

Expected: FAIL。

**Step 2: 实现 named assertion registry**

不允许场景 JSON 携带 Python 表达式。固定注册：

```python
BRAND_ASSERTIONS = {
    "brand_single_platform": score_single_platform,
    "brand_multi_platform": score_multi_platform,
    "brand_period_comparison": score_period_comparison,
    "brand_partial_sections": score_partial_sections,
    "brand_empty_result": score_empty_result,
    "brand_one_tool_error": score_one_tool_error,
    "brand_mixed_definitions": score_mixed_definitions,
    "brand_high_volume": score_high_volume,
    "brand_ambiguous_scope": score_ambiguous_scope,
    "brand_sparse_or_failed": score_sparse_or_failed,
}
```

每个正式场景先执行共同断言：

1. Run terminal 为 `completed`；
2. 当前 tenant/session/run 恰有一个目标 Artifact Version；
3. payload 可由 `BrandReportV3.model_validate`；
4. `draft_calls` 为 1 或 2；
5. 没有 running/unknown ToolCall，replay 账务 reserved 为 0；
6. Artifact scope 与场景 scope 一致；
7. unavailable/partial 章节不出现用 0 替代缺失的关键字段；
8. narrative 中出现的数字必须存在于结构化 data 或确定性比较结果；
9. corpus oracle 声明的关键字段，通过 `literal`/`sum`/`ratio`/`delta` 四种受控运算从原始
   Tool Result 计算并与 Artifact JSON Pointer 对比；不使用模型 judge。

Oracle 规则保存在 Git-ignored corpus index，仓库场景只声明必须覆盖的 semantic fact 名；lock
对 oracle 文件同样做 SHA-256。不得把缺 oracle 的关键字段当通过。

**Step 3: 实现 append-only JSONL**

每个 record 包含 `sequence`、`prev_hash`、`record_hash`；写入顺序固定为 canonical body →
hash → 单行写入 → flush → fsync。已有 round 目录禁止覆盖。最终 summary 从已验证 JSONL 重算，
不能信任内存累计值。

**Step 4: 绿灯和提交**

```bash
cd backend
.venv/bin/pytest -q tests/marketing_skill_eval/test_scoring.py
.venv/bin/ruff check scripts/marketing_skill_eval/scoring.py \
  tests/marketing_skill_eval/test_scoring.py
git diff --check
```

Commit:

```bash
git add backend/scripts/marketing_skill_eval/scoring.py \
  backend/tests/marketing_skill_eval/test_scoring.py
git commit -m "feat(marketing-skill-eval): score stability and efficiency gates"
```

---

## Task 6：参数化生产拓扑并实现真实模型 A/B runner

**Files:**

- Modify: `backend/tests/integration/pi_uat/harness.py`
- Modify: `backend/tests/integration/pi_uat/test_harness_lifecycle.py`
- Create: `backend/tests/integration/test_marketing_skill_eval_topology.py`
- Create: `backend/scripts/marketing_skill_eval/runner.py`
- Create: `backend/scripts/run_marketing_skill_eval.py`
- Create: `backend/scripts/run_marketing_skill_eval.sh`
- Create: `backend/tests/marketing_skill_eval/test_runner.py`

**Step 1: 写兼容与 fail-closed 红灯**

先覆盖：

- `PiUatTopology()` 默认仍启动现有 FakeModel/FakeDataTap，现有生命周期事件顺序不变；
- 传入 `RuntimeEndpointSecrets` 时，Runtime Config 使用传入的 model endpoint 和本地 replay MCP
  URLs；不得把 SecretStr 写日志或 dataclass repr；
- eval capability 只能在 draft RuntimeConfig 上注入，随后仍走生产 `activate` 校验；active/retired
  row 禁止修改；
- recording/replay server 启动失败时 task、listener、PGID、端口和测试数据全部回收；
- runner 未设置 `RUN_MARKETING_SKILL_EVAL=1` 立即退出；
- compare 模式发现 DataTap origin 是真实域名立即退出；它只能访问 loopback replay；
- model API key 为测试占位值或缺失立即退出；
- 数据库不是 `kol_insight_test`、用户不是 `kol_test@localhost` 或能访问 `kol_insight.users` 时退出；
- runner 两个 variant 用不同 tenant，新 session/Run；每个 repetition 重新建拓扑并交换 tenant 分配；
- 每个 repetition 经正式 admin wallet-adjust/quota API 把两个测试租户分别置备为
  balance=100000、reserved=0、用户月额度=100000；Idempotency-Key 含 round/repetition/tenant，
  并断言 admin_adjust 账本恒等式，禁止直接 UPDATE 钱包；
- 同一 scenario 的 baseline/candidate 相邻交错，顺序由固定 seed 决定；
- 每个 Run 的本地 replay dispatch 紧急上限为 50；达到上限记为 Skill failure
  `eval_replay_dispatch_limit`，不得创建 fresh Run 隐藏失败；
- provider transient 只补跑该 observation 一次；Skill failure 不自动重跑；
- output round 目录已存在时拒绝覆盖。

先运行：

```bash
cd backend
.venv/bin/pytest -q \
  tests/integration/pi_uat/test_harness_lifecycle.py \
  tests/integration/test_marketing_skill_eval_topology.py \
  tests/marketing_skill_eval/test_runner.py
```

Expected: FAIL。

**Step 2: 只参数化测试 harness，不改生产 Runtime**

新增测试基础设施 DTO：

```python
@dataclass(frozen=True, repr=False)
class RuntimeEndpointSecrets:
    model_base_url: str
    model_name: str
    model_provider: str
    model_api_key: SecretStr
    datatap_token: SecretStr


@dataclass(frozen=True)
class EvalCapabilityAssignment:
    tenant_slug: str
    capability: MarketingRunCapability
```

`PiUatTopology` 新参数必须有默认值，现有 28 个离线 UAT 调用点不改。通用 catalog approval 从
MCP service 的实际 `list_tools` 与 `DYNAMIC_TOOL_ALLOWLIST` 交集生成，不再在 eval 路径硬编码
工具列表；默认 fake 路径的最终集合必须与当前一致。

Capability 注入流程固定：经 admin API 创建 draft config → test harness 在同一测试库事务内锁定
该 draft row → 只替换 `config_json["capability_pack"]` → 赋回完整新 dict → commit → 经 admin API
activate。生产 `RuntimeConfigService.update_version` 仍保持 immutable；本计划不增加 eval 后门。

**Step 3: 实现交错 runner**

调度伪代码固定：

```python
for repetition in (1, 2, 3):
    assignments = (
        ("baseline-1.1.0", "tenant-a", "brand-candidate-1", "tenant-b")
        if repetition % 2
        else ("brand-candidate-1", "tenant-a", "baseline-1.1.0", "tenant-b")
    )
    async with build_eval_topology(assignments) as topology:
        for scenario in deterministic_scenario_order(matrix, repetition):
            for variant in deterministic_pair_order(scenario, repetition):
                await execute_and_observe(topology, variant, scenario, repetition)
```

每个执行创建新 Session；不能复用上一场景上下文。Run 创建前记录 DB 计数，结束后按当前
tenant/session/run 做 delta 对账。模型请求数/tokens 从 `runtime_usage_records` 读取，MCP 从
`agent_tool_calls.dispatch_count` 读取，Draft/Publish 次数从持久 `agent_events` 的 internal tool
事件读取，Artifact 从 `agent_artifact_versions.source_run_id` 读取。

测试钱包只代表本地 replay accounting。登录触发 welcome grant 后，runner 必须通过
`POST /api/v1/admin/tenants/{tenant_id}/wallet/adjust` 把余额补至 100000，并通过
`PUT /api/v1/admin/tenants/{tenant_id}/quota/{user_id}` 把月额度设为 100000；每次调整后读取钱包
和账本核对，cleanup 只清理本 topology 新建身份，不触碰其他测试数据。

**Step 4: shell guard**

`run_marketing_skill_eval.sh` 仿照现有真实 UAT 脚本加载 env，但强制：

```bash
export APP_ENV=test
export AUTH_MODE=mock
export MYSQL_HOST=127.0.0.1
export MYSQL_PORT=3306
export MYSQL_DATABASE=kol_insight_test
export MYSQL_USER=kol_test
export MYSQL_PASSWORD=test-only-password
export RUN_MARKETING_SKILL_EVAL=1
```

脚本不得 `set -x`，不得 echo URL query、token、header、DSN 或 raw prompt/result。

**Step 5: 跑绿灯与既有 UAT 回归**

```bash
cd backend
.venv/bin/pytest -q \
  tests/integration/pi_uat/test_harness_lifecycle.py \
  tests/integration/test_marketing_skill_eval_topology.py \
  tests/marketing_skill_eval/test_runner.py
.venv/bin/pytest -q tests/integration/test_pi_gateway_offline_uat.py
.venv/bin/ruff check scripts/marketing_skill_eval scripts/run_marketing_skill_eval.py \
  tests/integration/pi_uat tests/integration/test_marketing_skill_eval_topology.py \
  tests/marketing_skill_eval
git diff --check
```

Expected: 定向全绿；完整离线 UAT 28 场景全绿。测试中的 loopback provider 只证明装配，不得写入
promotion summary。

Commit:

```bash
git add backend/tests/integration/pi_uat/harness.py \
  backend/tests/integration/pi_uat/test_harness_lifecycle.py \
  backend/tests/integration/test_marketing_skill_eval_topology.py \
  backend/scripts/marketing_skill_eval/runner.py \
  backend/scripts/run_marketing_skill_eval.py \
  backend/scripts/run_marketing_skill_eval.sh \
  backend/tests/marketing_skill_eval/test_runner.py
git commit -m "test(marketing-skills): run real-model replay evaluations"
```

---

## Task 7：建立品牌十类矩阵与候选 Skill

**Files:**

- Create: `backend/tests/marketing_skill_eval/fixtures/brand/scenarios.json`
- Create: `backend/tests/marketing_skill_eval/fixtures/variants/brand-candidate-1/variant.json`
- Create: `backend/tests/marketing_skill_eval/fixtures/variants/brand-candidate-1/root-policy.md`
- Create: `backend/tests/marketing_skill_eval/fixtures/variants/brand-candidate-1/social-marketing-analyst.md`
- Create: `backend/tests/marketing_skill_eval/fixtures/variants/brand-candidate-1/brand-research-report.md`
- Create: `backend/tests/marketing_skill_eval/fixtures/variants/brand-candidate-1/references/metric-semantics.md`
- Create: `backend/tests/marketing_skill_eval/fixtures/variants/brand-candidate-1/references/restricted-report.md`
- Create: `backend/tests/marketing_skill_eval/test_brand_candidate.py`

**Step 1: 写矩阵/文本静态红灯**

断言：

- categories 恰好十种、每种一次、repetitions 恰为 3；
- ambiguous 场景 `formal_execution=false`、artifact 为 null；其余九类均为
  `formal_execution=true` 和 `brand_report_v3`；
- prompt 不含 MCP 工具名、`artifact_type`、JSON Schema、Builder 次数或预计算答案；
- candidate 必须含“正式执行必须发布”“缺数据发布 restricted”“最多一次字段级纠错”“不改变
  平台/期间/单位/指标口径”等语义；
- candidate 不含 `search_evidence`、`read_tool_result`、`mcp_result_v1`、固定工具清单、固定调用
  顺序或服务器 required gate；
- Skill 正文 UTF-8 字节数不超过 8 KiB，不含 JSON Schema 的 `$defs`/`properties` dump；
- reference 明确标注“不会被 production loader 自动加载”；所有 Runtime 必需规则都能在
  `brand-research-report.md` 找到；
- Root/Social/Brand 对澄清与报告必达语义无冲突。

运行：

```bash
cd backend
.venv/bin/pytest -q tests/marketing_skill_eval/test_brand_candidate.py
```

Expected: FAIL，fixture 尚不存在。

**Step 2: 写十条自然语言请求**

场景请求固定为：

1. `请生成蔚来在 2026-07-01 至 2026-07-31 的小红书品牌社媒分析报告，不需要环比或同比。`
2. `请生成蔚来在 2026-07-01 至 2026-07-31 的小红书和抖音品牌社媒分析报告，并分别说明平台表现。`
3. `请生成蔚来 2026 年 7 月的小红书和抖音品牌报告，同时给出相对 2026 年 6 月的环比和相对 2025 年 7 月的同比。`
4. `请生成蔚来 2026 年 7 月的小红书品牌报告；若部分章节没有数据，也要如实发布受限报告。`
5. `请生成“品牌空结果样本 20260814”在 2026 年 7 月的小红书品牌报告，空结果不得当成零。`
6. `请生成蔚来 2026 年 7 月的小红书品牌报告；若某一数据源调用失败，请基于已返回数据发布受限报告。`
7. `请生成蔚来 2026 年 7 月的小红书和抖音品牌报告，严格区分不同来源的指标定义、单位和期间。`
8. `请生成华为 2026 年 7 月的全平台品牌社媒报告，保留关键趋势、话题和代表帖子。`
9. `请分析一下小米。`
10. `请为“品牌稀疏样本 20260814”生成 2026 年 7 月的小红书品牌报告；即使关键查询没有数据，也要发布说明限制的受限报告。`

这些 prompt 只表达业务目标，不暗示工具和 payload。真实 capture 若证明固定实体/期间无法得到对应
类别，Task 8 输出 `BRAND_CORPUS_INCOMPLETE`，不得在同一授权内换成未经记录的结果。

**Step 3: 写候选 Brand Skill**

候选正文使用以下结构和语义；允许润色句子，但测试中的行为条款不得删除：

```markdown
---
name: brand-research-report
description: 自适应研究品牌社媒表现，并稳定发布同版本 brand_report_v3、BI 与 Excel。
---

# 品牌社媒研究报告

## 执行入口

确认品牌实体、日期窗口、平台和比较需求。只有这些关键范围仍存在会改变查询与报告口径的歧义时，
调用 request_clarification 提出一个最关键问题并立即停止；该澄清轮不查询、不建报告。范围明确并
进入本 Skill 后即为正式执行，本次执行必须以发布 brand_report_v3 结束，不能只输出文字说明。

先调用一次 load_marketing_skill，读取本 Skill 与动态 model_input_contract。不要复制或猜测
Schema，也不要再次加载同一 Skill。

## 自适应研究

围绕 overview、sentiment、daily_trend、topics、top_posts 判断当前覆盖，自主选择 MCP 工具、参数
和顺序；这不是固定工具清单。完整标准 MCP Tool Result 直接用于分析，不写中间 Evidence，不改写
原始平台、期间、单位或指标含义。volume、posts、engagement 不互相替代，不跨平台或跨期间混算。

用户没有要求环比/同比时保持 not_requested，不为填字段额外查询。下一次查询不能补足重要章节，
或结果为空、受限、超时、失败时，停止撞击同类查询，使用已返回数据发布 restricted 报告；不得把
缺失当零，也不得为了 complete 编造值。

## 一次性组装

按 model_input_schema 一次性构造 scope、data、narrative、availability、limitations 和
methodology_input。只提交这些模型业务字段；schema_version、module、data_status、
canonical_data、field_lineage 由服务器生成。完整数据的必需章节标 complete；任何缺失章节标
partial/unavailable，业务字段使用 Schema 允许的 null/空集合，并给出覆盖 limitation。

## 定向纠错与发布

首次 build_artifact_draft 失败时，只按返回的 RFC 6901 path/type/reason 修复对应字段，最多重试
一次；不要重写整份 payload，也不要因为 Schema 错误重新查询 MCP。Draft 成功后立即调用
publish_artifacts。没有 Publication/Version 就不算完成。
```

**Step 4: 写 Root/Social 最小候选**

Root Policy 只改现行“只有用户要求正式报告”段，统一为：

```markdown
先区分专项报告执行、既有 Artifact 钻取、范围澄清和策略咨询。用户目标已进入品牌、活动或达人
报告专项且关键范围明确时，视为正式专项执行，必须按该专项 Skill 创建并发布对应 Artifact；策略
咨询和既有 Artifact 钻取不因此强制新建报告。该要求是模型行为契约，不是 Runtime required-
artifact 门禁。
```

Social Skill 对应改为：

```markdown
关键范围不明确时先澄清并停止；范围明确并进入报告专项 Skill 后，本次执行必须发布该专项
Artifact。策略咨询或既有 Artifact 钻取不强制新建报告。
```

其他安全、Direct MCP、server-owned fields 规则原样保留。

**Step 5: 写两个维护 reference**

`metric-semantics.md` 固定记录：volume/posts/engagement 区分、平台/期间/单位边界、comparison
四态、空值规则和五个核心章节。`restricted-report.md` 固定记录：部分缺失、单工具失败、全空、
可选章节缺失四种结构化处理。文件顶部必须写：

```markdown
> 维护与评测参考：production loader 不会自动加载本文件；运行时必需规则必须保留在 SKILL.md。
```

reference 不复制完整 raw Tool Result 或完整 JSON Schema。

**Step 6: 绿灯与提交**

```bash
cd backend
.venv/bin/pytest -q \
  tests/marketing_skill_eval/test_brand_candidate.py \
  tests/marketing_skill_eval/test_variants.py
.venv/bin/ruff check tests/marketing_skill_eval/test_brand_candidate.py
git diff --check
```

Commit:

```bash
git add backend/tests/marketing_skill_eval/fixtures/brand/scenarios.json \
  backend/tests/marketing_skill_eval/fixtures/variants/brand-candidate-1 \
  backend/tests/marketing_skill_eval/test_brand_candidate.py
git commit -m "test(marketing-skills): define brand stability candidate"
```

---

## Task 8：授权后采集品牌 raw corpus 并锁定 hash

**Files:**

- Create: `backend/scripts/capture_marketing_skill_corpus.py`
- Create: `backend/tests/marketing_skill_eval/fixtures/brand/corpus.lock.json`
- Runtime-only, ignored: `backend/.data/marketing-skill-evals/brand-v1/**`
- Modify: execution-day changelog

**Step 1: 先实现 capture CLI 的离线 guard 测试**

在 `test_runner.py` 增加：缺 `RUN_MARKETING_SKILL_CORPUS_CAPTURE=1`、预算参数缺失、真实 DB、
输出目录非空、token 缺失、origin 非 HTTPS、工作树脏时均在任何网络前退出。用 mocked forwarder
断言 0 network。

```bash
cd backend
.venv/bin/pytest -q tests/marketing_skill_eval/test_runner.py -k capture
```

Expected: 先红后绿。

**Step 2: 到达真实调用停止点并请求授权**

执行会话必须停止，向用户报告当前 branch/HEAD、工作树、十个场景、真实模型名、DataTap origin
host（不含 path/query/token）和以下硬上限：

- 最长 3 小时；
- 最多 10 个业务 Run；
- 最多 600 次真实模型请求；
- 最多 300 次真实 DataTap dispatch；
- DataTap 只读 allowlist；
- 任一密钥形态落盘、非测试库、越 allowlist、预算达到上限立即停止；
- raw Tool Result 原样写入 Git-ignored corpus，明确不脱敏；
- 不创建生产 Artifact，不触碰开发库/生产库/历史 UAT retained rows。

没有用户新消息明确同意这些上限，不得继续。

**Step 3: 授权后运行 capture**

```bash
cd backend
RUN_MARKETING_SKILL_CORPUS_CAPTURE=1 \
MARKETING_SKILL_CAPTURE_MAX_MODEL_REQUESTS=600 \
MARKETING_SKILL_CAPTURE_MAX_DATATAP_DISPATCHES=300 \
MARKETING_SKILL_CAPTURE_MAX_WALL_SECONDS=10800 \
.venv/bin/python scripts/capture_marketing_skill_corpus.py \
  --matrix tests/marketing_skill_eval/fixtures/brand/scenarios.json \
  --baseline tests/marketing_skill_eval/fixtures/baseline/marketing-v2-1.1.0-capability.json \
  --corpus-root .data/marketing-skill-evals/brand-v1
```

Expected: `CAPTURE_COMPLETE corpus_version=brand-v1`。不得打印 prompt、raw result、token 或 DSN。

**Step 4: corpus coverage gate**

```bash
cd backend
.venv/bin/python scripts/lock_marketing_skill_corpus.py \
  --corpus-root .data/marketing-skill-evals/brand-v1 \
  --output tests/marketing_skill_eval/fixtures/brand/corpus.lock.json
.venv/bin/python scripts/run_marketing_skill_eval.py verify-corpus \
  --matrix tests/marketing_skill_eval/fixtures/brand/scenarios.json \
  --corpus-root .data/marketing-skill-evals/brand-v1 \
  --lock tests/marketing_skill_eval/fixtures/brand/corpus.lock.json
```

Coverage 必须确认：九个正式场景都有可回放路径；single/multi/comparison/high-volume 的关键 oracle
齐全；partial/empty/error/sparse 各自存在真实捕获的对应 result/status；ambiguous capture 为 0
业务 dispatch。若任一不满足，输出 `BRAND_CORPUS_INCOMPLETE` 并停止。不得用合成业务 payload
填充缺口；补采需新授权。

**Step 5: 只提交 lock，不提交 raw**

```bash
git status --short
# Expected: corpus.lock.json + source/test/changelog only; backend/.data absent

git add backend/scripts/capture_marketing_skill_corpus.py \
  backend/tests/marketing_skill_eval/fixtures/brand/corpus.lock.json \
  backend/tests/marketing_skill_eval/test_runner.py changelog/2026-08-14.md
git commit -m "test(marketing-skills): lock brand evaluation corpus"
git show --check --stat HEAD
```

---

## Task 9：授权后运行 30×2 真实模型 A/B

**Files:**

- Runtime-only, ignored: `backend/.data/marketing-skill-evals/results/<round-id>/**`
- Create after PASS: `docs/qa/2026-08-14-brand-skill-evaluation.md`
- Modify: execution-day changelog

**Step 1: 评测前静态门禁**

```bash
git status --porcelain
# Expected: no output

cd backend
.venv/bin/pytest -q tests/marketing_skill_eval tests/marketing_capability_pack
.venv/bin/python scripts/run_marketing_skill_eval.py verify-corpus \
  --matrix tests/marketing_skill_eval/fixtures/brand/scenarios.json \
  --corpus-root .data/marketing-skill-evals/brand-v1 \
  --lock tests/marketing_skill_eval/fixtures/brand/corpus.lock.json
```

任何失败都输出 `BRAND_SKILL_EVAL_BLOCKED`，不调用模型。

**Step 2: 请求独立真实模型授权**

此阶段 DataTap 外部 dispatch 上限为 0，所有 MCP 结果来自 loopback replay。执行会话必须把当前
HEAD、真实模型名、corpus lock hash 和以下上限发给用户：

- baseline 30 个有效轮次 + candidate 30 个有效轮次；
- 基础设施无效轮次每个最多补跑一次，全 round 最多 62 个 Run；
- 每 Run `max_decisions=60`；
- 总真实模型请求硬上限 3720；
- 本地 replay dispatch 每 Run 最多 50、全 round 最多 3000；
- input token 20,000,000、output token 4,000,000；
- 最长 6 小时；
- 外部 DataTap dispatch 必须为 0；本地 replay 的测试积分不是真实钱包消费；
- 达到任一上限立即停止，不用新 Run 隐藏 Skill 失败。

没有用户新消息明确授权，不得运行。

**Step 3: 运行 interleaved compare**

```bash
cd backend
bash scripts/run_marketing_skill_eval.sh compare \
  --matrix tests/marketing_skill_eval/fixtures/brand/scenarios.json \
  --corpus-root .data/marketing-skill-evals/brand-v1 \
  --lock tests/marketing_skill_eval/fixtures/brand/corpus.lock.json \
  --baseline tests/marketing_skill_eval/fixtures/baseline/marketing-v2-1.1.0-capability.json \
  --candidate tests/marketing_skill_eval/fixtures/variants/brand-candidate-1/variant.json \
  --repetitions 3 \
  --max-runs 62 \
  --max-model-requests 3720 \
  --max-replay-dispatches 3000 \
  --max-input-tokens 20000000 \
  --max-output-tokens 4000000 \
  --max-wall-seconds 21600
```

Expected terminal 之一：

- `BRAND_SKILL_GATE_PASS`；
- `BRAND_SKILL_GATE_FAIL:<reason>`；
- `BRAND_SKILL_EVAL_BLOCKED:<reason>`；
- `CORPUS_MISS:<call_key>`。

**Step 4: 处理 CORPUS_MISS 的唯一合法路径**

发现 miss 时本轮无效，停止。新授权下用 recording mode 只补该 call key，重算 lock，然后 baseline
和 candidate 对所有受影响 scenario 从 repetition 1 重新跑；不得只给 candidate 补数据或保留旧
分数。

**Step 5: Gate PASS 后写 QA 摘要并提交**

QA 文档必须列出：branch/HEAD、model identity、baseline/candidate snapshot digest、corpus lock
hash、30×2 有效轮次、无效/补跑轮次、十类通过数、首次 Draft、Publication、五项效率配对总和、
safety violations、最终 Gate。不得复制 raw Tool Result 或模型隐藏 reasoning。

```bash
git add docs/qa/2026-08-14-brand-skill-evaluation.md changelog/2026-08-14.md
git commit -m "docs(marketing-skills): record brand ab evaluation"
git show --check --stat HEAD
```

若 Gate FAIL，不创建生产 1.2.0 修改；记录失败事实后以 `BRAND_SKILL_GATE_FAIL` 停止。

---

## Task 10：Gate PASS 后把候选精确晋级为 marketing-v2 / 1.2.0

**Files:**

- Modify: `backend/app/marketing_capability_pack/packs/marketing-v2/policies/root-policy.md`
- Modify: `backend/app/marketing_capability_pack/packs/marketing-v2/skills/social-marketing-analyst/SKILL.md`
- Modify: `backend/app/marketing_capability_pack/packs/marketing-v2/skills/brand-research-report/SKILL.md`
- Create: `backend/app/marketing_capability_pack/packs/marketing-v2/skills/brand-research-report/references/metric-semantics.md`
- Create: `backend/app/marketing_capability_pack/packs/marketing-v2/skills/brand-research-report/references/restricted-report.md`
- Modify: `backend/app/marketing_capability_pack/packs/marketing-v2/manifest.json`
- Modify: `backend/tests/marketing_capability_pack/test_loader.py`
- Modify: `backend/tests/pi_runtime_poc/test_marketing_capability_runtime.py`
- Create: `backend/scripts/update_marketing_pack_manifest.py`
- Create: `backend/tests/marketing_capability_pack/test_manifest_release.py`

**Step 1: 先写 1.2.0 红灯**

新增断言：

```python
expected_versions = {
    "social-marketing-analyst": "1.2.0",
    "brand-research-report": "1.2.0",
    "campaign-evaluation-report": "1.1.0",
    "kol-selection-report": "1.1.0",
    "artifact-drilldown": "1.1.0",
    "marketing-strategy": "1.1.0",
}

assert snapshot.pack_version == "1.2.0"
assert {skill.name: skill.version for skill in snapshot.skills} == expected_versions
assert snapshot.builder_versions == {
    "brand_report_v3": "1.1.0",
    "campaign_report_v3": "1.1.0",
    "kol_selection_v3": "1.1.0",
}
assert snapshot.exporter_versions == snapshot.builder_versions
```

另断言生产三个文本与已评测 candidate 文件逐字节一致；活动、达人和其他 Skill 与冻结 baseline
逐字节一致；旧 `marketing-v1` 整目录 hash 不变；Root/Social/Brand digest 正确；contract digest
不变。

```bash
cd backend
.venv/bin/pytest -q \
  tests/marketing_capability_pack/test_loader.py \
  tests/marketing_capability_pack/test_manifest_release.py \
  tests/pi_runtime_poc/test_marketing_capability_runtime.py
```

Expected: FAIL，production pack 尚为 1.1.0。

**Step 2: 精确复制获胜候选**

使用 `apply_patch` 修改三个生产文本，内容必须与 candidate fixture byte-equal；复制两个 reference。
不得顺便重写 campaign/KOL/其他 Skill。

**Step 3: 实现并运行 manifest updater**

updater 必须要求旧身份和目标身份，且只更新声明的版本与所有现有 content digest：

```bash
cd backend
.venv/bin/python scripts/update_marketing_pack_manifest.py \
  --pack marketing-v2 \
  --expected-pack-version 1.1.0 \
  --expected-manifest-digest 65d28bb1afdc20c51729f4222fbed929165d0412ad188f59d7d55d5c9e9931ce \
  --new-pack-version 1.2.0 \
  --skill-version social-marketing-analyst=1.2.0 \
  --skill-version brand-research-report=1.2.0
```

脚本不得改 builder/exporter version，不得增加 manifest 字段；结束时用 production loader 自验并只
打印新 manifest digest。

**Step 4: 跑绿灯**

```bash
cd backend
.venv/bin/pytest -q \
  tests/marketing_capability_pack/test_loader.py \
  tests/marketing_capability_pack/test_manifest_release.py \
  tests/pi_runtime_poc/test_marketing_capability_runtime.py \
  tests/agent_runtime/test_pi_internal_tools.py \
  tests/agent_artifacts/model_inputs/test_model_inputs.py
.venv/bin/ruff check app scripts/update_marketing_pack_manifest.py \
  tests/marketing_capability_pack tests/pi_runtime_poc/test_marketing_capability_runtime.py
git diff --check
```

Expected: PASS。`model_input_contract` 仍直接来自 DTO；Artifact contract/builder/exporter 无变化。

**Step 5: 提交生产晋级**

```bash
git add backend/app/marketing_capability_pack/packs/marketing-v2 \
  backend/tests/marketing_capability_pack \
  backend/tests/pi_runtime_poc/test_marketing_capability_runtime.py \
  backend/scripts/update_marketing_pack_manifest.py
git commit -m "feat(marketing-skills): promote brand reporting to 1.2.0"
git show --check --stat HEAD
```

---

## Task 11：离线全量回归与独立代码审查

**Files:**

- Modify only if tests expose a regression within this plan's files
- Modify: `docs/qa/2026-08-14-brand-skill-evaluation.md`
- Modify: execution-day changelog

**Step 1: 后端串行全量**

确认测试库已在 migration head；期间不运行任何其他 pytest：

```bash
cd backend
.venv/bin/ruff check app tests scripts/marketing_skill_eval \
  scripts/freeze_marketing_skill_baseline.py \
  scripts/lock_marketing_skill_corpus.py \
  scripts/capture_marketing_skill_corpus.py \
  scripts/run_marketing_skill_eval.py \
  scripts/update_marketing_pack_manifest.py
.venv/bin/pytest -q
```

Expected: 全绿；真实服务 marker 保持 skipped。若现有 flake 出现，保存日志、单跑复现、确认是否与
diff 相关；不得放宽断言或增加盲目 sleep。

**Step 2: Pi Gateway / Runtime / 前端**

```bash
cd pi-gateway
npm run test
npm run typecheck
npm run build

cd ../pi-runtime
npm run test
npm run typecheck

cd ..
npm run test
npm run lint
npm run build
```

Expected: 全绿。即使本轮没有 production TypeScript 改动，也验证 Capability Snapshot 交互未回归。

**Step 3: 离线生产拓扑**

```bash
cd backend
.venv/bin/pytest -q tests/integration/test_pi_gateway_offline_uat.py
```

Expected: 28/28；只用 fake model/MCP 的这一步是回归证明，不是 Skill 晋级评测。

**Step 4: Git/secret/process 检查**

```bash
git diff --check
git status --short
git log --oneline 03a3b4a4c064426a3c5e22bbc4985f7615921255..HEAD
```

确认：

- `backend/.data/marketing-skill-evals` 没有 tracked 文件；
- 不存在 `.env`、API key、Bearer header、私钥、带密 DSN；
- corpus 允许包含完整业务/个人数据，不得因为扫描工具误报而改写 raw 文件；只检查凭证形态；
- 无 pytest、uvicorn、Gateway、worker、Vite 残留；8000/5173/9471 等本轮端口关闭；
- 每个提交 `git show --check <sha>` 通过。

**Step 5: 独立审查**

请求独立 reviewer 对 `03a3b4a..HEAD` 审查，目标 Critical 0 / Important 0。专项确认：

1. compare 模式绝不连接 DataTap；
2. raw Tool Result 没有被脱敏、摘要、裁剪或业务归一化；
3. corpus/结果 hash chain 与路径 fail-closed；
4. baseline/candidate 只差允许的三个文本；
5. scorer Gate 实现与 §2.3 一致；
6. 没有 fake model 结果被用作晋级证据；
7. 没有恢复 Evidence Bridge、fixed tool order、required Runtime gate；
8. Pack 1.2.0 只升级 Social/Brand 文本，DTO/Schema/Builder/Exporter 和其他 Skill 不变；
9. tenant/session/run/Artifact 归属与测试库隔离不被放松。

Important 必须修复并重跑受影响测试；Minor 记录到 QA 文档，不顺手扩范围。

**Step 6: 提交审查记录**

```bash
git add docs/qa/2026-08-14-brand-skill-evaluation.md changelog/2026-08-14.md
git commit -m "docs(marketing-skills): record brand offline verification"
git show --check --stat HEAD
```

---

## Task 12：新授权下执行一次真实模型 + 真实 DataTap 品牌端到端

**Files:**

- Runtime-only evidence: `/private/tmp/BRAND_SKILL_1_2_E2E_<round-id>/`
- Create: `docs/qa/2026-08-14-brand-skill-1.2-real-e2e.md`
- Modify: `docs/runbooks/pi-agent-gateway.md`
- Modify: execution-day changelog

**Step 1: 到达外部验收停止点**

执行会话先报告：branch/HEAD、工作树、Pack 1.2.0 新 manifest digest、自动 Gate、独立审查、全量
测试结果；然后请求一次独立授权。授权硬上限固定：

- 专用数据库 `kol_insight_b7_uat`，不得访问 `kol_insight`/`kol_insight_test`；
- 新 tenant/user/session/Runtime Config/License/wallet，禁止复用历史失败 Run；
- 最长 2 小时；
- Web 业务提交恰 1 次、业务 Run 恰 1 个、Attempt 1；
- `max_decisions=40`，真实模型请求最多 40；
- discovery 最多 5，真实 DataTap 业务 dispatch 最多 30；
- 测试钱包总暴露最多 300 积分；
- 只读 approved DataTap 工具；不使用 fake model、fake MCP、预计算 payload、工具名提示或
  Artifact Schema 提示；
- 外发后不得 fresh Run 隐藏失败；任一越库、凭证落盘、预算超限立即停止。

无用户新授权不得启动服务或连接数据库/模型/DataTap。

**Step 2: 使用自然语言执行**

唯一业务请求：

```text
请生成蔚来在 2026-07-01 至 2026-07-31 的小红书和抖音品牌社媒分析报告，并分别说明平台表现；
如果部分数据不可用，也请发布一份准确说明限制的报告。
```

不得在 system/user message 追加工具名、`artifact_type`、JSON Schema、Builder 次数或预构造字段。

**Step 3: 功能验收**

PASS 必须同时满足：

- Pack=`marketing-v2/1.2.0` 且 manifest digest 与 HEAD 一致；
- 单业务 Run、Attempt 1、terminal completed 或 completed_with_warnings；
- Draft 调用 1 或 2 次；
- 当前 Run 发布 `brand_report_v3` Publication/Version；
- payload 严格合法，complete/restricted 与真实结果一致；
- BI 展示该 Version，Web 导出 Excel 绑定同一 Version，xlsx 可打开；
- message.completed 在唯一 terminal 前，事件 sequence 单调；
- 无 running ToolCall/Step/permit/Attempt；
- Evidence 增量 0，且无 `mcp_result_v1` 回灌；
- 模型直接接收完整 Tool Result，日志不记录完整 payload 或凭证。

若出现 `result_unknown` 但核心报告、Publication、Version、BI/Excel 全部成功，判
`BRAND_SKILL_1_2_FUNCTIONAL_PASS_WITH_ACCOUNTING_WARNINGS`，保留 reservation 由既有 admin
reconciliation 处理；不得自动重放、释放或把 warning 隐藏为 clean PASS。该 warning 继续阻塞完整
B7/生产切流，但不推翻本阶段 Skill 功能结论。

**Step 4: 封存与文档**

服务有界关闭、端口/进程清零、工作树保持干净。QA 文档记录计数、Artifact/Version/Excel hash、
Pack 身份、限制和 accounting warning；不提交 raw Tool Result、凭证或数据库 dump。

更新 runbook 的 Pack 版本事实，并在 changelog 记录真实验收；不得宣称完整 B7 或生产就绪。

```bash
git add docs/qa/2026-08-14-brand-skill-1.2-real-e2e.md \
  docs/runbooks/pi-agent-gateway.md changelog/2026-08-14.md
git commit -m "docs(marketing-skills): record brand 1.2 real e2e"
git show --check --stat HEAD
git status --porcelain
# Expected: no output
```

到此停止。输出
`BRAND_SKILL_1_2_READY_FOR_REVIEW` 或
`BRAND_SKILL_1_2_FUNCTIONAL_PASS_WITH_ACCOUNTING_WARNINGS`，等待架构审核；不得开始活动分析。

---

## 3. 最终验收清单

- [ ] 1.1.0 baseline Snapshot 已冻结并由已知 manifest digest 锁定。
- [ ] raw corpus 位于 Git-ignored 目录，完整 Tool Result 未脱敏、未摘要、未改写。
- [ ] replay compare 模式对外部 DataTap 的调用数严格为 0。
- [ ] 十类品牌场景 × baseline/candidate × 3 次均有有效 observation。
- [ ] candidate 通过率 ≥90%，safety=100%，无 category 三连败。
- [ ] 正式执行 27/27 均产生当前 Run 的 Publication/Version；澄清 3/3 均 0 MCP/0 Artifact。
- [ ] 每轮 Draft ≤2，首次 Draft 合法率严格高于 baseline。
- [ ] 五项效率指标均未恶化超过 20%。
- [ ] production Pack 只升级到 1.2.0；Social/Brand=1.2.0，其余 Skill 和 Builder/Exporter=1.1.0。
- [ ] Root/Social/Brand 行为语义一致；无 fixed tool order、Evidence Bridge 或 Runtime required gate。
- [ ] 后端、Pi Gateway、Pi Runtime、前端、完整离线 UAT 和 Git checks 全绿。
- [ ] 独立审查 Critical 0 / Important 0。
- [ ] 单次真实模型 + 真实 DataTap 端到端产生品牌 Version，BI/Excel 同版。
- [ ] QA、runbook、changelog 已记录实际结果和边界。
- [ ] 未进入活动、达人、完整 B7、生产切流或方案 C。

## 4. 后续计划顺序

品牌架构审核通过后，按以下顺序另建计划：

1. `campaign-evaluation-report`：复用同一 corpus/runner/scorer 基础设施，新增活动期/基线期/
   观察期、归因和 ROI 受限语义，成功后 Pack 1.3.0；
2. `kol-selection-report`：新增候选身份、预算、受众、评分 snapshot 评测；若低于 90%，以
   `KOL_SCORING_CONTRACT_BLOCKED` 停止并另写确定性评分接入设计；通过后 Pack 1.4.0。

两者都必须沿用“每个 Skill 晋级后停审”的边界，不能一次性越过三个 Gate。
