#!/usr/bin/env bash
# 真实模型 + 真实 DataTap MCP UAT 运行器（Task 26）。
#
# 用法：cd backend && bash scripts/run_real_agent_uat.sh
#
# 安全约束（HARD）：
#   1. 加载 backend/.env（真实 TencentPlan 密钥）+ 根 .env（真实 DATATAP_MCP_TOKEN），
#      但随后 FORCE-override APP_ENV=test / MYSQL_*=kol_insight_test 等——测试只打
#      test DB，绝不触碰 dev DB。
#   2. 日志与测试输出必须脱敏：不得打印 token、DSN、或完整原始 prompt/payload。
#   3. 依赖真实模型与真实 DataTap，会消耗真实积分；每次 MCP 调用 10 分。
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT_DIR="$(cd "${BACKEND_DIR}/.." && pwd)"

if [[ ! -d "${BACKEND_DIR}/.venv" ]]; then
  echo "[UAT] .venv 不存在: ${BACKEND_DIR}/.venv" >&2
  exit 2
fi

# 1) 加载真实密钥（不覆盖已有环境变量）。
#    backend/.env 提供 TENCENT_PLAN_API_KEY/BASE_URL/MODEL；
#    根 .env 提供 DATATAP_MCP_TOKEN。
for env_file in "${BACKEND_DIR}/.env" "${ROOT_DIR}/.env"; do
  if [[ -f "${env_file}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${env_file}"
    set +a
  fi
done

# 2) FORCE-override 测试隔离变量：任何时候都必须指向 test DB + mock auth。
export APP_ENV=test
export AUTH_MODE=mock
export MYSQL_HOST=127.0.0.1
export MYSQL_PORT=3306
export MYSQL_DATABASE=kol_insight_test
export MYSQL_USER=kol_test
export MYSQL_PASSWORD=test-only-password
export RUN_REAL_SERVICES=1

# 3) 运行真实服务 UAT。仅输出安全的场景摘要与 run_id，不回显密钥。
echo "[UAT] env: APP_ENV=${APP_ENV} MYSQL_DATABASE=${MYSQL_DATABASE} AUTH_MODE=${AUTH_MODE} RUN_REAL_SERVICES=${RUN_REAL_SERVICES}"
echo "[UAT] 真实模型 + 真实 DataTap MCP，连接 test DB；每次 MCP 调用消耗 10 积分。"
cd "${BACKEND_DIR}"
"${BACKEND_DIR}/.venv/bin/python" -m pytest \
  tests/integration/test_agent_runtime_real.py \
  -m real_services \
  -q \
  -p no:cacheprovider
