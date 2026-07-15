import { describe, expect, it } from 'vitest';

import { initialTaskRuntime, reduceTaskEvent, type TaskEvent } from './taskEvents';


describe('task event reducer', () => {
  it('applies a duplicate event id exactly once', () => {
    const event: TaskEvent = {
      id: 41,
      taskId: 'task-1',
      type: 'message.delta',
      payload: { text: '推荐' },
    };

    const once = reduceTaskEvent(initialTaskRuntime('task-1'), event);
    const twice = reduceTaskEvent(once, event);

    expect(twice.assistantDraft).toBe('推荐');
    expect(twice.lastEventId).toBe(41);
  });

  it('shows BI only after its candidate version is present', () => {
    const early = reduceTaskEvent(initialTaskRuntime('task-1'), {
      id: 1,
      taskId: 'task-1',
      type: 'bi.updated',
      payload: { reportId: 'report-2', candidateVersion: 2 },
    });

    expect(early.visibleReportId).toBeUndefined();

    const ready = reduceTaskEvent(early, {
      id: 2,
      taskId: 'task-1',
      type: 'candidates.updated',
      payload: { version: 2 },
    });

    expect(ready.visibleReportId).toBe('report-2');
  });

  it('ignores a late event from a previously selected task', () => {
    const state = reduceTaskEvent(initialTaskRuntime('task-current'), {
      id: 9,
      taskId: 'task-previous',
      type: 'task.failed',
      payload: { errorCode: 'old_task_failure' },
    });

    expect(state.lastEventId).toBe(0);
    expect(state.status).toBeUndefined();
  });

  it('uses the persisted server delta field for streamed summary text', () => {
    const state = reduceTaskEvent(initialTaskRuntime('task-1'), {
      id: 3,
      taskId: 'task-1',
      type: 'message.delta',
      payload: { delta: '可优先联系' },
    });

    expect(state.assistantDraft).toBe('可优先联系');
  });

  it('hides the previous BI when candidates advance to a newer version', () => {
    const withReport = reduceTaskEvent(
      reduceTaskEvent(initialTaskRuntime('task-1'), {
        id: 1, taskId: 'task-1', type: 'candidates.updated', payload: { version: 1 },
      }),
      {
        id: 2, taskId: 'task-1', type: 'bi.updated',
        payload: { reportId: 'report-1', candidateVersion: 1 },
      },
    );
    const nextCandidates = reduceTaskEvent(withReport, {
      id: 3, taskId: 'task-1', type: 'candidates.updated', payload: { version: 2 },
    });

    expect(withReport.visibleReportId).toBe('report-1');
    expect(nextCandidates.visibleReportId).toBeUndefined();
    expect(nextCandidates.pendingReport).toBeUndefined();
  });

  it('exposes a reviewed Chinese progress label without retaining tool payloads', () => {
    const toolStarted = reduceTaskEvent(initialTaskRuntime('task-1'), {
      id: 1, taskId: 'task-1', type: 'tool.started', payload: { tool_name: 'get_kol_list', endpoint: '/secret' },
    });
    const settled = reduceTaskEvent(toolStarted, {
      id: 2, taskId: 'task-1', type: 'points.settled', payload: { raw_response: 'do-not-show' },
    });

    expect(toolStarted.activity).toBe('正在获取达人数据');
    expect(settled.activity).toBe('本次调用积分已结算');
  });
});
