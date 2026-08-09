import { describe, expect, it, vi } from "vitest";

import { WorkerPool } from "../src/worker-pool.js";

describe("WorkerPool", () => {
  it("never exceeds capacity and drains only the active workers", async () => {
    let active = 0;
    let peak = 0;
    const release: Array<() => void> = [];
    const pool = new WorkerPool({ capacity: 2, onWorkerError: vi.fn() });

    const run = async () => {
      active += 1;
      peak = Math.max(peak, active);
      await new Promise<void>((resolve) => release.push(resolve));
      active -= 1;
    };
    const tasks = [pool.submit(run), pool.submit(run)];
    expect(peak).toBe(2);
    expect(pool.activeCount).toBe(2);
    pool.setDraining(true);
    await expect(pool.submit(run)).rejects.toThrow("pi_gateway_draining");
    release.splice(0).forEach((resolve) => resolve());
    await Promise.all(tasks);
    await pool.waitForIdle();
    expect(pool.activeCount).toBe(0);
  });

  it("rejects queued work when draining starts", async () => {
    let release!: () => void;
    const pool = new WorkerPool({ capacity: 1 });
    const active = pool.submit(() => new Promise<void>((resolve) => { release = resolve; }));
    const queued = pool.submit(async () => {
      throw new Error("queued task must not start");
    });

    pool.setDraining(true);
    await expect(queued).rejects.toThrow("pi_gateway_draining");
    release();
    await active;
    await pool.waitForIdle();
    expect(pool.queuedCount).toBe(0);
  });
});
