#!/usr/bin/env bash
# 方案 A 唯一真实入口：加载主分支真实模型/DataTap 配置，但只写 POC 数据库。
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT_DIR="$(cd "${BACKEND_DIR}/.." && pwd)"
# 默认取 Git 主工作树，真实配置只能从该处的未跟踪 .env 读取；不会复制到 POC
# 工作树或写入输出。也允许调用者显式指定主工作树路径。
MAIN_ROOT="${PI_RUNTIME_POC_MAIN_ROOT:-$(git -C "${ROOT_DIR}" worktree list --porcelain | sed -n '1s/^worktree //p')}"
[[ -n "${MAIN_ROOT}" && -d "${MAIN_ROOT}" ]] || exit 2
# 接入链接解析出的 token/endpoint mapping 只能在本次进程内存在；主工作树的 .env
# 仅作为常规运行时配置回退，不能覆盖调用方刚解析出的 DataTap 连接凭证。
CONNECT_DATATAP_TOKEN="${DATATAP_MCP_TOKEN:-}"
CONNECT_DATATAP_ENDPOINTS_JSON="${DATATAP_MCP_ENDPOINTS_JSON:-}"

for env_file in "${MAIN_ROOT}/.env" "${MAIN_ROOT}/backend/.env"; do
  if [[ -f "${env_file}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${env_file}"
    set +a
  fi
done

if [[ -n "${CONNECT_DATATAP_TOKEN}" ]]; then
  export DATATAP_MCP_TOKEN="${CONNECT_DATATAP_TOKEN}"
fi
if [[ -n "${CONNECT_DATATAP_ENDPOINTS_JSON}" ]]; then
  export DATATAP_MCP_ENDPOINTS_JSON="${CONNECT_DATATAP_ENDPOINTS_JSON}"
fi

export APP_ENV=test
export AUTH_MODE=mock
export MYSQL_DATABASE=kol_insight_pi_poc
export PI_RUNTIME_POC_ENABLED=true
export PI_RUNTIME_POC_INTERNAL_SECRET="${PI_RUNTIME_POC_INTERNAL_SECRET:-$(openssl rand -hex 32)}"
export RUN_REAL_SERVICES=1

[[ "${APP_ENV}" == "test" ]] || exit 2
[[ "${MYSQL_DATABASE}" == "kol_insight_pi_poc" ]] || exit 2
[[ "${PI_RUNTIME_POC_ENABLED}" == "true" ]] || exit 2
[[ -n "${DATATAP_MCP_ENDPOINTS_JSON:-}" ]] || exit 2
[[ -n "${TENCENT_PLAN_API_KEY:-}" ]] || exit 2

cd "${BACKEND_DIR}"
"${BACKEND_DIR}/.venv/bin/alembic" upgrade head

# Pi 的受控 Evidence/内部工具回调必须落到同一 POC 服务。已有 8000 服务来源不明，
# 为防止误连主库/非 POC 配置直接拒绝，不复用。
if curl -fsS --max-time 1 http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
  exit 2
fi
# 只能启动 Pi POC 内部回调服务，禁止启动主应用的后台 Current Runtime 领取循环。
"${BACKEND_DIR}/.venv/bin/uvicorn" app.pi_runtime_poc.server:app --host 127.0.0.1 --port 8000 \
  --timeout-graceful-shutdown 5 >/dev/null 2>&1 &
SERVER_PID=$!
cleanup() { kill "${SERVER_PID}" >/dev/null 2>&1 || true; }
trap cleanup EXIT
for _ in $(seq 1 30); do
  if curl -fsS --max-time 1 http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS --max-time 1 http://127.0.0.1:8000/healthz >/dev/null
"${BACKEND_DIR}/.venv/bin/python" scripts/run_pi_runtime_poc.py "$@"
