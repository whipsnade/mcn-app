import { describe, expect, it } from "vitest";

import { GatewayConfigError, loadGatewayConfig } from "../src/config.js";

const VALID_ENV: NodeJS.ProcessEnv = {
  PI_GATEWAY_ID: "gw-test-1",
  PI_GATEWAY_CONTROL_PLANE_URL: "https://control.invalid",
  PI_GATEWAY_INTERNAL_SECRET: "test-only-gateway-secret-0123456789",
  PI_GATEWAY_ENVIRONMENT: "test",
};

function load(overrides: NodeJS.ProcessEnv = {}) {
  return loadGatewayConfig({ ...VALID_ENV, ...overrides });
}

describe("loadGatewayConfig", () => {
  it("loads a valid production configuration with defaults", () => {
    const config = load({ PI_GATEWAY_ENVIRONMENT: "production" });
    expect(config.gatewayId).toBe("gw-test-1");
    expect(config.controlPlaneOrigin).toBe("https://control.invalid");
    expect(config.capacity).toBe(1);
    expect(config.environment).toBe("production");
    expect(config.healthHost).toBe("127.0.0.1");
    expect(config.healthPort).toBeGreaterThan(0);
    expect(config.claimIntervalMs).toBeGreaterThan(0);
    expect(config.claimMaxBackoffMs).toBeGreaterThanOrEqual(config.claimIntervalMs);
    expect(config.heartbeatIntervalMs).toBeGreaterThan(0);
    expect(config.shutdownTimeoutMs).toBeGreaterThan(0);
    expect(config.workerScript).toMatch(/worker-entry\.js$/);
    expect(config.workerExecArgv).toEqual([]);
  });

  it("fails closed on missing identity, origin or secret and never echoes values", () => {
    for (const key of [
      "PI_GATEWAY_ID",
      "PI_GATEWAY_CONTROL_PLANE_URL",
      "PI_GATEWAY_INTERNAL_SECRET",
    ]) {
      const env = { ...VALID_ENV };
      delete env[key];
      expect(() => loadGatewayConfig(env)).toThrow(GatewayConfigError);
      try {
        loadGatewayConfig(env);
      } catch (error) {
        expect(error).toBeInstanceOf(GatewayConfigError);
        expect((error as GatewayConfigError).invalidKeys).toContain(key);
        expect((error as Error).message).toContain(key);
      }
    }
    const secretValue = "short-secret";
    try {
      load({ PI_GATEWAY_INTERNAL_SECRET: secretValue });
      expect.unreachable();
    } catch (error) {
      expect(error).toBeInstanceOf(GatewayConfigError);
      expect((error as Error).message).not.toContain(secretValue);
    }
  });

  it("requires https outside loopback development and test environments", () => {
    expect(() => load({
      PI_GATEWAY_ENVIRONMENT: "production",
      PI_GATEWAY_CONTROL_PLANE_URL: "http://control.invalid",
    })).toThrow(GatewayConfigError);
    expect(() => load({
      PI_GATEWAY_ENVIRONMENT: "development",
      PI_GATEWAY_CONTROL_PLANE_URL: "http://control.invalid",
    })).toThrow(GatewayConfigError);
    expect(load({
      PI_GATEWAY_ENVIRONMENT: "test",
      PI_GATEWAY_CONTROL_PLANE_URL: "http://127.0.0.1:8080",
    }).controlPlaneOrigin).toBe("http://127.0.0.1:8080");
  });

  it("rejects invalid capacity, ports, intervals and gateway id shapes", () => {
    for (const overrides of [
      { PI_GATEWAY_CAPACITY: "0" },
      { PI_GATEWAY_CAPACITY: "129" },
      { PI_GATEWAY_CAPACITY: "1.5" },
      { PI_GATEWAY_HEALTH_PORT: "70000" },
      { PI_GATEWAY_ENVIRONMENT: "production", PI_GATEWAY_HEALTH_PORT: "0" },
      { PI_GATEWAY_HEALTH_HOST: "0.0.0.0" },
      { PI_GATEWAY_CLAIM_INTERVAL_MS: "0" },
      { PI_GATEWAY_CLAIM_MAX_BACKOFF_MS: "5", PI_GATEWAY_CLAIM_INTERVAL_MS: "1000" },
      { PI_GATEWAY_HEARTBEAT_INTERVAL_MS: "60000" },
      { PI_GATEWAY_SHUTDOWN_TIMEOUT_MS: "1" },
      { PI_GATEWAY_ID: "bad id with spaces" },
      { PI_GATEWAY_ENVIRONMENT: "staging" },
    ]) {
      expect(() => load(overrides)).toThrow(GatewayConfigError);
    }
  });

  it("allows an ephemeral health port only outside production", () => {
    expect(load({ PI_GATEWAY_HEALTH_PORT: "0" }).healthPort).toBe(0);
  });

  it("accepts explicit numeric overrides and worker exec argv for local runners", () => {
    const config = load({
      PI_GATEWAY_CAPACITY: "4",
      PI_GATEWAY_HEALTH_PORT: "19555",
      PI_GATEWAY_CLAIM_INTERVAL_MS: "250",
      PI_GATEWAY_CLAIM_MAX_BACKOFF_MS: "5000",
      PI_GATEWAY_WORKER_EXEC_ARGV: '["--import","tsx"]',
    });
    expect(config.capacity).toBe(4);
    expect(config.healthPort).toBe(19555);
    expect(config.claimIntervalMs).toBe(250);
    expect(config.claimMaxBackoffMs).toBe(5000);
    expect(config.workerExecArgv).toEqual(["--import", "tsx"]);
  });

  it("rejects a non-array worker exec argv", () => {
    expect(() => load({ PI_GATEWAY_WORKER_EXEC_ARGV: "tsx" })).toThrow(GatewayConfigError);
  });
});
