"""导出 mcp_tool_catalog 为 Markdown 工具说明文档（一次性脚本）。"""

import asyncio
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings


async def main() -> None:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT service_slug, internal_tool_name, reviewed_description, "
                    "input_schema_json, review_status, is_enabled "
                    "FROM mcp_tool_catalog ORDER BY service_slug, internal_tool_name"
                )
            )
        ).all()
    await engine.dispose()

    fence = chr(96) * 3
    lines = [
        "# DataTap MCP 工具说明（catalog 快照）",
        "",
        "来源：开发库 `mcp_tool_catalog`——运行时生效的审核版 schema。"
        "input_schema 为该工具入参的 JSON Schema（KOL 搜索的入参需包在 request 对象内）。",
        "",
    ]
    current_service = None
    for slug, name, desc, schema, review, enabled in rows:
        if slug != current_service:
            current_service = slug
            lines += [f"## {slug}", ""]
        schema_obj = schema if isinstance(schema, dict) else json.loads(schema)
        lines += [
            f"### {name}",
            "",
            f"- 状态：{review} / enabled={bool(enabled)}",
            f"- 说明：{desc}",
            "",
            fence + "json",
            json.dumps(schema_obj, ensure_ascii=False, indent=2),
            fence,
            "",
        ]
    with open("../docs/datatap-mcp-tools.md", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("written, tools:", len(rows))


asyncio.run(main())
