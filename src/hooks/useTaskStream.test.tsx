import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import * as tasksApi from '../api/tasks';
import { installFetchSse } from '../test/fakeSse';
import { useTaskStream } from './useTaskStream';


describe('useTaskStream', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('reconnects from the last id without cancelling the task', async () => {
    const fake = installFetchSse();
    const cancel = vi.spyOn(tasksApi, 'cancelTask');
    const { unmount } = renderHook(() => useTaskStream('task-1'));

    await fake.waitForConnection();
    fake.emit({ id: 17, taskId: 'task-1', type: 'tool.started', payload: {} });
    fake.disconnect();
    await fake.waitForReconnect();

    expect(fake.lastRequestHeaders().get('Last-Event-ID')).toBe('17');
    expect(cancel).not.toHaveBeenCalled();
    unmount();
    fake.restore();
  });

  it('stops streaming when the task reaches a terminal state', async () => {
    const fake = installFetchSse();
    renderHook(() => useTaskStream('task-1'));
    await fake.waitForConnection();
    fake.emit({ id: 1, taskId: 'task-1', type: 'task.completed', payload: {} });
    fake.disconnect();

    await waitFor(() => expect(fake.connectionCount()).toBe(1));
    fake.restore();
  });
});
