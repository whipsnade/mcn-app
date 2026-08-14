export interface WorkerPoolOptions {
  capacity: number;
  onWorkerError?: (error: unknown) => void;
}

type Task = () => Promise<unknown>;

/** A bounded FIFO task pool with an explicit draining mode. */
export class WorkerPool {
  private readonly capacity: number;
  private readonly onWorkerError: (error: unknown) => void;
  private readonly queue: Array<{ task: Task; resolve: () => void; reject: (error: unknown) => void }> = [];
  private readonly idleWaiters: Array<() => void> = [];
  private readonly aborters = new Set<() => void | Promise<void>>();
  private active = 0;
  private draining = false;

  constructor(options: WorkerPoolOptions) {
    if (!Number.isInteger(options.capacity) || options.capacity < 1 || options.capacity > 128) {
      throw new Error("pi_gateway_capacity_invalid");
    }
    this.capacity = options.capacity;
    this.onWorkerError = options.onWorkerError ?? (() => undefined);
  }

  get activeCount(): number {
    return this.active;
  }

  get queuedCount(): number {
    return this.queue.length;
  }

  setDraining(value: boolean): void {
    this.draining = value;
    if (value) this.rejectQueued();
  }

  rejectQueued(error = new Error("pi_gateway_draining")): void {
    const queued = this.queue.splice(0);
    for (const entry of queued) entry.reject(error);
    this.resolveIdleIfNeeded();
  }

  submit(task: Task): Promise<void> {
    if (this.draining) return Promise.reject(new Error("pi_gateway_draining"));
    return new Promise<void>((resolve, reject) => {
      this.queue.push({ task, resolve, reject });
      if (!this.draining) this.pump();
    });
  }

  registerAbort(abort: () => void | Promise<void>): () => void {
    this.aborters.add(abort);
    return () => this.aborters.delete(abort);
  }

  async abortAll(): Promise<void> {
    await Promise.all([...this.aborters].map(async (abort) => {
      try {
        await abort();
      } catch (error) {
        this.onWorkerError(error);
      }
    }));
  }

  async waitForIdle(): Promise<void> {
    if (this.active === 0 && this.queue.length === 0) return;
    await new Promise<void>((resolve) => this.idleWaiters.push(resolve));
  }

  private pump(): void {
    while (this.active < this.capacity && this.queue.length > 0) {
      const entry = this.queue.shift();
      if (!entry) return;
      this.active += 1;
      void this.run(entry);
    }
  }

  private async run(entry: { task: Task; resolve: () => void; reject: (error: unknown) => void }): Promise<void> {
    try {
      await entry.task();
      entry.resolve();
    } catch (error) {
      this.onWorkerError(error);
      entry.reject(error);
    } finally {
      this.active -= 1;
      if (!this.draining) this.pump();
      this.resolveIdleIfNeeded();
    }
  }

  private resolveIdleIfNeeded(): void {
    if (this.active === 0 && this.queue.length === 0) {
      this.idleWaiters.splice(0).forEach((resolve) => resolve());
    }
  }
}
