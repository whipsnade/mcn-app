"""开发库实测脚本：注册新用户 → brainstorm 到 ready → 品牌分析任务 → 复合任务。

用法：cd backend && .venv/bin/python scripts/smoke_multi_intent.py
真实调用模型与 MCP（扣积分，新用户 1000 分），只打 127.0.0.1:8000。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import httpx

BASE = "http://127.0.0.1:8000/api/v1"
POLL_TIMEOUT_S = 900

# brainstorm 画像答案（模型一次一问，循环回答直到 ready）
PROFILE_ANSWERS = [
    "品牌是星巴克，品类是咖啡/现制饮品",
    "平台：小红书、抖音",
    "目标受众：20-35岁一二线城市女性",
    "时间范围：最近30天",
    "达人要求：粉丝10万以上，美食或生活方式类",
    "目标是品牌声量与情感分析，后续圈选达人做投放",
    "目标地区：全国",
    "没有了，开始吧",
]


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=60.0) as client:
        phone = f"139{uuid.uuid4().int % 10**8:08d}"
        r = await client.post("/auth/mock/sms/code", json={"phone": phone})
        r.raise_for_status()
        r = await client.post("/auth/mock/sms/login", json={"phone": phone, "code": "000000"})
        r.raise_for_status()
        token = r.json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        print(f"[ok] 登录新用户 {phone}", flush=True)

        r = await client.post("/sessions", json={})
        r.raise_for_status()
        session_id = r.json()["id"]
        print(f"[ok] 空白会话 {session_id}", flush=True)

        # brainstorm 直到 ready（ready 时后端内联创建第一个任务）
        task_id = None
        for answer in PROFILE_ANSWERS:
            r = await client.post(f"/sessions/{session_id}/brainstorm", json={"content": answer})
            r.raise_for_status()
            data = r.json()
            task_id = data.get("task_id")
            msg = (data.get("message") or {}).get("content", "")
            print(f"[brainstorm] ready={data.get('ready')} task={task_id} msg={msg[:60]}", flush=True)
            if data.get("ready") and task_id:
                break
        if not task_id:
            print("[fail] brainstorm 未 ready，退出")
            return

        # 内联任务是完整 kol_selection（贵）：立即取消，把积分留给场景验证
        r = await client.post(f"/tasks/{task_id}/cancel")
        print(f"[ok] 取消内联任务 {task_id}: {r.status_code}", flush=True)
        # 取消是异步标记：等它真正进入终态，否则下一条 create 会 409（会话串行守卫）
        await wait_task(client, task_id, "内联任务取消")

        # 场景 1：品牌分析（应产出 brand_analysis goal + brand_report）
        await run_and_report(client, session_id, "分析一下星巴克最近30天的品牌声量和用户情感", "场景1 品牌分析")

        # 场景 2：复合任务（品牌分析 → 圈选，应产出 2 个 goal）
        await run_and_report(
            client,
            session_id,
            "分析星巴克最近30天的品牌表现，并根据表现圈选合适的达人",
            "场景2 复合任务",
        )

        r = await client.get(f"/sessions/{session_id}/artifacts/summary")
        print("[summary]", json.dumps(r.json(), ensure_ascii=False)[:1000], flush=True)
        print(f"[done] session_id={session_id}", flush=True)


async def run_and_report(client: httpx.AsyncClient, session_id: str, content: str, label: str) -> None:
    r = await client.post(f"/sessions/{session_id}/tasks", json={"content": content})
    r.raise_for_status()
    data = r.json()
    if data.get("outcome") == "clarify":
        print(f"[{label}] planner 要求澄清: {(data.get('message') or {}).get('content')}", flush=True)
        return
    task = data["task"]
    print(f"[{label}] task={task['id']}", flush=True)
    await wait_task(client, task["id"], label)


async def wait_task(client: httpx.AsyncClient, task_id: str, label: str) -> dict | None:
    deadline = time.time() + POLL_TIMEOUT_S
    while time.time() < deadline:
        await asyncio.sleep(15)
        r = await client.get(f"/tasks/{task_id}")
        r.raise_for_status()
        task = r.json()
        status = task["status"]
        print(f"[{label}] …status={status}", flush=True)
        if status in {"completed", "completed_with_warnings", "failed", "cancelled", "insufficient_balance"}:
            print(f"[{label}] 终态={status} error={task.get('error_code')}", flush=True)
            return task
    print(f"[{label}] 超时未终态", flush=True)
    return None


if __name__ == "__main__":
    asyncio.run(main())
