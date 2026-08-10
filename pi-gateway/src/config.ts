import { fileURLToPath } from "node:url";

export type GatewayEnvironment = "development" | "test" | "production";

export interface GatewayConfig {
  gatewayId: string;
  controlPlaneOrigin: string;
  internalSecret: string;
  capacity: number;
  environment: GatewayEnvironment;
  healthHost: string;
  healthPort: number;
  claimIntervalMs: number;
  claimMaxBackoffMs: number;
  heartbeatIntervalMs: number;
  shutdownTimeoutMs: number;
  maxBufferedEvents: number;
  workerScript: string;
  workerExecArgv: string[];
}

/**
 * Fail-closed configuration error.  The message and ``invalidKeys`` only ever
 * contain environment variable *names*; values are never echoed.
 */
export class GatewayConfigError extends Error {
  readonly code = "pi_gateway_config_invalid" as const;
  readonly invalidKeys: readonly string[];

  constructor(invalidKeys: readonly string[]) {
    super(`pi_gateway_config_invalid:${invalidKeys.join(",")}`);
    this.name = "GatewayConfigError";
    this.invalidKeys = [...invalidKeys];
  }
}

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);
const GATEWAY_ID_PATTERN = /^[A-Za-z0-9._:-]{1,128}$/;

function readString(
  env: NodeJS.ProcessEnv,
  key: string,
  invalid: string[],
  options: { pattern?: RegExp; minLength?: number; maxLength?: number } = {},
): string | undefined {
  const value = env[key];
  if (value === undefined || value === "") {
    invalid.push(key);
    return undefined;
  }
  if (options.pattern && !options.pattern.test(value)) {
    invalid.push(key);
    return undefined;
  }
  if (options.minLength !== undefined && value.length < options.minLength) {
    invalid.push(key);
    return undefined;
  }
  if (options.maxLength !== undefined && value.length > options.maxLength) {
    invalid.push(key);
    return undefined;
  }
  return value;
}

function readInteger(
  env: NodeJS.ProcessEnv,
  key: string,
  fallback: number,
  min: number,
  max: number,
  invalid: string[],
): number {
  const raw = env[key];
  if (raw === undefined || raw === "") return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < min || value > max) {
    invalid.push(key);
    return fallback;
  }
  return value;
}

function readOrigin(
  env: NodeJS.ProcessEnv,
  environment: GatewayEnvironment,
  invalid: string[],
): string | undefined {
  const raw = env.PI_GATEWAY_CONTROL_PLANE_URL;
  if (!raw) {
    invalid.push("PI_GATEWAY_CONTROL_PLANE_URL");
    return undefined;
  }
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    invalid.push("PI_GATEWAY_CONTROL_PLANE_URL");
    return undefined;
  }
  const loopback = LOOPBACK_HOSTS.has(url.hostname);
  const valid =
    url.pathname === "/" &&
    !url.search &&
    !url.hash &&
    !url.username &&
    !url.password &&
    (url.protocol === "https:" ||
      (url.protocol === "http:" && loopback && environment !== "production"));
  if (!valid) {
    invalid.push("PI_GATEWAY_CONTROL_PLANE_URL");
    return undefined;
  }
  return url.toString().replace(/\/$/, "");
}

/**
 * Validate the process environment and return the immutable Gateway config.
 * Any violation throws ``GatewayConfigError`` before any network or worker
 * machinery starts.
 */
export function loadGatewayConfig(env: NodeJS.ProcessEnv): GatewayConfig {
  const invalid: string[] = [];
  const environmentRaw = env.PI_GATEWAY_ENVIRONMENT ?? "production";
  const environment = (["development", "test", "production"] as const).find(
    (value) => value === environmentRaw,
  );
  if (environment === undefined) invalid.push("PI_GATEWAY_ENVIRONMENT");
  const resolvedEnvironment: GatewayEnvironment = environment ?? "production";

  const gatewayId = readString(env, "PI_GATEWAY_ID", invalid, { pattern: GATEWAY_ID_PATTERN });
  const internalSecret = readString(env, "PI_GATEWAY_INTERNAL_SECRET", invalid, {
    minLength: 16,
    maxLength: 512,
  });
  const controlPlaneOrigin = readOrigin(env, resolvedEnvironment, invalid);

  const capacity = readInteger(env, "PI_GATEWAY_CAPACITY", 1, 1, 128, invalid);
  const claimIntervalMs = readInteger(env, "PI_GATEWAY_CLAIM_INTERVAL_MS", 1_000, 10, 60_000, invalid);
  const claimMaxBackoffMs = readInteger(
    env,
    "PI_GATEWAY_CLAIM_MAX_BACKOFF_MS",
    30_000,
    10,
    300_000,
    invalid,
  );
  if (claimMaxBackoffMs < claimIntervalMs && !invalid.includes("PI_GATEWAY_CLAIM_MAX_BACKOFF_MS")) {
    invalid.push("PI_GATEWAY_CLAIM_MAX_BACKOFF_MS");
  }
  const heartbeatIntervalMs = readInteger(
    env,
    "PI_GATEWAY_HEARTBEAT_INTERVAL_MS",
    20_000,
    10,
    55_000,
    invalid,
  );
  const shutdownTimeoutMs = readInteger(
    env,
    "PI_GATEWAY_SHUTDOWN_TIMEOUT_MS",
    10_000,
    100,
    120_000,
    invalid,
  );
  const maxBufferedEvents = readInteger(env, "PI_GATEWAY_MAX_BUFFERED_EVENTS", 256, 1, 100_000, invalid);

  const healthHost = env.PI_GATEWAY_HEALTH_HOST ?? "127.0.0.1";
  if (!LOOPBACK_HOSTS.has(healthHost)) invalid.push("PI_GATEWAY_HEALTH_HOST");
  const healthPortMin = resolvedEnvironment === "production" ? 1 : 0;
  const healthPort = readInteger(
    env,
    "PI_GATEWAY_HEALTH_PORT",
    9_471,
    healthPortMin,
    65_535,
    invalid,
  );

  const workerScript =
    env.PI_GATEWAY_WORKER_SCRIPT ??
    fileURLToPath(new URL("./worker-entry.js", import.meta.url));
  let workerExecArgv: string[] = [];
  const rawExecArgv = env.PI_GATEWAY_WORKER_EXEC_ARGV;
  if (rawExecArgv !== undefined && rawExecArgv !== "") {
    try {
      const parsed: unknown = JSON.parse(rawExecArgv);
      if (
        !Array.isArray(parsed) ||
        parsed.some((item) => typeof item !== "string" || item.length === 0 || item.length > 256)
      ) {
        throw new Error("shape");
      }
      workerExecArgv = parsed as string[];
    } catch {
      invalid.push("PI_GATEWAY_WORKER_EXEC_ARGV");
    }
  }

  if (invalid.length > 0) throw new GatewayConfigError(invalid);
  return {
    gatewayId: gatewayId as string,
    controlPlaneOrigin: controlPlaneOrigin as string,
    internalSecret: internalSecret as string,
    capacity,
    environment: resolvedEnvironment,
    healthHost,
    healthPort,
    claimIntervalMs,
    claimMaxBackoffMs,
    heartbeatIntervalMs,
    shutdownTimeoutMs,
    maxBufferedEvents,
    workerScript,
    workerExecArgv,
  };
}
