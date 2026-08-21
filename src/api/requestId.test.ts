import { afterEach, describe, expect, it, vi } from 'vitest';

import { createRequestId } from './requestId';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

describe('createRequestId', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('uses native randomUUID when available', () => {
    vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'native-id') });

    expect(createRequestId()).toBe('native-id');
  });

  it('falls back to getRandomValues outside secure contexts', () => {
    vi.stubGlobal('crypto', {
      getRandomValues: (bytes: Uint8Array) => {
        bytes.fill(0xab);
        return bytes;
      },
    });

    expect(createRequestId()).toMatch(UUID_PATTERN);
  });

  it('still returns a UUID-shaped value when Web Crypto is unavailable', () => {
    vi.stubGlobal('crypto', undefined);

    expect(createRequestId()).toMatch(UUID_PATTERN);
  });
});
