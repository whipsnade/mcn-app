import { pathToFileURL } from "node:url";

import { loadGatewayConfig, GatewayConfigError, type GatewayConfig } from "./config.js";
import { ControlPlaneClient } from "./control-plane-client.js";
import { PiGateway, type GatewayWorkerHandle } from "./gateway.js";
import { startHealthServer, type GatewayMetricsSnapshot } from "./health.js";
import type { GatewaySignalSource } from "./server.js";
import { clearSecretBundle, decryptSecretEnvelope } from "./secret-env.js";
import { attachWorkerRpcBridge, createWorkerRpcHandlers } from "./worker-bridge.js";
import { spawnIsolatedWorker } from "./worker-entry.js";
import type {
  AdapterCatalogEntry,
  ClaimedRun,
  PiGatewayClaimResponse,
  RuntimeSnapshot,
  SkillCatalogEntry,
} from "./protocol.js";

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function snapshotError(): never {
  throw new Error("pi_gateway_claim_snapshot_invalid");
}

/**
 * ``limits.max_decisions`` is the server-owned model request budget.  It must
 * be a present integer in 1..100 — missing, boolean, float, non-positive or
 * out-of-range values all fail closed before any worker is spawned, and the
 * gateway never substitutes a default for the server snapshot.
 */
export function readSnapshotMaxDecisions(snapshot: Record<string, unknown>): number {
  const limits = snapshot.limits;
  if (!isRecord(limits)) throw new Error("pi_gateway_runtime_snapshot_invalid");
  const value = limits.max_decisions;
  if (
    typeof value !== "number" ||
    !Number.isInteger(value) ||
    value < 1 ||
    value > 100
  ) {
    throw new Error("pi_gateway_runtime_snapshot_invalid");
  }
  return value;
}

function skillDescription(content: unknown): string {
  if (typeof content !== "string") return "";
  for (const line of content.split("\n")) {
    if (line.startsWith("description:")) return line.slice("description:".length).trim();
  }
  return "";
}

/**
 * Convert the authenticated backend claim snapshot (snake_case, server-owned)
 * into the worker runtime snapshot.  Any shape violation fails closed before
 * a worker process is spawned; the snapshot carries no secret material.
 */
export function mapClaimRuntimeSnapshot(snapshot: Record<string, unknown>): RuntimeSnapshot {
  if (!isRecord(snapshot)) snapshotError();
  const configVersionId = snapshot.config_version_id;
  if (typeof configVersionId !== "string" || configVersionId.length === 0) snapshotError();
  const model = snapshot.model;
  if (!isRecord(model) || typeof model.name !== "string" || model.name.length === 0) snapshotError();
  const provider =
    typeof model.provider === "string" && model.provider.length > 0 ? model.provider : "custom";
  const capabilityPack = snapshot.capability_pack;
  if (!isRecord(capabilityPack)) snapshotError();
  const profileName = snapshot.profile_name;
  const requiredArtifactContract = snapshot.required_artifact_contract;
  const capabilityPackVersion = snapshot.capability_pack_version;
  const capabilityPackManifestDigest = snapshot.capability_pack_manifest_digest;
  if (profileName !== undefined && (typeof profileName !== "string" || profileName.length === 0)) snapshotError();
  if (requiredArtifactContract !== undefined && requiredArtifactContract !== null && (typeof requiredArtifactContract !== "string" || requiredArtifactContract.length === 0)) snapshotError();
  if (capabilityPackVersion !== undefined && (typeof capabilityPackVersion !== "string" || capabilityPackVersion.length === 0)) snapshotError();
  if (capabilityPackManifestDigest !== undefined && (typeof capabilityPackManifestDigest !== "string" || !/^[0-9a-fA-F]{64}$/.test(capabilityPackManifestDigest))) snapshotError();
  if (capabilityPackVersion !== undefined && capabilityPack.pack_version !== capabilityPackVersion) snapshotError();
  if (capabilityPackManifestDigest !== undefined && capabilityPack.manifest_digest !== capabilityPackManifestDigest) snapshotError();
  if (requiredArtifactContract !== undefined && requiredArtifactContract !== null && profileName === undefined) snapshotError();
  const rootPolicy = capabilityPack.root_policy;
  if (typeof rootPolicy !== "string" || rootPolicy.length === 0) snapshotError();
  const skillsRaw = capabilityPack.skills ?? [];
  if (!Array.isArray(skillsRaw)) snapshotError();
  const skillCatalog: SkillCatalogEntry[] = skillsRaw.map((skill) => {
    if (!isRecord(skill)) snapshotError();
    const { name, version, artifact_contract: artifactContract, content } = skill;
    if (
      typeof name !== "string" || name.length === 0 ||
      typeof version !== "string" || version.length === 0 ||
      typeof artifactContract !== "string" || artifactContract.length === 0
    ) snapshotError();
    return {
      name: name as string,
      version: version as string,
      artifactContract: artifactContract as string,
      description: skillDescription(content),
    };
  });
  const adapterCatalogRaw = snapshot.adapterCatalog;
  if (!Array.isArray(adapterCatalogRaw) || adapterCatalogRaw.length === 0) snapshotError();
  const adapterCatalog: AdapterCatalogEntry[] = adapterCatalogRaw.map((entry) => {
    if (!isRecord(entry)) snapshotError();
    const { service, adapterName, remoteName, schemaDigest } = entry;
    if (
      typeof service !== "string" || service.length === 0 ||
      typeof adapterName !== "string" || adapterName.length === 0 ||
      typeof remoteName !== "string" || remoteName.length === 0 ||
      typeof schemaDigest !== "string" || !/^sha256:[0-9a-fA-F]{64}$/.test(schemaDigest)
    ) snapshotError();
    return {
      service: service as string,
      adapterName: adapterName as string,
      remoteName: remoteName as string,
      schemaDigest: schemaDigest as string,
    };
  });
  return {
    configVersionId: configVersionId as string,
    model: {
      provider,
      id: (model as Record<string, unknown>).name as string,
      // The control-plane contract only supports OpenAI-compatible endpoints;
      // the fake provider stays an explicit worker-entry test hook.
      api: "openai-completions",
    },
    rootPolicy: rootPolicy as string,
    skillCatalog,
    adapterCatalog,
    ...(profileName === undefined ? {} : { profileName: profileName as string }),
    ...(requiredArtifactContract === undefined ? {} : { requiredArtifactContract: requiredArtifactContract as string | null }),
    ...(capabilityPackVersion === undefined ? {} : { capabilityPackVersion: capabilityPackVersion as string }),
    ...(capabilityPackManifestDigest === undefined ? {} : { capabilityPackManifestDigest: capabilityPackManifestDigest as string }),
    maxDecisions: readSnapshotMaxDecisions(snapshot),
  };
}

/** The triggering user message is the last user transcript entry. */
export function extractUserPrompt(transcript: readonly Record<string, unknown>[]): string {
  for (let index = transcript.length - 1; index >= 0; index -= 1) {
    const entry = transcript[index];
    if (entry?.role === "user" && typeof entry.content === "string" && entry.content.length > 0) {
      return entry.content;
    }
  }
  throw new Error("pi_gateway_claim_transcript_invalid");
}

/**
 * Assemble the per-Run isolated worker: decrypt the secret envelope under the
 * exact run/attempt/config/gateway AAD, spawn the child with secrets confined
 * to its environment, then drop every parent-side secret reference.
 */
export function createProductionWorker(options: {
  gatewayId: string;
  controlPlane: ControlPlaneClient;
  workerScript: string;
  workerExecArgv: readonly string[];
  spawn?: typeof spawnIsolatedWorker;
  attachRpcBridge?: typeof attachWorkerRpcBridge;
}): (claim: PiGatewayClaimResponse) => Promise<GatewayWorkerHandle> {
  const spawn = options.spawn ?? spawnIsolatedWorker;
  const attachRpc = options.attachRpcBridge ?? attachWorkerRpcBridge;
  return async (claim) => {
    const runtimeSnapshot = mapClaimRuntimeSnapshot(claim.runtime_snapshot);
    const userPrompt = extractUserPrompt(claim.transcript);
    const internalTools = claim.internal_tools.map((tool) => {
      if (!tool || typeof tool.name !== "string" || tool.name.length === 0) {
        throw new Error("pi_gateway_claim_tools_invalid");
      }
      return tool.name;
    });
    const secrets = await decryptSecretEnvelope(claim.secret_envelope, claim.lease_token, {
      runId: claim.run_id,
      attemptId: claim.attempt_id,
      configVersionId: runtimeSnapshot.configVersionId,
      gatewayId: options.gatewayId,
    });
    try {
      const work: ClaimedRun = {
        runId: claim.run_id,
        attemptId: claim.attempt_id,
        runtimeBackend: "pi",
        runtimeSnapshot,
        userPrompt,
        internalTools,
      };
      // abort grace 必须与 gateway.ts 的租约预算同构：严格小于 lease/4，
      // 保证「beat 超时 × 重试 + grace」总和永远在 lease 之内。
      const leaseMs = Math.max(0, claim.lease_expires_at * 1000 - Date.now());
      const abortGraceMs = Math.max(25, Math.min(5_000, Math.floor(leaseMs / 4)));
      const child = spawn(work, secrets, {
        workerScript: options.workerScript,
        execArgv: [...options.workerExecArgv],
        abortGraceMs,
      });
      // The parent keeps the HMAC secret and lease token; every child tool
      // call crosses this bridge and is re-executed against FastAPI.
      attachRpc(child, createWorkerRpcHandlers(options.controlPlane, claim));
      return child;
    } finally {
      clearSecretBundle(secrets);
    }
  };
}

export interface GatewayMainDependencies {
  env?: NodeJS.ProcessEnv;
  signalSource?: GatewaySignalSource | null;
  fetchImpl?: typeof fetch;
  logger?: (line: string) => void;
  sleep?: (ms: number) => Promise<void>;
  random?: () => number;
  spawnWorker?: typeof spawnIsolatedWorker;
}

const SAFE_ERROR_CODE = /^[a-z0-9_:-]{1,64}$/;

function errorCode(error: unknown): string {
  const candidate =
    error && typeof error === "object" && "code" in error
      ? (error as { code?: unknown }).code
      : error instanceof Error
        ? error.message
        : undefined;
  return typeof candidate === "string" && SAFE_ERROR_CODE.test(candidate) ? candidate : "unknown";
}

/**
 * Production composition root: validated config, signed control-plane client,
 * periodic fair claim loop with bounded backoff, health/metrics HTTP and
 * SIGTERM/SIGINT draining with a bounded exit.  Returns the process exit code.
 */
export async function runGatewayMain(deps: GatewayMainDependencies = {}): Promise<number> {
  const logger = deps.logger ?? ((line: string) => console.log(line));
  let config: GatewayConfig;
  try {
    config = loadGatewayConfig(deps.env ?? process.env);
  } catch (error) {
    if (error instanceof GatewayConfigError) {
      logger(`pi_gateway_config_invalid ${error.invalidKeys.join(",")}`);
      return 1;
    }
    logger("pi_gateway_config_invalid unknown");
    return 1;
  }

  const startedAt = Date.now();
  const state = {
    ready: false,
    draining: false,
    claimsTotal: 0,
    claimsEmpty: 0,
    claimErrors: 0,
    errorsTotal: 0,
    lastErrorCode: null as string | null,
    lastClaimAtMs: null as number | null,
  };
  const recordError = (error: unknown): void => {
    state.errorsTotal += 1;
    state.lastErrorCode = errorCode(error);
  };

  const controlPlane = new ControlPlaneClient({
    origin: config.controlPlaneOrigin,
    gatewayId: config.gatewayId,
    internalSecret: config.internalSecret,
    fetchImpl: deps.fetchImpl,
    environment: config.environment,
    timeoutMs: config.controlTimeoutMs,
  });
  const gateway = new PiGateway({
    controlPlane,
    capacity: config.capacity,
    heartbeatIntervalMs: config.heartbeatIntervalMs,
    controlTimeoutMs: config.controlTimeoutMs,
    shutdownTimeoutMs: config.shutdownTimeoutMs,
    maxBufferedEvents: config.maxBufferedEvents,
    worker: createProductionWorker({
      gatewayId: config.gatewayId,
      controlPlane,
      workerScript: config.workerScript,
      workerExecArgv: config.workerExecArgv,
      spawn: deps.spawnWorker,
    }),
    onError: (error) => {
      recordError(error);
      logger(`pi_gateway_error ${state.lastErrorCode}`);
    },
  });
  const health = await startHealthServer({
    host: config.healthHost,
    port: config.healthPort,
    source: {
      snapshot: (): GatewayMetricsSnapshot => ({
        uptime_seconds: Math.max(0, Math.round((Date.now() - startedAt) / 1000)),
        ready: state.ready && !state.draining,
        draining: state.draining,
        active_workers: gateway.activeCount,
        claims_total: state.claimsTotal,
        claims_empty: state.claimsEmpty,
        claim_errors: state.claimErrors,
        errors_total: state.errorsTotal,
        last_error_code: state.lastErrorCode,
        last_claim_at: state.lastClaimAtMs === null ? null : new Date(state.lastClaimAtMs).toISOString(),
        ...(config.environment === "test" ? { worker_pids: [...gateway.activeWorkerPids] } : {}),
      }),
    },
  });

  let stopping = false;
  const stopController = new AbortController();
  let stopPromise: Promise<void> | undefined;
  const stop = (): Promise<void> => {
    if (!stopPromise) {
      stopPromise = (async () => {
        stopping = true;
        state.draining = true;
        state.ready = false;
        stopController.abort();
        await gateway.stop();
        await health.close();
      })();
    }
    return stopPromise;
  };

  const signalSource = deps.signalSource === undefined ? process : deps.signalSource;
  const signalHandlers: Array<["SIGTERM" | "SIGINT", () => void]> = [];
  if (signalSource) {
    for (const signal of ["SIGTERM", "SIGINT"] as const) {
      const handler = () => {
        void stop();
      };
      signalHandlers.push([signal, handler]);
      signalSource.on(signal, handler);
    }
  }

  const sleep =
    deps.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  const random = deps.random ?? Math.random;
  const interruptibleSleep = async (ms: number): Promise<void> => {
    if (stopping) return;
    // The abort listener is removed as soon as the sleep settles so the
    // shared signal never accumulates reactions across loop iterations.
    let onAbort: (() => void) | undefined;
    const aborted = new Promise<void>((resolve) => {
      onAbort = resolve;
    });
    stopController.signal.addEventListener("abort", onAbort as () => void, { once: true });
    await Promise.race([sleep(ms), aborted]);
    stopController.signal.removeEventListener("abort", onAbort as () => void);
    // Always yield to the event-loop check phase at least once per loop
    // iteration: a synchronous custom sleep must not starve worker IPC,
    // heartbeat timers or fetch completion.
    await new Promise<void>((resolve) => setImmediate(resolve));
  };
  const jitter = (ms: number): number => Math.max(1, Math.round(ms * (0.75 + random() * 0.5)));

  const inFlight = new Set<Promise<void>>();
  let claimFailures = 0;
  state.ready = true;
  logger("pi_gateway_started");
  try {
    while (!stopping) {
      if (gateway.activeCount >= config.capacity) {
        await interruptibleSleep(config.claimIntervalMs);
        continue;
      }
      let dispatch: Awaited<ReturnType<PiGateway["dispatchNext"]>>;
      try {
        dispatch = await gateway.dispatchNext();
      } catch (error) {
        recordError(error);
        dispatch = { outcome: "unavailable" };
      }
      if (dispatch.outcome === "claimed") {
        claimFailures = 0;
        state.claimsTotal += 1;
        state.lastClaimAtMs = Date.now();
        const tracked = dispatch.completion
          .catch((error: unknown) => {
            recordError(error);
          })
          .finally(() => {
            inFlight.delete(tracked);
          });
        inFlight.add(tracked);
        continue;
      }
      if (dispatch.outcome === "empty") {
        claimFailures = 0;
        state.claimsEmpty += 1;
        await interruptibleSleep(jitter(config.claimIntervalMs));
        continue;
      }
      claimFailures += 1;
      state.claimErrors += 1;
      const backoff = Math.min(
        config.claimMaxBackoffMs,
        config.claimIntervalMs * 2 ** claimFailures,
      );
      await interruptibleSleep(jitter(backoff));
    }
  } finally {
    await stop();
    if (signalSource?.removeListener) {
      for (const [signal, handler] of signalHandlers) {
        signalSource.removeListener(signal, handler);
      }
    }
    await Promise.race([
      Promise.allSettled([...inFlight]),
      sleep(config.shutdownTimeoutMs),
    ]);
  }
  logger("pi_gateway_stopped");
  return 0;
}

const invokedDirectly =
  typeof process.argv[1] === "string" &&
  import.meta.url === pathToFileURL(process.argv[1]).href;
if (invokedDirectly) {
  runGatewayMain().then(
    (code) => {
      process.exitCode = code;
    },
    (error: unknown) => {
      console.error(errorCode(error));
      process.exitCode = 1;
    },
  );
}
