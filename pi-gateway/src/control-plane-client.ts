import { createHash, createHmac, randomUUID } from "node:crypto";

import type {
  PiGatewayAdapterCatalogEntry,
  PiGatewayClaimResponse,
  PiGatewaySourceEvent,
} from "./protocol.js";
import { parseProviderFailureMetadata, type ProviderFailureMetadata } from "./provider-failure.js";
import type {
  McpAccountingControlPlane,
  McpFailureMetadata,
  McpFinalizeMetadata,
  McpPermit,
  McpToolCallInput,
} from "./mcp-accounting-extension.js";
import {
  CONTROL_PLANE_BASE_PATH,
  normalizePiGatewayClaimResponse,
  parsePiGatewayClaimResponse,
  parsePiGatewaySourceEvent,
} from "./protocol.js";

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

export interface RunControlPlaneTransport extends ControlPlaneTransport, McpAccountingControlPlane {}

/** Stable infrastructure classification; callers must leave the Run for recovery. */
export class ControlPlaneUnavailableError extends Error {
  readonly code = "control_plane_unreachable" as const;

  constructor(cause?: unknown) {
    super("control_plane_unreachable", { cause });
    this.name = "ControlPlaneUnavailableError";
  }
}

/** A durable business rejection returned by the FastAPI control plane. */
export class ControlPlaneBusinessError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(status: number, code: string) {
    super(code);
    this.name = "ControlPlaneBusinessError";
    this.status = status;
    this.code = code;
  }
}

const BACKEND_SERVICE_SLUGS: Readonly<Record<string, string>> = Object.freeze({
  "insight-cube": "insight-cube-mcp",
  "social-grow": "social-grow-mcp",
  "social-grow-content": "social-grow-content-mcp",
  aktools: "aktools-mcp",
});

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
    return response === undefined
      ? undefined
      : normalizePiGatewayClaimResponse(parsePiGatewayClaimResponse(response));
  }

  async heartbeat(
    runId: string,
    attemptId: string,
    leaseToken: string,
    signal?: AbortSignal,
  ): Promise<{ cancel_requested?: boolean; lease_expires_at?: number } | undefined> {
    const result = await this.request<unknown>(
      "POST",
      `/runs/${encodeURIComponent(runId)}/heartbeat`,
      { attempt_id: attemptId },
      leaseToken,
      signal,
    );
    if (result === undefined) return undefined;
    if (!result || typeof result !== "object") throw new Error("pi_gateway_heartbeat_invalid");
    const decision = result as Record<string, unknown>;
    const out: { cancel_requested?: boolean; lease_expires_at?: number } = {};
    if ("cancel_requested" in decision) {
      if (typeof decision.cancel_requested !== "boolean") {
        throw new Error("pi_gateway_heartbeat_invalid");
      }
      out.cancel_requested = decision.cancel_requested;
    }
    if ("lease_expires_at" in decision) {
      const expiry = decision.lease_expires_at;
      if (typeof expiry !== "number" || !Number.isFinite(expiry) || expiry <= 0) {
        throw new Error("pi_gateway_heartbeat_invalid");
      }
      out.lease_expires_at = expiry;
    }
    return out;
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

  async preflightMcp(
    runId: string,
    input: McpToolCallInput,
    leaseToken: string,
  ): Promise<McpPermit> {
    const result = await this.request<unknown>(
      "POST",
      `/runs/${encodeURIComponent(runId)}/mcp/preflight`,
      {
        tool_name: input.tool,
        server: BACKEND_SERVICE_SLUGS[input.server.split("__", 1)[0]] ?? input.server,
        args: input.args,
      },
      leaseToken,
    );
    if (!result || typeof result !== "object" || !("permit_id" in result) || typeof result.permit_id !== "string") {
      throw new Error("mcp_permit_invalid");
    }
    return result as McpPermit;
  }

  async finalizeMcp(
    runId: string,
    permit: McpPermit,
    metadata: McpFinalizeMetadata,
    leaseToken: string,
  ): Promise<unknown> {
    return this.request("POST", `/runs/${encodeURIComponent(runId)}/mcp/finalize`, {
      permit_id: permit.permit_id,
      ...metadata,
    }, leaseToken);
  }

  async failMcp(
    runId: string,
    permit: McpPermit,
    classification: "definitely_not_sent" | "failed_confirmed" | "result_unknown",
    leaseToken: string,
    metadata?: McpFailureMetadata,
  ): Promise<unknown> {
    return this.request("POST", `/runs/${encodeURIComponent(runId)}/mcp/fail`, {
      permit_id: permit.permit_id,
      classification,
      ...(metadata === undefined ? {} : { metadata }),
    }, leaseToken);
  }

  forRun(runId: string, attemptId: string, leaseToken: string): RunControlPlaneTransport {
    return {
      executeInternalTool: (toolName, args) =>
        this.executeInternalTool(toolName, args, runId, attemptId, leaseToken),
      preflight: (input) => this.preflightMcp(runId, input, leaseToken),
      finalize: (permit, metadata) => this.finalizeMcp(runId, permit, metadata, leaseToken),
      fail: (permit, classification, metadata) =>
        this.failMcp(runId, permit, classification, leaseToken, metadata),
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

  async sendUsage(
    runId: string,
    event: PiGatewaySourceEvent,
    leaseToken: string,
  ): Promise<unknown> {
    if (event.event_type !== "usage") throw new Error("pi_usage_event_invalid");
    return this.sendEvent(runId, event, leaseToken);
  }

  async terminal(
    runId: string,
    attemptId: string,
    outcome: "completed" | "completed_with_warnings" | "failed" | "cancelled",
    leaseToken: string,
    payload: Record<string, unknown> = {},
    failureMetadata?: ProviderFailureMetadata,
  ): Promise<unknown> {
    if (failureMetadata !== undefined && outcome !== "failed") {
      throw new Error("pi_provider_failure_metadata_invalid");
    }
    const safeFailureMetadata = failureMetadata === undefined
      ? undefined
      : parseProviderFailureMetadata(failureMetadata);
    return this.request(
      "POST",
      `/runs/${encodeURIComponent(runId)}/terminal`,
      {
        attempt_id: attemptId,
        outcome,
        payload,
        ...(safeFailureMetadata === undefined ? {} : { failure_metadata: safeFailureMetadata }),
      },
      leaseToken,
    );
  }

  private async request<T = unknown>(
    method: string,
    path: string,
    payload: unknown,
    leaseToken?: string,
    externalSignal?: AbortSignal,
  ): Promise<T | undefined> {
    const body = JSON.stringify(payload);
    const timestamp = this.timestamp();
    const nonce = this.nonceFactory();
    // The signature binds the exact mounted path the backend verifies via
    // ``request.url.path``; a relative suffix would not authenticate.
    const fullPath = `${CONTROL_PLANE_BASE_PATH}${path}`;
    const signature = buildSignature(this.internalSecret, method, fullPath, timestamp, nonce, body);
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
    // 外部信号（如租约 beat 超时）联动中止底层 fetch，避免超时后请求仍
    // 在服务端完成续租。
    const linkExternal = (): void => controller.abort();
    if (externalSignal) {
      if (externalSignal.aborted) controller.abort();
      else externalSignal.addEventListener("abort", linkExternal, { once: true });
    }
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.origin}${fullPath}`, {
        method,
        headers,
        body,
        redirect: "manual",
        signal: controller.signal,
      });
    } catch (error) {
      throw new ControlPlaneUnavailableError(error);
    } finally {
      clearTimeout(timer);
      externalSignal?.removeEventListener("abort", linkExternal);
    }
    if (response.status >= 300 && response.status < 400) throw new Error("pi_gateway_redirect_forbidden");
    if (response.status === 204) return undefined;
    if (response.status >= 500) throw new ControlPlaneUnavailableError(new Error(`http_${response.status}`));
    if (!response.ok) {
      let code = `pi_gateway_http_${response.status}`;
      try {
        const errorBody = (await response.json()) as unknown;
        if (
          errorBody &&
          typeof errorBody === "object" &&
          !Array.isArray(errorBody) &&
          typeof (errorBody as { detail?: unknown }).detail === "string" &&
          (errorBody as { detail: string }).detail.length > 0
        ) {
          code = (errorBody as { detail: string }).detail;
        }
      } catch {
        // Preserve the stable HTTP fallback when the error body is not JSON.
      }
      throw new ControlPlaneBusinessError(response.status, code);
    }
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
