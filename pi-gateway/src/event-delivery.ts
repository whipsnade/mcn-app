import { ControlPlaneBusinessError, ControlPlaneUnavailableError } from "./control-plane-client.js";
import {
  parsePiGatewaySourceEvent,
  parsePiGatewaySourceEventBatch,
  parsePiGatewaySourceEventBatchReceipt,
  piGatewaySourceEventBatchBytes,
  PI_GATEWAY_EVENT_BATCH_MAX_BYTES,
  PI_GATEWAY_EVENT_BATCH_MAX_EVENTS,
  type PiGatewaySourceEvent,
  type PiGatewaySourceEventBatchReceipt,
} from "./protocol.js";

export type EventDeliveryFailureClass =
  | "timeout"
  | "network"
  | "http_5xx"
  | "business_rejection"
  | "protocol";

export type EventDeliveryDiagnosticKind = "failure" | "retry" | "ack" | "overflow";
export type EventDeliveryOperation = "event" | "event_batch" | "heartbeat" | "terminal";

/** Metadata-only event delivery observation; never carries event or user data. */
export interface EventDeliveryDiagnostic {
  operation: EventDeliveryOperation;
  kind: EventDeliveryDiagnosticKind;
  failure_class?: EventDeliveryFailureClass;
  status?: number;
  queue_depth: number;
  queue_high_water: number;
  batch_size: number;
  last_acked_source_sequence: number | null;
  consecutive_failures: number;
  latency_bucket?: "lt_50ms" | "50_250ms" | "250_1000ms" | "gte_1000ms";
}

export class EventDeliveryFailure extends Error {
  readonly code: "control_plane_unreachable" | "event_buffer_overflow";
  readonly failureClass: EventDeliveryFailureClass;
  readonly status?: number;

  constructor(
    code: "control_plane_unreachable" | "event_buffer_overflow",
    failureClass: EventDeliveryFailureClass,
    status?: number,
  ) {
    super(code);
    this.name = "EventDeliveryFailure";
    this.code = code;
    this.failureClass = failureClass;
    if (status !== undefined) this.status = status;
  }
}

export interface EventDeliveryPumpOptions {
  runId: string;
  attemptId: string;
  leaseToken: string;
  sendEventBatch?: (
    runId: string,
    events: readonly PiGatewaySourceEvent[],
    leaseToken: string,
  ) => Promise<unknown>;
  sendEvent?: (
    runId: string,
    event: PiGatewaySourceEvent,
    leaseToken: string,
  ) => Promise<unknown>;
  maxBufferedEvents: number;
  retryBaseMs?: number;
  maxTransientRetries?: number;
  now?: () => number;
  canRetry?: (delayMs: number) => boolean;
  onDiagnostic?: (diagnostic: EventDeliveryDiagnostic) => void;
  onPermanentFailure?: (failure: EventDeliveryFailure) => void;
}

function sourceAttemptId(event: PiGatewaySourceEvent): string {
  return event.source_event_id.slice(0, event.source_event_id.lastIndexOf(":"));
}

function batchBytes(events: readonly PiGatewaySourceEvent[]): number {
  return piGatewaySourceEventBatchBytes(events);
}

export function eventDeliveryLatencyBucket(milliseconds: number): EventDeliveryDiagnostic["latency_bucket"] {
  if (milliseconds < 50) return "lt_50ms";
  if (milliseconds < 250) return "50_250ms";
  if (milliseconds < 1_000) return "250_1000ms";
  return "gte_1000ms";
}

function safeStatus(error: unknown): number | undefined {
  if (!error || typeof error !== "object") return undefined;
  const status = (error as { status?: unknown }).status;
  return typeof status === "number" && Number.isInteger(status) && status >= 100 && status <= 599
    ? status
    : undefined;
}

function classifyFailure(error: unknown): {
  failureClass: EventDeliveryFailureClass;
  status?: number;
  transient: boolean;
} {
  if (error instanceof ControlPlaneUnavailableError) {
    return {
      failureClass: error.failureClass,
      ...(error.status === undefined ? {} : { status: error.status }),
      transient: true,
    };
  }
  if (error instanceof ControlPlaneBusinessError) {
    return { failureClass: "business_rejection", status: error.status, transient: false };
  }
  const code = error && typeof error === "object" && typeof (error as { code?: unknown }).code === "string"
    ? (error as { code: string }).code
    : undefined;
  const message = error instanceof Error ? error.message : "";
  if (code === "pi_gateway_network_error" || code === "control_plane_unreachable") {
    return { failureClass: "network", transient: true };
  }
  if (/timeout/i.test(code ?? "") || /timeout/i.test(message)) {
    return { failureClass: "timeout", transient: true };
  }
  if (/^http_5\d\d$/.test(code ?? "")) {
    const status = Number((code ?? "").slice(5));
    return { failureClass: "http_5xx", status, transient: true };
  }
  return { failureClass: "protocol", status: safeStatus(error), transient: false };
}

export function classifyEventDeliveryFailure(error: unknown): {
  failureClass: EventDeliveryFailureClass;
  status?: number;
} {
  const classified = classifyFailure(error);
  return {
    failureClass: classified.failureClass,
    ...(classified.status === undefined ? {} : { status: classified.status }),
  };
}

export class EventDeliveryPump {
  private readonly runId: string;
  private readonly attemptId: string;
  private readonly leaseToken: string;
  private readonly sendEventBatch?: EventDeliveryPumpOptions["sendEventBatch"];
  private readonly sendEvent?: EventDeliveryPumpOptions["sendEvent"];
  private readonly maxBufferedEvents: number;
  private readonly retryBaseMs: number;
  private readonly maxTransientRetries: number;
  private readonly now: () => number;
  private readonly canRetry: (delayMs: number) => boolean;
  private readonly onDiagnostic: (diagnostic: EventDeliveryDiagnostic) => void;
  private readonly onPermanentFailure: (failure: EventDeliveryFailure) => void;
  private readonly queue: PiGatewaySourceEvent[] = [];
  private runner: Promise<void> | undefined;
  private activeBatch: PiGatewaySourceEvent[] | undefined;
  private retryTimer: ReturnType<typeof setTimeout> | undefined;
  private retryWake: (() => void) | undefined;
  private fatalFailure: EventDeliveryFailure | undefined;
  private stopped = false;
  private expectedSequence: number | undefined;
  private attemptForQueue: string | undefined;
  private queueHighWater = 0;
  private lastAckedSourceSequence: number | null = null;
  private consecutiveFailures = 0;

  constructor(options: EventDeliveryPumpOptions) {
    if (!options.runId || !options.attemptId || !options.leaseToken) {
      throw new Error("pi_gateway_event_delivery_identity_invalid");
    }
    if (!options.sendEventBatch && !options.sendEvent) {
      throw new Error("pi_gateway_event_delivery_transport_missing");
    }
    if (!Number.isInteger(options.maxBufferedEvents) || options.maxBufferedEvents < 1 || options.maxBufferedEvents > 100_000) {
      throw new Error("pi_gateway_event_buffer_limit_invalid");
    }
    this.runId = options.runId;
    this.attemptId = options.attemptId;
    this.leaseToken = options.leaseToken;
    this.sendEventBatch = options.sendEventBatch;
    this.sendEvent = options.sendEvent;
    this.maxBufferedEvents = options.maxBufferedEvents;
    this.retryBaseMs = Math.max(1, Math.min(1_000, options.retryBaseMs ?? 100));
    this.maxTransientRetries = Math.max(0, Math.min(10, options.maxTransientRetries ?? 5));
    this.now = options.now ?? Date.now;
    this.canRetry = options.canRetry ?? (() => true);
    this.onDiagnostic = options.onDiagnostic ?? (() => undefined);
    this.onPermanentFailure = options.onPermanentFailure ?? (() => undefined);
  }

  get queueDepth(): number {
    return this.queue.length;
  }

  get highWater(): number {
    return this.queueHighWater;
  }

  get lastAckedSequence(): number | null {
    return this.lastAckedSourceSequence;
  }

  get retryCount(): number {
    return this.consecutiveFailures;
  }

  enqueue(event: PiGatewaySourceEvent): boolean {
    if (this.stopped || this.fatalFailure) return false;
    let parsed: PiGatewaySourceEvent;
    try {
      parsed = parsePiGatewaySourceEvent(event);
    } catch {
      this.fail(new EventDeliveryFailure("control_plane_unreachable", "protocol"), "failure");
      return false;
    }
    if (sourceAttemptId(parsed) !== this.attemptId) {
      this.fail(new EventDeliveryFailure("control_plane_unreachable", "protocol"), "failure");
      return false;
    }
    if (this.attemptForQueue === undefined) this.attemptForQueue = sourceAttemptId(parsed);
    if (this.attemptForQueue !== sourceAttemptId(parsed)) {
      this.fail(new EventDeliveryFailure("control_plane_unreachable", "protocol"), "failure");
      return false;
    }
    if (this.expectedSequence !== undefined && parsed.sequence !== this.expectedSequence) {
      this.fail(new EventDeliveryFailure("control_plane_unreachable", "protocol"), "failure");
      return false;
    }
    if (this.expectedSequence === undefined) this.expectedSequence = parsed.sequence + 1;
    else this.expectedSequence += 1;
    if (this.queue.length >= this.maxBufferedEvents) {
      this.fail(new EventDeliveryFailure("event_buffer_overflow", "protocol"), "overflow");
      return false;
    }
    this.queue.push(parsed);
    this.queueHighWater = Math.max(this.queueHighWater, this.queue.length);
    this.ensureRunner();
    return true;
  }

  async drain(): Promise<boolean> {
    while (this.queue.length > 0 || this.activeBatch !== undefined || this.runner !== undefined) {
      if (this.fatalFailure || this.stopped) break;
      this.ensureRunner();
      const runner = this.runner;
      if (runner) await runner;
    }
    return !this.fatalFailure && this.queue.length === 0 && this.activeBatch === undefined;
  }

  stop(): void {
    this.stopped = true;
    if (this.retryTimer !== undefined) clearTimeout(this.retryTimer);
    this.retryTimer = undefined;
    this.retryWake?.();
    this.retryWake = undefined;
  }

  private ensureRunner(): void {
    if (this.runner || this.stopped || this.fatalFailure || this.queue.length === 0) return;
    const runner = this.run();
    this.runner = runner;
    void runner.finally(() => {
      if (this.runner === runner) this.runner = undefined;
    });
  }

  private async run(): Promise<void> {
    let transientRetries = 0;
    let startedAt = this.now();
    while (!this.stopped && !this.fatalFailure) {
      try {
        startedAt = this.now();
        if (this.activeBatch === undefined) {
          if (this.queue.length === 0) return;
          this.activeBatch = this.takeBatch();
          transientRetries = 0;
        }
        await this.deliver(this.activeBatch, startedAt);
        this.activeBatch = undefined;
        transientRetries = 0;
        this.consecutiveFailures = 0;
      } catch (error) {
        const classification = classifyFailure(error);
        this.consecutiveFailures += 1;
        this.emitDiagnostic({
          kind: "failure",
          failure_class: classification.failureClass,
          ...(classification.status === undefined ? {} : { status: classification.status }),
          batch_size: this.activeBatch?.length ?? 0,
          latency_bucket: eventDeliveryLatencyBucket(Math.max(0, this.now() - startedAt)),
        });
        const delayMs = Math.min(1_000, this.retryBaseMs * 2 ** transientRetries);
        if (
          !classification.transient ||
          transientRetries >= this.maxTransientRetries ||
          !this.canRetry(delayMs) ||
          this.stopped
        ) {
          this.fail(
            new EventDeliveryFailure("control_plane_unreachable", classification.failureClass, classification.status),
            "failure",
            false,
          );
          return;
        }
        transientRetries += 1;
        this.emitDiagnostic({
          kind: "retry",
          failure_class: classification.failureClass,
          ...(classification.status === undefined ? {} : { status: classification.status }),
          batch_size: this.activeBatch?.length ?? 0,
          latency_bucket: eventDeliveryLatencyBucket(Math.max(0, this.now() - startedAt)),
        });
        await this.wait(delayMs);
      }
    }
  }

  private takeBatch(): PiGatewaySourceEvent[] {
    if (!this.sendEventBatch) return [this.queue.shift() as PiGatewaySourceEvent];
    const maximum = Math.min(this.queue.length, PI_GATEWAY_EVENT_BATCH_MAX_EVENTS);
    let count = 1;
    while (count < maximum && batchBytes(this.queue.slice(0, count + 1)) <= PI_GATEWAY_EVENT_BATCH_MAX_BYTES) count += 1;
    const candidate = this.queue.slice(0, count);
    if (batchBytes(candidate) > PI_GATEWAY_EVENT_BATCH_MAX_BYTES) {
      throw new EventDeliveryFailure("control_plane_unreachable", "protocol");
    }
    this.queue.splice(0, count);
    return candidate;
  }

  private async deliver(batch: PiGatewaySourceEvent[], startedAt: number): Promise<void> {
    const operation = this.sendEventBatch ? "event_batch" : "event";
    if (this.sendEventBatch) {
      const parsedBatch = parsePiGatewaySourceEventBatch({ events: batch });
      const rawReceipt = await this.sendEventBatch(this.runId, parsedBatch.events, this.leaseToken);
      const receipt = parsePiGatewaySourceEventBatchReceipt(rawReceipt);
      this.assertReceipt(batch, receipt);
    } else {
      if (batch.length !== 1 || !this.sendEvent) throw new Error("pi_gateway_event_delivery_protocol");
      await this.sendEvent(this.runId, batch[0], this.leaseToken);
    }
    this.lastAckedSourceSequence = batch[batch.length - 1].sequence;
    this.emitDiagnostic({
      operation,
      kind: "ack",
      batch_size: batch.length,
      latency_bucket: eventDeliveryLatencyBucket(Math.max(0, this.now() - startedAt)),
    });
  }

  private assertReceipt(batch: readonly PiGatewaySourceEvent[], receipt: PiGatewaySourceEventBatchReceipt): void {
    if (receipt.receipts.length !== batch.length) throw new Error("pi_gateway_event_batch_receipt_invalid");
    for (let index = 0; index < batch.length; index += 1) {
      const expected = batch[index];
      const actual = receipt.receipts[index];
      if (actual.source_event_id !== expected.source_event_id || actual.sequence !== expected.sequence) {
        throw new Error("pi_gateway_event_batch_receipt_invalid");
      }
    }
    if (receipt.last_acked_source_sequence !== batch[batch.length - 1].sequence) {
      throw new Error("pi_gateway_event_batch_receipt_invalid");
    }
  }

  private async wait(delayMs: number): Promise<void> {
    if (this.stopped) return;
    await new Promise<void>((resolve) => {
      this.retryWake = resolve;
      this.retryTimer = setTimeout(resolve, delayMs);
    });
    this.retryTimer = undefined;
    this.retryWake = undefined;
  }

  private emitDiagnostic(
    diagnostic: Omit<EventDeliveryDiagnostic, "operation" | "queue_depth" | "queue_high_water" | "last_acked_source_sequence" | "consecutive_failures"> &
      Partial<Pick<EventDeliveryDiagnostic, "operation">>,
  ): void {
    const operation = diagnostic.operation ?? (this.sendEventBatch ? "event_batch" : "event");
    this.onDiagnostic({
      ...diagnostic,
      operation,
      queue_depth: this.queueDepth,
      queue_high_water: this.queueHighWater,
      last_acked_source_sequence: this.lastAckedSourceSequence,
      consecutive_failures: this.consecutiveFailures,
    });
  }

  private fail(
    failure: EventDeliveryFailure,
    kind: EventDeliveryDiagnosticKind,
    emitDiagnostic = true,
  ): void {
    if (this.fatalFailure) return;
    this.fatalFailure = failure;
    if (emitDiagnostic) {
      this.emitDiagnostic({
        operation: this.sendEventBatch ? "event_batch" : "event",
        kind,
        failure_class: failure.failureClass,
        ...(failure.status === undefined ? {} : { status: failure.status }),
        batch_size: this.activeBatch?.length ?? 0,
      });
    }
    this.onPermanentFailure(failure);
  }
}
