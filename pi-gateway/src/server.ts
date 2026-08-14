import { PiGateway, type PiGatewayOptions } from "./gateway.js";

export interface GatewaySignalSource {
  on(event: "SIGTERM" | "SIGINT", listener: () => void): unknown;
  removeListener?(event: "SIGTERM" | "SIGINT", listener: () => void): unknown;
}

/** Thin lifecycle wrapper used by the executable entrypoint in later tasks. */
export class PiGatewayServer {
  readonly gateway: PiGateway;

  private readonly signalSource: GatewaySignalSource | null;
  private readonly signalHandlers: Array<["SIGTERM" | "SIGINT", () => void]> = [];
  private stopPromise: Promise<void> | undefined;

  constructor(options: PiGatewayOptions, signalSource: GatewaySignalSource | null = process) {
    this.gateway = new PiGateway(options);
    this.signalSource = signalSource;
    if (signalSource) {
      for (const signal of ["SIGTERM", "SIGINT"] as const) {
        const handler = () => { void this.stop(); };
        this.signalHandlers.push([signal, handler]);
        signalSource.on(signal, handler);
      }
    }
  }

  async stop(): Promise<void> {
    if (!this.stopPromise) {
      this.stopPromise = (async () => {
        await this.gateway.stop();
        if (this.signalSource?.removeListener) {
          for (const [signal, handler] of this.signalHandlers) {
            this.signalSource.removeListener(signal, handler);
          }
        }
      })();
    }
    await this.stopPromise;
  }
}
