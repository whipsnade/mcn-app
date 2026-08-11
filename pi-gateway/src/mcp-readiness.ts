import type { ExtensionFactory } from "@earendil-works/pi-coding-agent";

// Keep the bridge independent from the adapter's TypeScript source tree. The
// pinned adapter documents this stable wire channel; importing its `.ts`
// entrypoint would pull all node_modules sources into this package's tsc graph.
const MCP_STATUS_EVENT = "pi-mcp-adapter/status/v1";

interface McpStatusSnapshot {
  readonly version: 1;
  readonly servers: ReadonlyArray<{
    readonly name: string;
    readonly status: string;
    readonly toolCount: number;
    readonly disabled: boolean;
  }>;
}

const DEFAULT_READINESS_TIMEOUT_MS = 30_000;

type ReadinessState = "pending" | "ready" | "failed" | "closed";

export interface McpReadinessGate {
  /** Wait for the adapter to connect every required service and discover tools. */
  waitUntilReady(signal?: AbortSignal): Promise<void>;
  /** Consume the adapter's public, credential-free status snapshot. */
  observeSnapshot(snapshot: unknown): void;
  /** Start a new adapter session generation; stale eager-load state is ignored. */
  beginSession(): void;
  /** Remove listeners and reject any pending wait. */
  dispose(): void;
}

function readinessError(code: string): Error {
  return new Error(code);
}

function isStatusSnapshot(value: unknown): value is McpStatusSnapshot {
  if (!value || typeof value !== "object" || !Array.isArray((value as { servers?: unknown }).servers)) {
    return false;
  }
  const snapshot = value as { version?: unknown; servers: unknown[] };
  return snapshot.version === 1 && snapshot.servers.every((server) => {
    if (!server || typeof server !== "object") return false;
    const item = server as { name?: unknown; status?: unknown; toolCount?: unknown; disabled?: unknown };
    return (
      typeof item.name === "string" &&
      typeof item.status === "string" &&
      Number.isInteger(item.toolCount) &&
      typeof item.disabled === "boolean"
    );
  });
}

function withAbort<T>(promise: Promise<T>, signal: AbortSignal | undefined): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(readinessError("pi_mcp_readiness_aborted"));
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => {
      signal.removeEventListener("abort", onAbort);
      reject(readinessError("pi_mcp_readiness_aborted"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(error);
      },
    );
  });
}

/**
 * Readiness barrier for the production adapter.
 *
 * The adapter deliberately catches session_start initialization errors because
 * its interactive host can continue with a degraded MCP surface. A Gateway
 * worker has a stricter contract: no prompt or business tool may run until all
 * catalogued services are connected and their tool metadata is available.
 */
export function createMcpReadinessGate(
  requiredServices: readonly string[],
  timeoutMs = DEFAULT_READINESS_TIMEOUT_MS,
): McpReadinessGate {
  const services = [...new Set(requiredServices)].filter(Boolean);
  if (services.length === 0) throw readinessError("pi_mcp_catalog_incomplete");
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 300_000) {
    throw readinessError("pi_mcp_readiness_timeout_invalid");
  }

  let state: ReadinessState = "pending";
  let failure: Error | undefined;
  let resolveWait: (() => void) | undefined;
  let rejectWait: ((error: Error) => void) | undefined;
  let waitPromise: Promise<void> | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const settleFailure = (error: Error, nextState: ReadinessState = "failed") => {
    if (state !== "pending") return;
    state = nextState;
    failure = error;
    if (timer) clearTimeout(timer);
    rejectWait?.(error);
    resolveWait = undefined;
    rejectWait = undefined;
  };

  const settleReady = () => {
    if (state !== "pending") return;
    state = "ready";
    if (timer) clearTimeout(timer);
    resolveWait?.();
    resolveWait = undefined;
    rejectWait = undefined;
  };

  return {
    waitUntilReady(signal) {
      if (state === "ready") return Promise.resolve();
      if (state === "failed" || state === "closed") {
        return Promise.reject(failure ?? readinessError("pi_mcp_readiness_failed"));
      }
      waitPromise ??= new Promise<void>((resolve, reject) => {
        resolveWait = resolve;
        rejectWait = reject;
        timer = setTimeout(() => settleFailure(readinessError("pi_mcp_readiness_timeout")), timeoutMs);
        timer.unref?.();
      });
      return withAbort(waitPromise, signal);
    },

    observeSnapshot(rawSnapshot) {
      if (state !== "pending") return;
      if (!isStatusSnapshot(rawSnapshot)) {
        settleFailure(readinessError("pi_mcp_readiness_invalid"));
        return;
      }
      const snapshot = rawSnapshot;
      const byName = new Map(snapshot.servers.map((server) => [server.name, server]));
      for (const service of services) {
        const server = byName.get(service);
        if (!server) return;
        if (["failed", "needs-auth", "disabled"].includes(server.status)) {
          settleFailure(readinessError("pi_mcp_readiness_failed"));
          return;
        }
        if (server.status !== "connected" || server.toolCount < 1) return;
      }
      settleReady();
    },

    beginSession() {
      if (state === "pending" && waitPromise === undefined) return;
      if (state === "pending" && waitPromise !== undefined) {
        settleFailure(readinessError("pi_mcp_session_restarted"));
      }
      if (timer) clearTimeout(timer);
      state = "pending";
      failure = undefined;
      resolveWait = undefined;
      rejectWait = undefined;
      waitPromise = undefined;
    },

    dispose() {
      settleFailure(readinessError("pi_mcp_readiness_closed"), "closed");
    },
  };
}

/** Attach the gate to the adapter's documented status event bus. */
export function createMcpReadinessExtensionFactory(gate: McpReadinessGate): ExtensionFactory {
  return (pi) => {
    const unsubscribe = pi.events.on(MCP_STATUS_EVENT, (snapshot) => gate.observeSnapshot(snapshot));
    pi.on("session_start", () => gate.beginSession());
    pi.on("session_shutdown", () => {
      unsubscribe();
      gate.dispose();
    });
  };
}
