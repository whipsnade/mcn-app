interface FakeSseEvent {
  id: number;
  taskId: string;
  type: string;
  payload: Record<string, unknown>;
}

interface ActiveConnection {
  controller: ReadableStreamDefaultController<Uint8Array>;
  request: RequestInit;
}

export function installFetchSse() {
  const originalFetch = globalThis.fetch;
  const encoder = new TextEncoder();
  const connections: ActiveConnection[] = [];
  let nextConnection: (() => void) | undefined;

  globalThis.fetch = async (_path, init = {}) => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({
      start(next) {
        controller = next;
      },
    });
    connections.push({ controller, request: init });
    nextConnection?.();
    nextConnection = undefined;
    return new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });
  };

  const waitForCount = (expected: number) => new Promise<void>(resolve => {
    if (connections.length >= expected) {
      resolve();
      return;
    }
    nextConnection = resolve;
  });

  return {
    async waitForConnection() {
      await waitForCount(1);
    },
    async waitForReconnect() {
      await waitForCount(2);
    },
    emit(event: FakeSseEvent) {
      const active = connections.at(-1);
      if (!active) throw new Error('SSE_NOT_CONNECTED');
      active.controller.enqueue(encoder.encode(
        `id: ${event.id}\nevent: ${event.type}\ndata: ${JSON.stringify(event.payload)}\n\n`,
      ));
    },
    disconnect() {
      connections.at(-1)?.controller.close();
    },
    lastRequestHeaders() {
      return new Headers(connections.at(-1)?.request.headers);
    },
    lastRequestSignal() {
      const signal = connections.at(-1)?.request.signal;
      if (!signal) throw new Error('SSE_SIGNAL_NOT_FOUND');
      return signal;
    },
    connectionCount() {
      return connections.length;
    },
    restore() {
      globalThis.fetch = originalFetch;
    },
  };
}
