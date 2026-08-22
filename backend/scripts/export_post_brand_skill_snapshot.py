"""导出成功 Run 的 Skill Snapshot 固化 fixture（post-brand Task 1 Step 4）。

两个子命令：
- candidates：列出 run_prefix 唯一命中 Run 的 manifest 每个 entry 的精确匹配
  候选（revision_id/scope_key，无正文），供实施者编写显式 source map；
- export：按显式 source map 逐 entry 校验并导出无秘密 fixture JSON。

只把 DTO JSON 写到显式 --output；不打印正文、不读环境变量进输出。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.db.session import SessionFactory
from app.marketing_skills.promotion import (
    SkillRevisionSource,
    list_post_brand_skill_source_candidates,
    load_post_brand_skill_snapshot,
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    candidates = sub.add_parser("candidates", help="列出每个 manifest entry 的候选（无正文）")
    candidates.add_argument("--run-prefix", required=True)
    candidates.add_argument("--output", required=True)

    export = sub.add_parser("export", help="按显式 source map 导出固化 fixture")
    export.add_argument("--run-prefix", required=True)
    export.add_argument("--source-map", required=True)
    export.add_argument("--output", required=True)
    return parser.parse_args(argv)


async def _run_candidates(args: argparse.Namespace) -> int:
    async with SessionFactory() as db:
        result = await list_post_brand_skill_source_candidates(db, run_prefix=args.run_prefix)
    payload = result.model_dump(mode="json")
    Path(args.output).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"run_id={result.run_id} entries={len(result.entries)} "
        f"multi_candidate={[item.name for item in result.entries if len(item.candidates) > 1]}"
    )
    return 0


async def _run_export(args: argparse.Namespace) -> int:
    raw_map = json.loads(Path(args.source_map).read_text(encoding="utf-8"))
    source_map = {
        name: SkillRevisionSource.model_validate(entry) for name, entry in raw_map.items()
    }
    async with SessionFactory() as db:
        export = await load_post_brand_skill_snapshot(
            db, run_prefix=args.run_prefix, source_map=source_map
        )
    Path(args.output).write_text(
        json.dumps(export.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    print(
        f"run_id={export.run_id} manifest_digest={export.manifest_digest} "
        f"entries={sorted(export.entries)}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "candidates":
        return asyncio.run(_run_candidates(args))
    return asyncio.run(_run_export(args))


if __name__ == "__main__":
    sys.exit(main())
