import { fork, type ChildProcess } from "node:child_process";
import { fileURLToPath } from "node:url";

import { createProductionPiSession } from "./pi-session.js";
import { buildSecretEnv, clearSecretEnv } from "./secret-env.js";
import type { McpAccountingControlPlane } from "./mcp-accounting-extension.js";
import type { ControlPlaneTransport } from "./control-plane-client.js";
import { WorkerRpcClient, WorkerRpcControlPlane } from "./ipc-rpc.js";
import { PiSdkUsageProjector, projectPiSdkEvent } from "./event-projector.js";
import type {
  ClaimedRun,
  FakeScriptStep,
  PiGatewaySourceEvent,
  PiRunSession,
  PiSdkEvent,
  PiSessionFactory,
  SecretBundle,
} from "./protocol.js";

export interface WorkerOptions {
  sessionFactory?: PiSessionFactory;
  fakeProvider?: boolean;
  fakeScript?: readonly FakeScriptStep[];
  parentEnv?: NodeJS.ProcessEnv;
  /** Projected, secret-free source events delivered to the Gateway buffer. */
  onEvent?: (event: PiGatewaySourceEvent) => void;
  /** Optional raw SDK audit hook; never send this payload to FastAPI. */
  onSdkEvent?: (event: PiSdkEvent) => void;
  onReady?: () => void;
  mcpAccounting?: McpAccountingControlPlane;
  internalTools?: ControlPlaneTransport;
}

export interface IsolatedWorkerOptions {
  workerScript?: string;
  parentEnv?: NodeJS.ProcessEnv;
  execArgv?: string[];
  /** SIGTERM 后到 SIGKILL 的升级窗口；必须小于 Run lease 的安全预算。 */
  abortGraceMs?: number;
}

export interface IsolatedWorkerProcess extends ChildProcess {
  onEvent(listener: (event: PiGatewaySourceEvent) => void): () => void;
  done: Promise<void>;
  /** SIGTERM 并等待 Child 真正 close（grace 后升级 SIGKILL）才返回。 */
  abort(): Promise<void>;
}

export type WorkerFailureCode =
  | "worker_exited"
  | "worker_signaled"
  | "sdk_protocol_error"
  | "worker_error";

export function classifyWorkerExit(
  exitCode: number | null,
  signal: NodeJS.Signals | null,
): "worker_exited" | "worker_signaled" {
  void exitCode;
  return signal !== null ? "worker_signaled" : "worker_exited";
}

export function classifyWorkerError(error: unknown): "sdk_protocol_error" | "worker_error" {
  return error instanceof Error && error.message === "sdk_protocol_error"
    ? "sdk_protocol_error"
    : "worker_error";
}

/** Spawn a child worker with only non-secret claim data on IPC. */
export function spawnIsolatedWorker(
  work: ClaimedRun,
  secrets: SecretBundle,
  options: IsolatedWorkerOptions = {},
): IsolatedWorkerProcess {
  const childEnv = buildSecretEnv(secrets, options.parentEnv, work.runId);
  childEnv.PI_WORKER_CHILD = "1";
  const child = fork(options.workerScript ?? fileURLToPath(import.meta.url), [], {
    env: childEnv,
    execArgv: options.execArgv ?? [],
    stdio: ["ignore", "ignore", "ignore", "ipc"],
    serialization: "advanced",
  });
  const listeners = new Set<(event: PiGatewaySourceEvent) => void>();
  // Child 终帧：{type:"done"} 或 {type:"failed", errorCode}。父进程据此把
  // 业务失败（worker_error）与基础设施崩溃（无终帧的退出/信号）区分开。
  let terminalFrameCode: string | undefined;
  const TERMINAL_FRAME_CODES = new Set(["worker_error", "sdk_protocol_error"]);
  child.on("message", (message: unknown) => {
    if (!message || typeof message !== "object" || !("type" in message)) return;
    if (message.type === "failed" && "errorCode" in message) {
      const code = (message as { errorCode?: unknown }).errorCode;
      if (typeof code === "string" && TERMINAL_FRAME_CODES.has(code)) {
        terminalFrameCode = code;
      }
      return;
    }
    if (message.type !== "event") return;
    if (!("event" in message) || !message.event || typeof message.event !== "object") return;
    for (const listener of listeners) listener(message.event as PiGatewaySourceEvent);
  });
  const done = new Promise<void>((resolve, reject) => {
    child.once("close", (code, signal) => {
      if (code === 0 && signal === null) resolve();
      else if (terminalFrameCode !== undefined) reject(new Error(terminalFrameCode));
      else reject(new Error(signal ? "worker_signaled" : "worker_exited"));
    });
  });
  // `abort()` intentionally waits only for the child to close; callers do not
  // always consume the business/crash outcome. Attach a sink so a rejected
  // done promise can never become an unhandled rejection while preserving the
  // original rejection for callers that explicitly await `handle.done`.
  void done.catch(() => undefined);
  const handle = child as IsolatedWorkerProcess;
  handle.onEvent = (listener) => {
    listeners.add(listener);
    return () => listeners.delete(listener);
  };
  handle.done = done;
  // 幂等 close 信号：任何退出码/信号都 resolve（abort 等待的是进程消失，
  // 不是成功失败）。
  const closed = new Promise<void>((resolve) => {
    child.once("close", () => resolve());
  });
  let abortStarted = false;
  handle.abort = async () => {
    if (abortStarted) return closed;
    abortStarted = true;
    // 已 exit 但尚未 close 时同样等待 close（stdio/IPC 资源排空），保持
    // 「只有 Child 真正 close 才返回」的契约。
    if (child.exitCode !== null || child.signalCode !== null) return closed;
    const graceMs = options.abortGraceMs ?? 5_000;
    // 子进程安装了优雅停机钩子（adapter/SDK 清理），可能永远不退出；
    // 租约丢失或取消必须保证 worker 真正死亡，否则旧 Attempt 会继续
    // 经 IPC 桥执行工具调用（双重执行）。SIGTERM 后有限时升级 SIGKILL，
    // 且只有 Child 真正 close 才返回。
    child.kill("SIGTERM");
    const grace = new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, graceMs);
      timer.unref?.();
    });
    await Promise.race([closed, grace]);
    if (child.exitCode === null && child.signalCode === null) {
      try {
        child.kill("SIGKILL");
      } catch {
        // 进程刚好退出。
      }
      await closed;
    }
  };
  clearSecretEnv(childEnv);
  child.send({ type: "run", work });
  return handle;
}

/** Install idempotent SIGTERM/SIGINT cleanup without terminating the caller. */
export function installWorkerSignalHandlers(cleanup: () => void | Promise<void>): () => void {
  let started = false;
  const handle = (): void => {
    if (started) return;
    started = true;
    void cleanup();
  };
  process.once("SIGTERM", handle);
  process.once("SIGINT", handle);
  return () => {
    process.removeListener("SIGTERM", handle);
    process.removeListener("SIGINT", handle);
  };
}

/** Execute exactly one claimed Run and always close the SDK/session boundary. */
export async function runSingleWorker(
  work: ClaimedRun,
  secrets: SecretBundle,
  options: WorkerOptions = {},
): Promise<void> {
  const env = buildSecretEnv(secrets, options.parentEnv, work.runId);
  let session: PiRunSession | undefined;
  let unsubscribe: (() => void) | undefined;
  let workerSecrets: SecretBundle | undefined = secrets;
  const usageProjector = new PiSdkUsageProjector(work.attemptId);
  let cleanupPromise: Promise<void> | undefined;
  const cleanup = (): Promise<void> => {
    if (cleanupPromise) return cleanupPromise;
    cleanupPromise = (async () => {
      try {
        await session?.abort();
      } finally {
        try {
          unsubscribe?.();
        } finally {
          try {
            await session?.dispose();
          } finally {
            clearSecretEnv(env);
          }
        }
      }
    })();
    return cleanupPromise;
  };
  const removeSignalHandlers = installWorkerSignalHandlers(cleanup);
  try {
    session = options.sessionFactory
      ? await options.sessionFactory.create(work, workerSecrets)
      : await createProductionPiSession(work, workerSecrets, {
        fakeProvider: options.fakeProvider,
        fakeScript: options.fakeScript,
        mcpAccounting: options.mcpAccounting,
        internalTools: options.internalTools,
      });
    workerSecrets = undefined;
    options.onReady?.();
    unsubscribe = session.subscribe((event) => {
      options.onSdkEvent?.(event);
      if (event.type !== "sdk_event") return;
      const projected = projectPiSdkEvent(event.event ?? event, usageProjector);
      for (const item of projected) options.onEvent?.(item);
    });
    await session.prompt(work.userPrompt ?? "");
  } finally {
    try {
      await cleanup();
    } finally {
      removeSignalHandlers();
    }
  }
}

type WorkerMessage = { type: "run"; work: ClaimedRun };

function finishChildProcess(
  message: { type: "done" | "failed"; runId: string; errorCode?: WorkerFailureCode },
  exitCode: number,
): void {
  let finished = false;
  const finish = (): void => {
    if (finished) return;
    finished = true;
    if (process.connected) process.disconnect();
    // 隔离 Worker 的全部持久状态都在 FastAPI；adapter 的 keep-alive 连接会
    // 拖住事件循环，因此终帧落 IPC 后必须硬退出，不能等待自然排空。
    process.exit(exitCode);
  };
  if (!process.send) {
    finish();
    return;
  }
  process.send(message, finish);
}

function readChildSecretBundle(env: NodeJS.ProcessEnv): SecretBundle {
  const modelBaseUrl = env.PI_MODEL_BASE_URL;
  const modelApiKey = env.PI_MODEL_API_KEY;
  const datatapToken = env.PI_DATATAP_TOKEN;
  if (!modelBaseUrl || !modelApiKey || !datatapToken) throw new Error("pi_worker_secret_env_missing");
  const datatapUrls: Record<string, string> = {};
  for (const [key, value] of Object.entries(env)) {
    if (key.startsWith("PI_DATATAP_URL_") && value) {
      datatapUrls[key.slice("PI_DATATAP_URL_".length).toLowerCase()] = value;
    }
  }
  return { modelBaseUrl, modelApiKey, datatapToken, datatapUrls };
}

if (process.env.PI_WORKER_CHILD === "1" && process.send) {
  let started = false;
  // The child never receives the HMAC secret or Run lease; internal tools and
  // MCP accounting are proxied to the parent over this bounded IPC RPC client.
  const rpc = new WorkerRpcClient({
    send: (message) => {
      if (!process.connected || !process.send) throw new Error("worker_rpc_disconnected");
      process.send(message);
    },
    onMessage: (listener) => {
      process.on("message", listener);
      return () => {
        process.removeListener("message", listener);
      };
    },
    onDisconnect: (listener) => {
      process.once("disconnect", listener);
      return () => {
        process.removeListener("disconnect", listener);
      };
    },
  });
  process.on("message", (message: WorkerMessage) => {
    if (started || !message || message.type !== "run") return;
    started = true;
    const bridge = new WorkerRpcControlPlane(rpc);
    void runSingleWorker(message.work, readChildSecretBundle(process.env), {
      fakeProvider: message.work.runtimeSnapshot.model.api === "faux",
      fakeScript: message.work.fakeScript,
      mcpAccounting: bridge,
      internalTools: bridge,
      onReady: () => process.send?.({ type: "ready", runId: message.work.runId }),
      onEvent: (event) => process.send?.({ type: "event", runId: message.work.runId, event }),
    })
      .then(() => {
        rpc.dispose();
        finishChildProcess({ type: "done", runId: message.work.runId }, 0);
      })
      .catch((error) => {
        rpc.dispose();
        finishChildProcess(
          {
            type: "failed",
            runId: message.work.runId,
            errorCode: classifyWorkerError(error),
          },
          1,
        );
      });
  });
}
