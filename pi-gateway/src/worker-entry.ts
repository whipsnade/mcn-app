import { fork, type ChildProcess } from "node:child_process";
import { fileURLToPath } from "node:url";

import { createProductionPiSession } from "./pi-session.js";
import { buildSecretEnv, clearSecretEnv } from "./secret-env.js";
import type { ClaimedRun, PiRunSession, PiSdkEvent, PiSessionFactory, SecretBundle } from "./protocol.js";

export interface WorkerOptions {
  sessionFactory?: PiSessionFactory;
  fakeProvider?: boolean;
  parentEnv?: NodeJS.ProcessEnv;
  onEvent?: (event: PiSdkEvent) => void;
  onReady?: () => void;
}

export interface IsolatedWorkerOptions {
  workerScript?: string;
  parentEnv?: NodeJS.ProcessEnv;
  execArgv?: string[];
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
): ChildProcess {
  const childEnv = buildSecretEnv(secrets, options.parentEnv, work.runId);
  childEnv.PI_WORKER_CHILD = "1";
  const child = fork(options.workerScript ?? fileURLToPath(import.meta.url), [], {
    env: childEnv,
    execArgv: options.execArgv ?? [],
    stdio: ["ignore", "ignore", "ignore", "ipc"],
    serialization: "advanced",
  });
  clearSecretEnv(childEnv);
  child.send({ type: "run", work });
  return child;
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
      : await createProductionPiSession(work, workerSecrets, { fakeProvider: options.fakeProvider });
    workerSecrets = undefined;
    options.onReady?.();
    unsubscribe = session.subscribe((event) => options.onEvent?.(event));
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
    process.exitCode = exitCode;
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
  process.on("message", (message: WorkerMessage) => {
    if (started || !message || message.type !== "run") return;
    started = true;
    void runSingleWorker(message.work, readChildSecretBundle(process.env), {
      fakeProvider: message.work.runtimeSnapshot.model.api === "faux",
      onReady: () => process.send?.({ type: "ready", runId: message.work.runId }),
    })
      .then(() => finishChildProcess({ type: "done", runId: message.work.runId }, 0))
      .catch((error) => finishChildProcess(
        {
          type: "failed",
          runId: message.work.runId,
          errorCode: classifyWorkerError(error),
        },
        1,
      ));
  });
}
