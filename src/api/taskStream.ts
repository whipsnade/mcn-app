interface RawSseEvent {
  id?: string;
  event?: string;
  data: string;
}

export async function parseSseStream(
  body: ReadableStream<Uint8Array>,
  onRawEvent: (event: RawSseEvent) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let current: RawSseEvent = { data: '' };

  const dispatch = () => {
    if (current.data !== '') onRawEvent(current);
    current = { data: '' };
  };
  const consumeLine = (line: string) => {
    if (line === '') {
      dispatch();
      return;
    }
    if (line.startsWith(':')) return;
    const separator = line.indexOf(':');
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '');
    if (field === 'id') current.id = value;
    if (field === 'event') current.event = value;
    if (field === 'data') current.data = current.data ? `${current.data}\n${value}` : value;
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() ?? '';
      lines.forEach(consumeLine);
      if (done) break;
    }
    if (buffer) consumeLine(buffer);
    dispatch();
  } finally {
    reader.releaseLock();
  }
}
