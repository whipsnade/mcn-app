#!/usr/bin/env bash
# Pi POC 的 DataTap 配置桥接。调用方提供的 JSON 只在当前进程内存中存在。

pi_poc_normalize_datatap_mapping() {
  local external_mapping="${DATATAP_MCP_ENDPOINTS_JSON:-}"

  if [[ -n "${external_mapping}" ]]; then
    # Pydantic 的 datatap_mcp_urls 字段读取 DATATAP_MCP_URLS；Pi Extension 则读取原始名称。
    export DATATAP_MCP_URLS="${external_mapping}"
    export DATATAP_MCP_ENDPOINTS_JSON="${external_mapping}"
  elif [[ -n "${DATATAP_MCP_URLS:-}" ]]; then
    # 允许主工作树的显式 Settings 配置进入同一受控链路，Pi 的最终值仍由 Python 重建。
    export DATATAP_MCP_ENDPOINTS_JSON="${DATATAP_MCP_URLS}"
  fi

  [[ -n "${DATATAP_MCP_URLS:-}" ]] || return 2
  [[ -n "${DATATAP_MCP_ENDPOINTS_JSON:-}" ]] || return 2
}
