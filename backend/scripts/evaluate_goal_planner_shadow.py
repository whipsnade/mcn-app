from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import select

from app.db.session import SessionFactory
from app.goals.evaluation import summarize_goal_planner_logs
from app.model.models import ModelPromptLog


async def run(limit: int) -> None:
    async with SessionFactory() as db:
        rows = list(
            (
                await db.scalars(
                    select(ModelPromptLog)
                    .where(ModelPromptLog.purpose == "goal_planner")
                    .order_by(ModelPromptLog.created_at.desc())
                    .limit(limit)
                )
            ).all()
        )
    print(
        json.dumps(
            summarize_goal_planner_logs(rows),
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
