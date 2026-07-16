# 真实运行时单一路径 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留模拟登录，但让模型与 MCP 业务执行只走腾讯 Token Plan、DeepSeek-V4-Pro 与 DataTap。

**Architecture:** 配置层固定真实服务、强制两个真实密钥；依赖注入层无条件构造 `TencentPlanAdapter` 和 `DataTapTransport`。删除 Fake Model、Fake MCP 与使用它们的测试替身，改用小范围真实连通性测试。

**Tech Stack:** Python 3.11、FastAPI、Pydantic Settings、OpenAI Python SDK、DataTap MCP、pytest、MySQL。

## Global Constraints

- 仅 `AUTH_MODE=mock` 可保留，供开发模拟短信与微信登录。
- 模型固定 `https://tokenhub.tencentmaas.com/plan/v3` 与 `deepseek-v4-pro`。
- MCP 固定为 DataTap；成功调用仍按 10 积分结算。
- 密钥仅在被 Git 忽略的 `.env` 中；不得写入源码、测试、日志或提交。
- 不保留 Fake Model、Fake MCP、`MODEL_PROVIDER` 或 `MCP_PROVIDER` 的运行时开关。

---

### Task 1: 固化真实服务配置契约

**Files:**
- Modify: `backend/app/core/config.py:17-87`
- Modify: `.env.example:18-27`
- Modify: `backend/tests/core/test_phase2_config.py`

**Interfaces:**
- Consumes: `TENCENT_PLAN_API_KEY`、`DATATAP_MCP_TOKEN`。
- Produces: 必填的 `Settings.tencent_plan_api_key: SecretStr` 与 `Settings.datatap_mcp_token: SecretStr`。

- [ ] **Step 1: 写入失败配置测试**

```python
def settings(**changes: object) -> Settings:
    values = {
        "mysql_password": SecretStr("test-only-password"),
        "jwt_secret": SecretStr("test-only-jwt-secret-at-least-32-characters"),
        "tencent_plan_api_key": SecretStr("unit-test-model-key"),
        "datatap_mcp_token": SecretStr("unit-test-mcp-token"),
    }
    values.update(changes)
    return Settings(_env_file=None, **values)

@pytest.mark.parametrize("field", ["tencent_plan_api_key", "datatap_mcp_token"])
def test_real_runtime_rejects_blank_credential(field: str) -> None:
    with pytest.raises(ValidationError):
        settings(**{field: SecretStr("")})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && .venv/bin/pytest tests/core/test_phase2_config.py -q`

Expected: FAIL，因为当前配置允许模拟 Provider 与空真实密钥。

- [ ] **Step 3: 移除配置中的模拟 Provider**

在 `Settings` 中删除 `model_provider`、`mcp_provider`，固定：

```python
tencent_plan_base_url: AnyHttpUrl = AnyHttpUrl("https://tokenhub.tencentmaas.com/plan/v3")
tencent_plan_api_key: SecretStr
tencent_plan_model: Literal["deepseek-v4-pro"] = "deepseek-v4-pro"
datatap_mcp_token: SecretStr
```

在 `validate_runtime_contracts` 无条件拒绝空白模型密钥与 DataTap 令牌；保留生产环境拒绝 `AUTH_MODE=mock` 的现有规则。

- [ ] **Step 4: 更新配置样例**

从 `.env.example` 删除：

```dotenv
MODEL_PROVIDER=fake
MCP_PROVIDER=fake
```

保留无真实值的 `TENCENT_PLAN_API_KEY=` 与 `DATATAP_MCP_TOKEN=`。

- [ ] **Step 5: 运行测试确认通过并提交**

Run: `cd backend && .venv/bin/pytest tests/core/test_phase2_config.py -q`

Expected: PASS。

```bash
git add backend/app/core/config.py .env.example backend/tests/core/test_phase2_config.py
git commit -m "feat: require real model and MCP configuration"
```

### Task 2: 删除 Fake 业务执行器

**Files:**
- Delete: `backend/app/model/fake.py`
- Delete: `backend/app/mcp_gateway/fake.py`
- Modify: `backend/app/model/dependencies.py:1-15`
- Modify: `backend/app/tasks/dependencies.py:13-18,308-316`
- Modify: `backend/tests/model/test_dependencies.py`
- Create: `backend/tests/mcp_gateway/test_dependencies.py`

**Interfaces:**
- Consumes: Task 1 的必填真实配置。
- Produces: `get_model_adapter() -> TencentPlanAdapter` 和 `get_mcp_transport() -> DataTapTransport`。

- [ ] **Step 1: 写入真实依赖注入测试**

```python
def test_process_dependency_always_builds_tencent_adapter(monkeypatch) -> None:
    dependencies.get_model_adapter.cache_clear()
    monkeypatch.setattr(dependencies, "get_settings", lambda: settings())
    assert isinstance(dependencies.get_model_adapter(), TencentPlanAdapter)
    dependencies.get_model_adapter.cache_clear()
```

```python
def test_process_dependency_always_builds_datatap_transport(monkeypatch) -> None:
    get_mcp_transport.cache_clear()
    monkeypatch.setattr("app.tasks.dependencies.get_settings", lambda: settings())
    assert isinstance(get_mcp_transport(), DataTapTransport)
    get_mcp_transport.cache_clear()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && .venv/bin/pytest tests/model/test_dependencies.py tests/mcp_gateway/test_dependencies.py -q`

Expected: FAIL，因为当前注入层仍包含 Fake 分支。

- [ ] **Step 3: 只构造真实适配器**

替换模型依赖实现：

```python
@lru_cache
def get_model_adapter() -> ModelAdapter:
    return TencentPlanAdapter.from_settings(get_settings())
```

替换 MCP 依赖实现：

```python
@lru_cache
def get_mcp_transport() -> DataTapTransport:
    return DataTapTransport(token=get_settings().datatap_mcp_token)
```

删除两个 Fake 文件及其导入。

- [ ] **Step 4: 运行测试确认通过并提交**

Run: `cd backend && .venv/bin/pytest tests/model/test_dependencies.py tests/mcp_gateway/test_dependencies.py -q`

Expected: PASS。

```bash
git add backend/app/model/dependencies.py backend/app/tasks/dependencies.py backend/tests/model/test_dependencies.py backend/tests/mcp_gateway/test_dependencies.py
git rm backend/app/model/fake.py backend/app/mcp_gateway/fake.py
git commit -m "refactor: remove fake model and MCP runtime"
```

### Task 3: 清除测试替身，新增真实服务冒烟覆盖

**Files:**
- Delete: `backend/tests/model/fakes.py`
- Delete: `backend/tests/model/test_fake_adapter.py`
- Delete: `backend/tests/mcp_gateway/fakes.py`
- Delete: `backend/tests/tasks/fakes.py`
- Delete: `backend/tests/reporting/fakes.py`
- Delete: 所有导入上述文件、`FakeModelAdapter` 或 `FakeMcpTransport` 的测试文件。
- Create: `backend/tests/integration/test_real_providers.py`

**Interfaces:**
- Consumes: 当前 `.env` 中的真实腾讯与 DataTap 凭据。
- Produces: 不记录原始响应的真实模型/MCP 连通性验证。

- [ ] **Step 1: 创建真实 DataTap 冒烟测试**

```python
import pytest

from app.core.config import get_settings
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.datatap import DataTapTransport
from app.model.tencent_plan import TencentPlanAdapter

@pytest.mark.asyncio
async def test_real_datatap_lists_tools() -> None:
    transport = DataTapTransport(token=get_settings().datatap_mcp_token)
    try:
        tools = await transport.list_tools(DataTapService.SOCIAL_GROW)
    finally:
        await transport.aclose()
    assert any(tool.name == "kol_xiaohongshu_search" for tool in tools)

def test_real_tencent_adapter_uses_confirmed_model() -> None:
    assert TencentPlanAdapter.from_settings(get_settings()).model == "deepseek-v4-pro"
```

- [ ] **Step 2: 删除全部 Fake 测试模块与引用**

Run: `cd backend && rg -l 'FakeModelAdapter|FakeMcpTransport|from .*fakes|import .*fakes' tests -g '*.py' | xargs git rm`

同时删除本任务 Files 列出的 5 个测试替身模块。

- [ ] **Step 3: 验证无 Fake 引用并运行真实冒烟**

Run: `cd backend && ! rg -n 'FakeModelAdapter|FakeMcpTransport|from .*fakes|import .*fakes' app tests -g '*.py'`

Expected: exit code 0 且无输出。

Run: `cd backend && .venv/bin/pytest tests/integration/test_real_providers.py -q`

Expected: PASS；不打印达人原始数据、令牌或接口地址。

- [ ] **Step 4: 提交测试替身清理**

```bash
git add backend/tests/integration/test_real_providers.py
git add -u backend/tests
git commit -m "test: remove fake provider fixtures"
```

### Task 4: 更新文档并验收真实链路

**Files:**
- Modify: `README.md:1-110`
- Modify: `docs/runbooks/phase-2-runtime.md`

**Interfaces:**
- Consumes: 启动的 MySQL、真实腾讯模型、真实 DataTap 与模拟登录。
- Produces: 无 Fake Provider 的可重复本地运行说明。

- [ ] **Step 1: 更新运行模式说明**

将 README 的 “fake/production 可切换” 替换为 “模型与 MCP 仅使用真实服务；仅登录保留开发模拟模式”。删除 `MODEL_PROVIDER` 与 `MCP_PROVIDER` 文档，明确两个真实密钥是启动前置条件但不写入其值。

- [ ] **Step 2: 增加真实验证命令**

在运行手册加入：

```bash
cd backend
.venv/bin/python -c 'from app.core.config import get_settings; s=get_settings(); print({"model":"deepseek-v4-pro","datatap_configured":bool(s.datatap_mcp_token.get_secret_value().strip())})'
.venv/bin/pytest tests/integration/test_real_providers.py -q
```

说明创建会话后必须出现 `plan.ready`、真实 MCP 调用及 BI 报表；故障日志只记录安全结构摘要。

- [ ] **Step 3: 静态检查、收集与启动验证**

Run: `cd backend && .venv/bin/ruff check app tests && .venv/bin/pytest --collect-only -q`

Expected: 两个命令 exit code 0。

Run: `cd backend && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000`

Expected: 使用模拟登录创建会话时，任务走真实腾讯模型与 DataTap MCP。

- [ ] **Step 4: 提交文档与验收**

```bash
git add README.md docs/runbooks/phase-2-runtime.md
git commit -m "docs: document real provider runtime"
```
