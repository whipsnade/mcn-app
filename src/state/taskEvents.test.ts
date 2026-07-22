import { describe, expect, it } from 'vitest';

import {
  initialTaskRuntime,
  isTerminalTaskStatus,
  reduceTaskEvent,
  type TaskEvent,
} from './taskEvents';


describe('task event reducer', () => {
  it('does not claim an empty follow-up result before any follow-up event arrives', () => {
    expect(initialTaskRuntime('task-1').followupSuggestions).toBeUndefined();
  });

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
        step_total: null,
        platform_progress: { 抖音: { succeeded: 2, failed: 0, unknown: 0 } },
      },
    });

    expect(state.phase).toBe('mcp_query');
    expect(state.phaseLabel).toBe('社媒数据 MCP 查询');
    expect(state.phaseProgress).toEqual({ current: 2 });
    expect(state.platformProgress?.['抖音']?.succeeded).toBe(2);
  });

  it('marks an insufficient balance terminal event with friendly copy', () => {
    const state = reduceTaskEvent(initialTaskRuntime('task-1'), {
      id: 2,
      taskId: 'task-1',
      type: 'task.failed',
      payload: { code: 'insufficient_balance', message_id: 'error-balance-1' },
    });

    expect(state.status).toBe('insufficient_balance');
    expect(state.phaseLabel).toBe('积分不足');
    expect(state.errorMessage).toBe('积分不足，任务已停止。');
    expect(state.errorMessageId).toBe('error-balance-1');
    expect(state.connection).toBe('closed');
    expect(isTerminalTaskStatus(state.status)).toBe(true);
    expect(state.nodes?.at(-1)?.label).toBe('积分不足');
  });

  it('notes the generated report when balance runs out after report.updated', () => {
    const withReport = reduceTaskEvent(initialTaskRuntime('task-1'), {
      id: 1,
      taskId: 'task-1',
      type: 'report.updated',
      payload: { report_id: 'analysis-report-1' },
    });
    const state = reduceTaskEvent(withReport, {
      id: 2,
      taskId: 'task-1',
      type: 'task.failed',
      payload: { code: 'insufficient_balance' },
    });

    expect(state.status).toBe('insufficient_balance');
    expect(state.errorMessage).toBe('积分不足，已基于已采集数据生成报告。');
    expect(state.activity).toBe('积分不足，已基于已采集数据生成报告');
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

  it('exposes an agent analysis report directly', () => {
    const state = reduceTaskEvent(initialTaskRuntime('task-agent'), {
      id: 1,
      taskId: 'task-agent',
      type: 'report.updated',
      payload: { report_id: 'analysis-report-1', phase: 'ai_summary', label: '分析报告已生成' },
    });

    expect(state.visibleAnalysisReportId).toBe('analysis-report-1');
    expect(state.phase).toBe('ai_summary');
    expect(state.phaseLabel).toBe('分析报告已生成');
  });

  it('accepts the camelCase report id on report.updated', () => {
    const state = reduceTaskEvent(initialTaskRuntime('task-agent'), {
      id: 1,
      taskId: 'task-agent',
      type: 'report.updated',
      payload: { reportId: 'analysis-report-2' },
    });

    expect(state.visibleAnalysisReportId).toBe('analysis-report-2');
  });

  it('clears the visible analysis report when a new task starts', () => {
    const withReport = reduceTaskEvent(initialTaskRuntime('task-agent'), {
      id: 1,
      taskId: 'task-agent',
      type: 'report.updated',
      payload: { report_id: 'analysis-report-1' },
    });
    const restarted = reduceTaskEvent(withReport, {
      id: 2,
      taskId: 'task-agent',
      type: 'task.pending',
      payload: { status: 'pending' },
    });

    expect(withReport.visibleAnalysisReportId).toBe('analysis-report-1');
    expect(restarted.visibleAnalysisReportId).toBeUndefined();
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

describe('task flow nodes', () => {
  it('builds a live node list from the task lifecycle and resets per task', () => {
    let state = initialTaskRuntime('task-1');
    state = reduceTaskEvent(state, { id: 1, taskId: 'task-1', type: 'task.pending', payload: { status: 'pending' } });
    state = reduceTaskEvent(state, { id: 2, taskId: 'task-1', type: 'plan.ready', payload: {} });
    state = reduceTaskEvent(state, { id: 3, taskId: 'task-1', type: 'tool.started', payload: { platform: '小红书', step_index: 1 } });
    state = reduceTaskEvent(state, { id: 4, taskId: 'task-1', type: 'tool.failed', payload: { platform: '小红书', step_index: 1, message: '社媒数据服务返回错误，请稍后重试。' } });
    state = reduceTaskEvent(state, { id: 5, taskId: 'task-1', type: 'report.updated', payload: { report_id: 'r-1' } });
    state = reduceTaskEvent(state, { id: 6, taskId: 'task-1', type: 'task.completed', payload: {} });

    expect(state.nodes?.map(node => [node.label, node.status])).toEqual([
      ['任务已受理', 'succeeded'],
      ['分析规划完成', 'succeeded'],
      ['查询小红书数据', 'failed'],
      ['分析报告已生成', 'succeeded'],
      ['分析完成', 'succeeded'],
    ]);
    expect(state.nodes?.[2].detail).toBe('社媒数据服务返回错误，请稍后重试。');

    // 新任务（新的 runtime）从空节点重新开始。
    const next = reduceTaskEvent(initialTaskRuntime('task-2'), { id: 1, taskId: 'task-2', type: 'task.pending', payload: {} });
    expect(next.nodes).toHaveLength(1);
  });

  it('matches batch-restarted step indexes to the latest running tool node', () => {
    let state = initialTaskRuntime('task-1');
    state = reduceTaskEvent(state, { id: 1, taskId: 'task-1', type: 'tool.started', payload: { platform: '小红书', step_index: 1 } });
    state = reduceTaskEvent(state, { id: 2, taskId: 'task-1', type: 'tool.succeeded', payload: { platform: '小红书', step_index: 1 } });
    // 第二批 step_index 又从 1 开始。
    state = reduceTaskEvent(state, { id: 3, taskId: 'task-1', type: 'tool.started', payload: { platform: '抖音', step_index: 1 } });
    state = reduceTaskEvent(state, { id: 4, taskId: 'task-1', type: 'tool.unknown', payload: { platform: '抖音', step_index: 1, message: '响应未确认' } });

    const tools = state.nodes?.filter(node => node.id.startsWith('tool-'));
    expect(tools?.map(node => [node.label, node.status])).toEqual([
      ['查询小红书数据', 'succeeded'],
      ['查询抖音数据', 'unknown'],
    ]);
    expect(tools?.[1].detail).toBe('响应未确认');
  });
});
