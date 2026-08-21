import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { describe, expect, it } from "vitest";

const execFileAsync = promisify(execFile);

describe("PiGateway process-level event delivery", () => {
  it("drains 257 local events after a transient batch failure without overflow", async () => {
    const gatewayUrl = new URL("../src/gateway.ts", import.meta.url).href;
    const script = `
      import { PiGateway } from ${JSON.stringify(gatewayUrl)};

      const diagnostics = [];
      const errors = [];
      const state = { batchCalls: 0, terminalCalls: 0, firstFailure: true };
      let emit;
      let finish;
      let aborted = false;
      const done = new Promise((resolve) => { finish = resolve; });
      const receipt = (events) => ({
        receipts: events.map((event) => ({
          source_event_id: event.source_event_id,
          sequence: event.sequence,
          duplicate: false,
        })),
        last_acked_source_sequence: events[events.length - 1].sequence,
      });
      const controlPlane = {
        claim: async () => ({
          run_id: "run-process-event-pump",
          attempt_id: "attempt-process-event-pump",
          lease_token: "lease-token-with-enough-entropy",
          lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
          runtime_snapshot: {},
          transcript: [],
          secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
          adapter_catalog: [],
          internal_tools: [],
        }),
        heartbeat: async () => ({ cancel_requested: false }),
        sendEventBatch: async (_runId, events) => {
          state.batchCalls += 1;
          if (state.firstFailure) {
            state.firstFailure = false;
            throw Object.assign(new Error("local transient"), { code: "pi_gateway_network_error" });
          }
          return receipt(events);
        },
        terminal: async () => { state.terminalCalls += 1; },
      };
      const gateway = new PiGateway({
        controlPlane,
        capacity: 1,
        eventDeliveryRetryBaseMs: 1,
        onError: (error) => errors.push(error?.code ?? error?.message ?? "unknown"),
        onEventDeliveryDiagnostic: (diagnostic) => diagnostics.push(diagnostic),
        worker: async () => ({
          done,
          abort: () => {
            if (!aborted) {
              aborted = true;
              finish();
            }
          },
          onEvent: (listener) => {
            emit = listener;
            return () => undefined;
          },
        }),
      });
      const tick = gateway.tick();
      while (!emit) await new Promise((resolve) => queueMicrotask(resolve));
      for (let sequence = 1; sequence <= 257; sequence += 1) {
        emit({
          source_event_id: "attempt-process-event-pump:" + sequence,
          sequence,
          event_type: "message.start",
          payload: {},
        });
        if (sequence % 4 === 0) await new Promise((resolve) => setImmediate(resolve));
      }
      finish();
      await tick;
      await gateway.stop();
      process.stdout.write(JSON.stringify({ state, errors, diagnostics }));
    `;

    const { stdout, stderr } = await execFileAsync(
      process.execPath,
      ["--import", "tsx", "--input-type=module", "-e", script],
      { cwd: new URL("..", import.meta.url), timeout: 15_000, maxBuffer: 2 * 1024 * 1024 },
    );
    expect(stderr).toBe("");
    const result = JSON.parse(stdout) as {
      state: { batchCalls: number; terminalCalls: number };
      errors: string[];
      diagnostics: Array<{ kind: string; last_acked_source_sequence: number | null }>;
    };
    expect(result.state.batchCalls).toBeGreaterThan(1);
    expect(result.state.terminalCalls).toBe(1);
    expect(result.errors).not.toContain("event_buffer_overflow");
    expect(result.diagnostics.some((item) => item.kind === "ack" && item.last_acked_source_sequence === 257)).toBe(true);
  });
});
