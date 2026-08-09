import { describe, expect, it, vi } from "vitest";

import { PiGatewayServer } from "../src/server.js";

describe("PiGatewayServer lifecycle", () => {
  it("starts draining on SIGTERM and removes signal listeners after stop", async () => {
    const listeners = new Map<string, () => void>();
    const signalSource = {
      on: vi.fn((event: "SIGTERM" | "SIGINT", listener: () => void) => {
        listeners.set(event, listener);
        return signalSource;
      }),
      removeListener: vi.fn((event: "SIGTERM" | "SIGINT") => listeners.delete(event)),
    };
    const server = new PiGatewayServer(
      {
        controlPlane: {
          claim: vi.fn().mockResolvedValue(undefined),
          terminal: vi.fn(),
          heartbeat: vi.fn(),
        },
        capacity: 1,
        worker: vi.fn(),
      },
      signalSource,
    );

    expect(signalSource.on).toHaveBeenCalledTimes(2);
    const stopSpy = vi.spyOn(server.gateway, "stop");
    listeners.get("SIGTERM")?.();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(stopSpy).toHaveBeenCalledTimes(1);
    await server.stop();
    expect(signalSource.removeListener).toHaveBeenCalledTimes(2);
  });
});
