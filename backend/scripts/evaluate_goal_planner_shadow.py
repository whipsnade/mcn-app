from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys


if __package__ in {None, ""}:
    # 运行手册以文件路径执行脚本；显式加入 backend 根，避免只解析到 scripts/。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.session import SessionFactory  # noqa: E402
from app.goals.evaluation import summarize_goal_planner_logs  # noqa: E402
from app.model.models import ModelPromptLog  # noqa: E402


async def run(limit: int) -> None:
    async with SessionFactory() as db:
        rows = list(
            (
                await db.scalars(
                    select(ModelPromptLog)
                    .where(ModelPromptLog.purpose == "goal_planner")
                    .order_by(
                        ModelPromptLog.created_at.desc(),
                        ModelPromptLog.id.desc(),
                    )
                    # Planner 每个 task 最多两次语义 attempt；先取 2 * limit
                    # 原始行，task 级汇总后再截断，避免重试挤掉任务样本。
                    .limit(2 * limit)
                )
            ).all()
        )
    print(
        json.dumps(
            summarize_goal_planner_logs(rows, limit=limit),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 1000:
        parser.error("--limit must be between 1 and 1000")
    asyncio.run(run(args.limit))


if __name__ == "__main__":
    main()
