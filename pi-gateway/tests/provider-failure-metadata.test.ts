import { describe, expect, it } from "vitest";

import {
  buildProviderFailureMetadata,
  isSafeProviderFailureMetadata,
  parseProviderFailureMetadata,
} from "../src/provider-failure.js";
import { PiModelProviderError } from "../src/model-request-budget.js";
import { parseWorkerFailureFrame } from "../src/worker-entry.js";

describe("provider_failure_v1", () => {
  it.each([
    ["401 Unauthorized", "authentication", 401],
    ["400 invalid request", "invalid_request", 400],
    ["HTTP 403 Forbidden", "authorization", 403],
    ["404 model not found", "model_not_found", 404],
    ["status=429 rate limit exceeded", "rate_limited", 429],
    ["422 invalid request", "invalid_request", 422],
    ["context_length_exceeded: prompt is too long", "context_length", undefined],
    ["request timed out", "timeout", undefined],
    ["ECONNRESET while connecting", "network", undefined],
    ["500 upstream failure", "upstream_5xx", 500],
    ["502 upstream failure", "upstream_5xx", 502],
    ["503 upstream failure", "upstream_5xx", 503],
    ["529 overloaded", "upstream_5xx", 529],
    ["provider returned an unfamiliar failure", "unknown", undefined],
  ])("classifies %s without retaining raw text", (errorMessage, failureClass, httpStatus) => {
    const metadata = buildProviderFailureMetadata(
      { stopReason: "error", errorMessage },
      new Date("2026-08-21T08:00:00.000Z"),
    );

    expect(metadata).toMatchObject({
      version: "provider_failure_v1",
      failure_class: failureClass,
      error_fingerprint: expect.stringMatching(/^[0-9a-f]{64}$/),
      observed_at: "2026-08-21T08:00:00.000Z",
    });
    if (httpStatus === undefined) expect(metadata).not.toHaveProperty("http_status");
    else expect(metadata.http_status).toBe(httpStatus);
    expect(isSafeProviderFailureMetadata(metadata)).toBe(true);
    expect(JSON.stringify(metadata)).not.toContain(errorMessage);
  });

  it("classifies an aborted SDK message distinctly", () => {
    expect(buildProviderFailureMetadata({ stopReason: "aborted", errorMessage: "cancelled" }))
      .toMatchObject({ failure_class: "aborted" });
  });

  it("keeps only a bounded provider request id when the error labels it explicitly", () => {
    const metadata = buildProviderFailureMetadata({
      stopReason: "error",
      errorMessage: 'status=500 request_id="req_abc-123"',
    });
    expect(metadata.provider_request_id).toBe("req_abc-123");
    expect(parseProviderFailureMetadata({
      version: "provider_failure_v1",
      failure_class: "unknown",
      provider_request_id: "provider-req-123",
      error_fingerprint: "a".repeat(64),
    }).provider_request_id).toBe("provider-req-123");
  });

  it("never projects secrets, bearer values, prompts, or response bodies", () => {
    const raw = "401 api_key=sk-proj-secret Bearer bearer-secret prompt=分析瑞幸咖啡 response body=private";
    const metadata = buildProviderFailureMetadata({ stopReason: "error", errorMessage: raw });
    const serialized = JSON.stringify(metadata);

    expect(serialized).not.toContain("sk-proj-secret");
    expect(serialized).not.toContain("bearer-secret");
    expect(serialized).not.toContain("分析瑞幸咖啡");
    expect(serialized).not.toContain("private");
    expect(metadata.provider_request_id).toBeUndefined();
    expect(new PiModelProviderError(metadata).message).toBe("pi_model_provider_error");
    expect(JSON.stringify(new PiModelProviderError(metadata))).not.toContain("sk-proj-secret");
  });

  it("does not infer a provider class from incidental numbers or vague words", () => {
    expect(buildProviderFailureMetadata({ stopReason: "error", errorMessage: "job 500 completed" }))
      .toMatchObject({ failure_class: "unknown" });
    expect(buildProviderFailureMetadata({ stopReason: "error", errorMessage: "network diagnostics were recorded" }))
      .toMatchObject({ failure_class: "unknown" });
  });

  it("fails closed for extra keys, invalid status, unsafe request ids, and bad fingerprints", () => {
    const valid = {
      version: "provider_failure_v1",
      failure_class: "authentication",
      http_status: 401,
      provider_request_id: "req_safe-123",
      error_fingerprint: "a".repeat(64),
      observed_at: "2026-08-21T08:00:00.000Z",
    } as const;
    expect(parseProviderFailureMetadata(valid)).toEqual(valid);
    expect(() => parseProviderFailureMetadata({ ...valid, extra: true })).toThrow("pi_provider_failure_metadata_invalid");
    expect(() => parseProviderFailureMetadata({ ...valid, http_status: 99 })).toThrow("pi_provider_failure_metadata_invalid");
    expect(() => parseProviderFailureMetadata({ ...valid, provider_request_id: "Bearer secret" })).toThrow("pi_provider_failure_metadata_invalid");
    expect(() => parseProviderFailureMetadata({ ...valid, error_fingerprint: "not-a-sha" })).toThrow("pi_provider_failure_metadata_invalid");
    expect(() => parseProviderFailureMetadata({ ...valid, observed_at: "2026-02-30T08:00:00.000Z" })).toThrow("pi_provider_failure_metadata_invalid");
    expect(() => parseProviderFailureMetadata({ ...valid, http_status: null } as unknown)).toThrow("pi_provider_failure_metadata_invalid");
  });

  it("fails closed when a child failure frame is tampered with", () => {
    const metadata = buildProviderFailureMetadata({ stopReason: "error", errorMessage: "401" });
    expect(parseWorkerFailureFrame({
      type: "failed",
      runId: "run-1",
      errorCode: "pi_model_provider_error",
      failure_metadata: metadata,
    })).toMatchObject({ errorCode: "pi_model_provider_error", failure_metadata: metadata });
    expect(() => parseWorkerFailureFrame({
      type: "failed",
      runId: "run-1",
      errorCode: "pi_model_provider_error",
      failure_metadata: { ...metadata, extra: "tampered" },
    })).toThrow("pi_worker_failure_frame_invalid");
    expect(() => parseWorkerFailureFrame({
      type: "failed",
      runId: "run-1",
      errorCode: "worker_error",
      failure_metadata: metadata,
    })).toThrow("pi_worker_failure_frame_invalid");
    expect(() => parseWorkerFailureFrame({
      type: "failed",
      runId: "run-2",
      errorCode: "pi_model_provider_error",
      failure_metadata: metadata,
    }, "run-1")).toThrow("pi_worker_failure_frame_invalid");
  });
});
