import { describe, expect, it } from 'vitest';

import {
  initialTaskRuntime,
  isTerminalTaskStatus,
  reduceTaskEvent,
  type TaskEvent,
} from './taskEvents';


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

    expect(toolStarted.activity).toBe('正在查询社媒数据');
    expect(settled.activity).toBe('本次调用积分已结算');
  });

  it('treats partial MCP success as a completed terminal task', () => {
    const state = reduceTaskEvent(initialTaskRuntime('task-1'), {
      id: 3,
      taskId: 'task-1',
      type: 'task.completed_with_warnings',
      payload: { code: 'mcp_partial_failure' },
    });

    expect(state.status).toBe('completed_with_warnings');
    expect(state.connection).toBe('closed');
    expect(state.activity).toBe('分析完成，部分渠道数据暂不可用');
    expect(isTerminalTaskStatus(state.status)).toBe(true);
  });

  it('derives the visible phase from the actual backend event type and progress', () => {
    const state = reduceTaskEvent(initialTaskRuntime('task-1'), {
      id: 1,
      taskId: 'task-1',
      type: 'tool.succeeded',
      payload: {
        platform: '抖音',
        step_index: 2,
        step_total: 3,
        platform_progress: { 抖音: { succeeded: 2, failed: 0, unknown: 0 } },
      },
    });

    expect(state.phase).toBe('mcp_query');
    expect(state.phaseLabel).toBe('社媒数据 MCP 查询');
    expect(state.phaseProgress).toEqual({ current: 2, total: 3 });
    expect(state.platformProgress?.['抖音']?.succeeded).toBe(2);
  });

  it('keeps a safe task error and message id idempotently', () => {
    const state = reduceTaskEvent(initialTaskRuntime('task-1'), {
      id: 2,
      taskId: 'task-1',
      type: 'task.failed',
      payload: { code: 'upstream_error', message: '社媒数据服务暂时不可用，请稍后重试。', message_id: 'error-1' },
    });
    const duplicate = reduceTaskEvent(state, {
      id: 1,
      taskId: 'task-1',
      type: 'task.failed',
      payload: { code: 'upstream_error', message: '重复错误', message_id: 'error-1' },
    });

    expect(state.errorMessage).toBe('社媒数据服务暂时不可用，请稍后重试。');
    expect(state.errorMessageId).toBe('error-1');
    expect(duplicate.errorMessage).toBe(state.errorMessage);
  });

  it('tracks follow-up suggestion lifecycle without exposing event internals', () => {
    const pending = reduceTaskEvent(initialTaskRuntime('task-1'), {
      id: 1,
      taskId: 'task-1',
      type: 'followup.suggestions_started',
      payload: { session_id: 'session-1', task_id: 'task-1' },
    });
    expect(pending.followupStatus).toBe('pending');
    expect(pending.followupSuggestions).toEqual([]);

    const ready = reduceTaskEvent(pending, {
      id: 2,
      taskId: 'task-1',
      type: 'followup.suggestions_updated',
      payload: {
        suggestions: [{ title: '受众拆解', prompt: '请继续分析受众地域', rationale: '定位重点区域' }],
      },
    });
    expect(ready.followupStatus).toBe('completed');
    expect(ready.followupSuggestions).toEqual([{ title: '受众拆解', prompt: '请继续分析受众地域', rationale: '定位重点区域' }]);

    const persistedOnly = reduceTaskEvent(pending, {
      id: 4,
      taskId: 'task-1',
      type: 'followup.suggestions_updated',
      payload: { count: 1 },
    });
    expect(persistedOnly.followupStatus).toBe('pending');

    const failed = reduceTaskEvent(pending, {
      id: 3,
      taskId: 'task-1',
      type: 'followup.suggestions_failed',
      payload: { error_code: 'FOLLOWUP_GENERATION_FAILED' },
    });
    expect(failed.followupStatus).toBe('failed');
    expect(failed.followupError).toBe('进一步分析建议暂时生成失败，请稍后重试。');
  });
});
