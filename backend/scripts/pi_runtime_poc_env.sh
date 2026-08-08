#!/usr/bin/env bash
# Pi POC 的 DataTap 配置桥接。调用方提供的 JSON 只在当前进程内存中存在。

pi_poc_normalize_datatap_mapping() {
  local external_mapping="${DATATAP_MCP_ENDPOINTS_JSON:-}"

  if [[ -n "${external_mapping}" ]]; then
    # Pydantic 的 datatap_mcp_urls 字段读取 DATATAP_MCP_URLS。
    export DATATAP_MCP_URLS="${external_mapping}"
  elif [[ -n "${DATATAP_MCP_URLS:-}" ]]; then
    external_mapping="${DATATAP_MCP_URLS}"
  fi

  [[ -n "${DATATAP_MCP_URLS:-}" ]] || return 2
  # pi-mcp-adapter 只读取项目 .mcp.json 中的四个明确环境变量；不再把 JSON mapping
  # 传入 Pi 子进程，避免项目扩展重新承担 MCP 连接/发现责任。
  export DATATAP_INSIGHT_CUBE_MCP_URL
  export DATATAP_SOCIAL_GROW_MCP_URL
  export DATATAP_SOCIAL_GROW_CONTENT_MCP_URL
  export DATATAP_AKTOOLS_MCP_URL
  DATATAP_INSIGHT_CUBE_MCP_URL="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["insight-cube-mcp"])' "${external_mapping}")" || return 2
  DATATAP_SOCIAL_GROW_MCP_URL="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["social-grow-mcp"])' "${external_mapping}")" || return 2
  DATATAP_SOCIAL_GROW_CONTENT_MCP_URL="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["social-grow-content-mcp"])' "${external_mapping}")" || return 2
  DATATAP_AKTOOLS_MCP_URL="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["bilibili-mcp"])' "${external_mapping}")" || return 2
  unset DATATAP_MCP_ENDPOINTS_JSON
}
