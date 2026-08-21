import { describe, expect, it, vi } from "vitest";

import {
  ControlPlaneBusinessError,
  ControlPlaneUnavailableError,
} from "../src/control-plane-client.js";
import {
  EventDeliveryPump,
  type EventDeliveryDiagnostic,
} from "../src/event-delivery.js";
import type { PiGatewaySourceEvent } from "../src/protocol.js";

function event(sequence: number, eventType = "message.start"): PiGatewaySourceEvent {
  return {
    source_event_id: "attempt-pump:" + sequence,
    sequence,
    event_type: eventType,
    payload: {},
  };
}

function receipt(events: readonly PiGatewaySourceEvent[], duplicate = false) {
  return {
    receipts: events.map((item) => ({
      source_event_id: item.source_event_id,
      sequence: item.sequence,
      duplicate,
    })),
    last_acked_source_sequence: events[events.length - 1].sequence,
  };
}

function pumpOptions(overrides: Partial<ConstructorParameters<typeof EventDeliveryPump>[0]> = {}) {
  return {
    runId: "run-pump",
    attemptId: "attempt-pump",
    leaseToken: "lease-token-with-enough-entropy",
    maxBufferedEvents: 256,
    retryBaseMs: 1,
    ...overrides,
  };
}

describe("EventDeliveryPump", () => {
  it("serializes bounded batches and preserves source order over a high event count", async () => {
    const batches: PiGatewaySourceEvent[][] = [];
    const diagnostics: EventDeliveryDiagnostic[] = [];
    const pump = new EventDeliveryPump(pumpOptions({
      sendEventBatch: vi.fn(async (_runId, events) => {
        batches.push([...events]);
        return receipt(events);
      }),
      onDiagnostic: (diagnostic) => diagnostics.push(diagnostic),
    }));

    for (let sequence = 1; sequence <= 65; sequence += 1) {
      expect(pump.enqueue(event(sequence))).toBe(true);
    }

    await expect(pump.drain()).resolves.toBe(true);
    expect(batches.every((batch) => batch.length <= 32)).toBe(true);
    expect(batches.length).toBeGreaterThanOrEqual(3);
    expect(batches.flat().map((item) => item.sequence)).toEqual(
      Array.from({ length: 65 }, (_, index) => index + 1),
    );
    expect(pump.lastAckedSequence).toBe(65);
    expect(Math.max(...diagnostics.map((item) => item.queue_high_water))).toBeGreaterThan(32);
    expect(JSON.stringify(diagnostics)).not.toMatch(/prompt|secret|token|payload/i);
  });

  it("replays the exact unacknowledged batch on transient failure", async () => {
    const batches: PiGatewaySourceEvent[][] = [];
    let calls = 0;
    const pump = new EventDeliveryPump(pumpOptions({
      sendEventBatch: vi.fn(async (_runId, events) => {
        batches.push([...events]);
        calls += 1;
        if (calls === 1) throw Object.assign(new Error("temporary network"), { code: "pi_gateway_network_error" });
        return receipt(events);
      }),
    }));

    pump.enqueue(event(1));
    pump.enqueue(event(2));

    await expect(pump.drain()).resolves.toBe(true);
    expect(batches.length).toBeGreaterThanOrEqual(3);
    expect(batches[1]).toEqual(batches[0]);
  });

  it("does not retry business rejection or malformed receipts", async () => {
    const businessPermanent = vi.fn();
    const businessPump = new EventDeliveryPump(pumpOptions({
      sendEventBatch: vi.fn(async () => {
        throw new ControlPlaneBusinessError(409, "pi_gateway_source_sequence_gap");
      }),
      onPermanentFailure: businessPermanent,
    }));
    businessPump.enqueue(event(1));
    await expect(businessPump.drain()).resolves.toBe(false);
    expect(businessPermanent).toHaveBeenCalledWith(expect.objectContaining({
      code: "control_plane_unreachable",
      failureClass: "business_rejection",
      status: 409,
    }));

    const protocolPermanent = vi.fn();
    const protocolPump = new EventDeliveryPump(pumpOptions({
      sendEventBatch: vi.fn(async () => ({
        receipts: [{ source_event_id: "attempt-pump:9", sequence: 9, duplicate: false }],
        last_acked_source_sequence: 9,
      })),
      onPermanentFailure: protocolPermanent,
    }));
    protocolPump.enqueue(event(1));
    await expect(protocolPump.drain()).resolves.toBe(false);
    expect(protocolPermanent).toHaveBeenCalledWith(expect.objectContaining({
      code: "control_plane_unreachable",
      failureClass: "protocol",
    }));
  });

  it("retries bounded 5xx failures and then fails closed", async () => {
    const sendEventBatch = vi.fn(async () => {
      throw new ControlPlaneUnavailableError(new Error("upstream"), "http_5xx", 503);
    });
    const permanent = vi.fn();
    const pump = new EventDeliveryPump(pumpOptions({
      sendEventBatch,
      maxTransientRetries: 2,
      onPermanentFailure: permanent,
    }));
    pump.enqueue(event(1));

    await expect(pump.drain()).resolves.toBe(false);
    expect(sendEventBatch).toHaveBeenCalledTimes(3);
    expect(permanent).toHaveBeenCalledWith(expect.objectContaining({
      failureClass: "http_5xx",
      status: 503,
    }));
  });

  it("keeps the legacy single-event endpoint compatible", async () => {
    const sendEvent = vi.fn().mockResolvedValue(undefined);
    const pump = new EventDeliveryPump(pumpOptions({ sendEvent }));
    pump.enqueue(event(1));
    pump.enqueue(event(2));

    await expect(pump.drain()).resolves.toBe(true);
    expect(sendEvent).toHaveBeenNthCalledWith(1, "run-pump", event(1), "lease-token-with-enough-entropy");
    expect(sendEvent).toHaveBeenNthCalledWith(2, "run-pump", event(2), "lease-token-with-enough-entropy");
  });
});
