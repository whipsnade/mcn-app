/**
 * 带单 Run 临时 token 的最小内部 HTTP client。
 *
 * token 只出现在 `Authorization: Bearer` 头中，绝不进入请求体，因此不会
 * 出现在 audit/事件/日志的 body 投影里。
 */

export interface StartToolCallRequest {
  toolCallId: string;
  toolName: string;
  requestedToolName: string;
  serviceName: string;
  arguments: Record<string, unknown>;
}

export interface StartToolCallResponse {
  trackedCallId: string;
}

export interface SettleToolCallResponse {
  evidenceId?: string;
}

export type PiExtensionStage =
  | "config"
  | "connect"
  | "tools_list"
  | "schema_validate"
  | "tool_register"
  | "audit_start"
  | "mcp_call"
  | "audit_settle";

export interface PiExtensionDiagnostic {
  stage: PiExtensionStage;
  serviceSlug?: string;
  toolName?: string;
  exceptionType?: string;
  errorCode?: string;
}

export class PiPocHttpClient {
  private readonly baseUrl: string;
  private readonly runId: string;
  private readonly token: string;

  constructor(opts: { baseUrl: string; runId: string; token: string }) {
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.runId = opts.runId;
    this.token = opts.token;
  }

  async startToolCall(req: StartToolCallRequest): Promise<StartToolCallResponse> {
    const body = await this.request(`/runs/${this.runId}/tool-calls/start`, {
      call_id: req.toolCallId,
      tool_name: req.toolName,
      requested_tool_name: req.requestedToolName,
      service_name: req.serviceName,
      arguments: req.arguments,
    });
    return { trackedCallId: String(body.call_id) };
  }

  async settleToolCall(callId: string, rawPayload: unknown): Promise<SettleToolCallResponse> {
    const body = await this.request(`/runs/${this.runId}/tool-calls/${callId}/settle`, {
      raw_payload: rawPayload,
    });
    return { evidenceId: body.evidence_id === undefined ? undefined : String(body.evidence_id) };
  }

  async failToolCall(callId: string, error: unknown): Promise<void> {
    await this.request(`/runs/${this.runId}/tool-calls/${callId}/fail`, { error });
  }

  async recordExtensionDiagnostic(diagnostic: PiExtensionDiagnostic): Promise<void> {
    await this.request(`/runs/${this.runId}/diagnostics`, {
      stage: diagnostic.stage,
      service_slug: diagnostic.serviceSlug,
      tool_name: diagnostic.toolName,
      exception_type: diagnostic.exceptionType,
      error_code: diagnostic.errorCode,
    });
  }

  async failSingleToolSmoke(code: string): Promise<void> {
    await this.request(`/runs/${this.runId}/smoke-failed`, { code });
  }

  async completeSingleToolSmoke(): Promise<void> {
    await this.request(`/runs/${this.runId}/smoke-succeeded`, {});
  }

  async executeInternalTool(
    toolName: string,
    argumentsValue: Record<string, unknown>,
  ): Promise<unknown> {
    const body = await this.request(`/runs/${this.runId}/internal-tools`, {
      tool_name: toolName,
      arguments: argumentsValue,
    });
    return body.result;
  }

  private async request(path: string, body: unknown): Promise<Record<string, unknown>> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      throw new Error(`pi_poc_http:${response.status}`);
    }
    const json = (await response.json()) as unknown;
    if (json === null || typeof json !== "object" || Array.isArray(json)) {
      throw new Error("pi_poc_http:invalid_json");
    }
    return json as Record<string, unknown>;
  }
}
