import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DEFAULT_LOADING_STAGES, useLoadingMessage } from './useLoadingMessage';

describe('useLoadingMessage', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('advances stages as waiting time grows', () => {
    const { result } = renderHook(() => useLoadingMessage(true));

    expect(result.current).toBe(DEFAULT_LOADING_STAGES[0][1]);

    act(() => {
      vi.advanceTimersByTime(8_500);
    });
    expect(result.current).toBe(DEFAULT_LOADING_STAGES[1][1]);

    act(() => {
      vi.advanceTimersByTime(17_000);
    });
    expect(result.current).toBe(DEFAULT_LOADING_STAGES[2][1]);
  });

  it('resets to the first stage when reactivated and supports custom stages', () => {
    const stages: Array<readonly [number, string]> = [
      [0, '开始'],
      [1000, '继续'],
    ];
    const { result, rerender } = renderHook(
      ({ active }) => useLoadingMessage(active, stages),
      { initialProps: { active: true } },
    );

    act(() => {
      vi.advanceTimersByTime(1_500);
    });
    expect(result.current).toBe('继续');

    rerender({ active: false });
    expect(result.current).toBe('开始');

    rerender({ active: true });
    expect(result.current).toBe('开始');
  });
});
