import { createHash, createHmac, randomUUID } from "node:crypto";

import type {
  PiGatewayAdapterCatalogEntry,
  PiGatewayClaimResponse,
  PiGatewaySourceEvent,
} from "./protocol.js";
import { parsePiGatewayClaimResponse, parsePiGatewaySourceEvent } from "./protocol.js";

export interface ControlPlaneClientOptions {
  origin: string;
  gatewayId: string;
  internalSecret: string;
  fetchImpl?: typeof fetch;
  nonceFactory?: () => string;
  timestamp?: () => number;
  timeoutMs?: number;
  environment?: "development" | "test" | "production";
}

export interface ControlPlaneTransport {
  executeInternalTool(toolName: string, args: Record<string, unknown>): Promise<unknown>;
}

export class ControlPlaneClient implements ControlPlaneTransport {
  private readonly origin: string;
  private readonly gatewayId: string;
  private readonly internalSecret: string;
  private readonly fetchImpl: typeof fetch;
  private readonly nonceFactory: () => string;
  private readonly timestamp: () => number;
  private readonly timeoutMs: number;

  constructor(options: ControlPlaneClientOptions) {
    const origin = new URL(options.origin);
    const loopback = ["localhost", "127.0.0.1", "::1", "[::1]"].includes(origin.hostname);
    const environment = options.environment ?? "production";
    if (
      origin.pathname !== "/" ||
      origin.search ||
      origin.hash ||
      origin.username ||
      origin.password ||
      (origin.protocol !== "https:" &&
        !(origin.protocol === "http:" && loopback && environment !== "production"))
    ) {
      throw new Error("pi_gateway_origin_invalid");
    }
    if (!options.gatewayId || !options.internalSecret) throw new Error("pi_gateway_config_invalid");
    this.origin = origin.toString().replace(/\/$/, "");
    this.gatewayId = options.gatewayId;
    this.internalSecret = options.internalSecret;
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.nonceFactory = options.nonceFactory ?? randomUUID;
    this.timestamp = options.timestamp ?? (() => Math.floor(Date.now() / 1000));
    this.timeoutMs = options.timeoutMs ?? 15_000;
    if (!Number.isFinite(this.timeoutMs) || this.timeoutMs <= 0 || this.timeoutMs > 120_000) {
      throw new Error("pi_gateway_config_invalid");
    }
  }

  async claim(payload: { capacity: number }): Promise<PiGatewayClaimResponse | undefined> {
    const response = await this.request("POST", "/claims", payload);
    return response === undefined ? undefined : parsePiGatewayClaimResponse(response);
  }

  async heartbeat(runId: string, attemptId: string, leaseToken: string): Promise<unknown> {
    return this.request("POST", `/runs/${encodeURIComponent(runId)}/heartbeat`, { attempt_id: attemptId }, leaseToken);
  }

  async executeInternalTool(
    toolName: string,
    args: Record<string, unknown>,
    runId?: string,
    attemptId?: string,
    leaseToken?: string,
  ): Promise<unknown> {
    if (!runId || !attemptId || !leaseToken) throw new Error("pi_gateway_lease_required");
    return this.request(
      "POST",
      `/runs/${encodeURIComponent(runId)}/internal-tools`,
      { tool_name: toolName, args },
      leaseToken,
    );
  }

  forRun(runId: string, attemptId: string, leaseToken: string): ControlPlaneTransport {
    return {
      executeInternalTool: (toolName, args) =>
        this.executeInternalTool(toolName, args, runId, attemptId, leaseToken),
    };
  }

  async sendEvent(
    runId: string,
    event: PiGatewaySourceEvent,
    leaseToken: string,
  ): Promise<unknown> {
    parsePiGatewaySourceEvent(event);
    return this.request("POST", `/runs/${encodeURIComponent(runId)}/events`, event, leaseToken);
  }

  async terminal(
    runId: string,
    attemptId: string,
    outcome: "completed" | "completed_with_warnings" | "failed" | "cancelled",
    leaseToken: string,
    payload: Record<string, unknown> = {},
  ): Promise<unknown> {
    return this.request("POST", `/runs/${encodeURIComponent(runId)}/terminal`, { attempt_id: attemptId, outcome, payload }, leaseToken);
  }

  private async request<T = unknown>(
    method: string,
    path: string,
    payload: unknown,
    leaseToken?: string,
  ): Promise<T | undefined> {
    const body = JSON.stringify(payload);
    const timestamp = this.timestamp();
    const nonce = this.nonceFactory();
    const signature = buildSignature(this.internalSecret, method, path, timestamp, nonce, body);
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      "X-Pi-Gateway-Id": this.gatewayId,
      "X-Pi-Timestamp": String(timestamp),
      "X-Pi-Nonce": nonce,
      "X-Pi-Signature": signature,
    };
    if (leaseToken) headers["X-Pi-Run-Lease"] = leaseToken;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.origin}/api/v1/internal/pi-gateway/v1${path}`, {
        method,
        headers,
        body,
        redirect: "manual",
        signal: controller.signal,
      });
    } catch (error) {
      throw new Error("pi_gateway_network_error", { cause: error });
    } finally {
      clearTimeout(timer);
    }
    if (response.status >= 300 && response.status < 400) throw new Error("pi_gateway_redirect_forbidden");
    if (response.status === 204) return undefined;
    if (!response.ok) throw new Error(`pi_gateway_http_${response.status}`);
    return (await response.json()) as T;
  }
}

export function buildSignature(
  secret: string,
  method: string,
  path: string,
  timestamp: number,
  nonce: string,
  body: string,
): string {
  const bodyHash = createHash("sha256").update(body).digest("hex");
  const signing = `${method.toUpperCase()}\n${path}\n${timestamp}\n${nonce}\n${bodyHash}`;
  return createHmac("sha256", secret).update(signing).digest("hex");
}
