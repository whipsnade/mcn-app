export type TaskEventType = string;
export type TaskPhase = 'accepting_data' | 'ai_analysis' | 'replanning' | 'mcp_query' | 'ai_summary' | 'completed' | 'completed_with_warnings' | 'failed' | 'cancelled';

export interface PlatformProgress {
  succeeded: number;
  failed: number;
  unknown: number;
}

export interface TaskEvent {
  id: number;
  taskId: string;
  type: TaskEventType;
  payload: Record<string, unknown>;
}

export type TaskConnection = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'closed' | 'error';

export interface TaskRuntimeState {
  taskId: string;
  lastEventId: number;
  assistantDraft: string;
  candidateVersion?: number;
  pendingReport?: { id: string; candidateVersion: number };
  visibleReportId?: string;
  status?: string;
  phase?: TaskPhase;
  phaseLabel?: string;
  phaseProgress?: { current: number; total: number };
  platformProgress?: Record<string, PlatformProgress>;
  errorCode?: string;
  errorMessage?: string;
  errorMessageId?: string;
  activity?: string;
  connection: TaskConnection;
  followupStatus?: 'pending' | 'completed' | 'failed';
  followupSuggestions?: Array<{ title: string; prompt: string; rationale: string }>;
  followupError?: string;
}

export function initialTaskRuntime(taskId: string): TaskRuntimeState {
  return {
    taskId,
    lastEventId: 0,
    assistantDraft: '',
    connection: 'idle',
  };
}

function valueOf(payload: Record<string, unknown>, camelName: string, snakeName: string): unknown {
  return payload[camelName] ?? payload[snakeName];
}

function exposeMatchingReport(state: TaskRuntimeState): TaskRuntimeState {
  if (state.pendingReport?.candidateVersion === state.candidateVersion) {
    return { ...state, visibleReportId: state.pendingReport.id };
  }
  return state;
}

function applyStatusAndPointEvent(state: TaskRuntimeState, event: TaskEvent): TaskRuntimeState {
  const progressFrom = () => {
    const current = Number(valueOf(event.payload, 'stepIndex', 'step_index'));
    const total = Number(valueOf(event.payload, 'stepTotal', 'step_total'));
    return Number.isFinite(current) && Number.isFinite(total)
      ? { current, total }
      : state.phaseProgress;
  };
  const platformProgress = event.payload.platform_progress;
  switch (event.type) {
    case 'task.pending':
      return { ...state, status: String(event.payload.status ?? 'pending'), phase: 'accepting_data', phaseLabel: String(event.payload.label ?? '接受数据'), activity: String(event.payload.label ?? '接受数据') };
    case 'plan.ready':
      return { ...state, status: String(event.payload.status ?? 'running'), phase: 'ai_analysis', phaseLabel: String(event.payload.label ?? 'AI 分析'), activity: String(event.payload.label ?? 'AI 分析') };
    case 'replan.ready':
      return { ...state, phase: 'replanning', phaseLabel: String(event.payload.label ?? '重新规划'), activity: String(event.payload.label ?? '重新规划') };
    case 'tool.started':
      return { ...state, phase: 'mcp_query', phaseLabel: '社媒数据 MCP 查询', phaseProgress: progressFrom(), activity: `正在查询${String(event.payload.platform ?? '社媒')}数据` };
    case 'tool.succeeded':
      return { ...state, phase: 'mcp_query', phaseLabel: '社媒数据 MCP 查询', phaseProgress: progressFrom(), platformProgress: typeof platformProgress === 'object' && platformProgress !== null ? platformProgress as Record<string, PlatformProgress> : state.platformProgress, activity: `${String(event.payload.platform ?? '社媒')}数据查询完成` };
    case 'tool.failed':
      return { ...state, phase: 'mcp_query', phaseLabel: '社媒数据 MCP 查询', phaseProgress: progressFrom(), platformProgress: typeof platformProgress === 'object' && platformProgress !== null ? platformProgress as Record<string, PlatformProgress> : state.platformProgress, errorCode: String(event.payload.code ?? event.payload.errorCode ?? event.payload.error_code ?? 'mcp_call_failed'), errorMessage: String(event.payload.message ?? '社媒数据查询失败，请稍后重试。'), errorMessageId: String(event.payload.messageId ?? event.payload.message_id ?? ''), activity: `${String(event.payload.platform ?? '社媒')}数据查询失败` };
    case 'tool.unknown':
      return { ...state, phase: 'mcp_query', phaseLabel: '社媒数据 MCP 查询', phaseProgress: progressFrom(), platformProgress: typeof platformProgress === 'object' && platformProgress !== null ? platformProgress as Record<string, PlatformProgress> : state.platformProgress, errorCode: String(event.payload.code ?? event.payload.errorCode ?? event.payload.error_code ?? 'mcp_unknown_outcome'), errorMessage: String(event.payload.message ?? '社媒数据服务响应未确认，请稍后重试。'), errorMessageId: String(event.payload.messageId ?? event.payload.message_id ?? ''), activity: `${String(event.payload.platform ?? '社媒')}数据响应未确认` };
    case 'points.reserved':
      return { ...state, activity: '已预留本次分析积分' };
    case 'points.settled':
      return { ...state, activity: '本次调用积分已结算' };
    case 'points.released':
      return { ...state, activity: '未使用积分已释放' };
    case 'task.completed':
      return { ...state, status: 'completed', phase: 'completed', phaseLabel: '分析完成', connection: 'closed', activity: '分析完成' };
    case 'task.completed_with_warnings':
      return {
        ...state,
        status: 'completed_with_warnings',
        phase: 'completed_with_warnings',
        phaseLabel: '分析完成（部分渠道不可用）',
        errorCode: String(event.payload.code ?? 'mcp_partial_failure'),
        errorMessage: String(event.payload.message ?? '部分社媒渠道查询失败，已保留可用结果。'),
        errorMessageId: String(event.payload.messageId ?? event.payload.message_id ?? ''),
        connection: 'closed',
        activity: '分析完成，部分渠道数据暂不可用',
      };
    case 'task.cancelled':
      return { ...state, status: 'cancelled', connection: 'closed' };
    case 'task.failed':
      return {
        ...state,
        status: 'failed',
        phase: 'failed',
        phaseLabel: '分析失败',
        errorCode: String(event.payload.code ?? event.payload.errorCode ?? 'task_failed'),
        errorMessage: String(event.payload.message ?? '分析任务执行失败，请稍后重试。'),
        errorMessageId: String(event.payload.messageId ?? event.payload.message_id ?? ''),
        connection: 'closed',
        activity: '分析未完成，请稍后重试',
      };
    case 'followup.suggestions_started':
      return {
        ...state,
        followupStatus: 'pending',
        followupSuggestions: [],
        followupError: undefined,
        activity: '正在生成进一步分析建议',
      };
    case 'followup.suggestions_updated':
      {
        const hasSuggestionsPayload = Array.isArray(event.payload.suggestions);
        const suggestionsPayload = hasSuggestionsPayload ? event.payload.suggestions as unknown[] : [];
        return {
          ...state,
          // Production SSE carries only a count; keep polling until the
          // persisted task metadata with the actual suggestions is recovered.
          followupStatus: hasSuggestionsPayload ? 'completed' : state.followupStatus ?? 'pending',
          followupSuggestions: hasSuggestionsPayload
            ? suggestionsPayload.filter(item => typeof item === 'object' && item !== null).map(item => {
              const value = item as Record<string, unknown>;
              return {
                title: String(value.title ?? ''),
                prompt: String(value.prompt ?? ''),
                rationale: String(value.rationale ?? ''),
              };
            }).filter(item => item.title && item.prompt)
            : state.followupSuggestions ?? [],
          followupError: undefined,
          activity: '进一步分析建议已生成',
        };
      }
    case 'followup.suggestions_failed':
      return {
        ...state,
        followupStatus: 'failed',
        followupSuggestions: [],
        followupError: '进一步分析建议暂时生成失败，请稍后重试。',
        activity: '进一步分析建议生成失败',
      };
    default:
      return state;
  }
}

export function isTerminalTaskStatus(status: string | undefined): boolean {
  return status === 'completed'
    || status === 'completed_with_warnings'
    || status === 'failed'
    || status === 'insufficient_balance'
    || status === 'cancelled';
}

export function reduceTaskEvent(state: TaskRuntimeState, event: TaskEvent): TaskRuntimeState {
  if (event.taskId !== state.taskId || event.id <= state.lastEventId) return state;
  const next = { ...state, lastEventId: event.id };
  switch (event.type) {
    case 'message.delta':
      return { ...next, assistantDraft: next.assistantDraft + String(event.payload.text ?? event.payload.delta ?? '') };
    case 'message.completed':
      return { ...next, assistantDraft: String(event.payload.text ?? next.assistantDraft) };
    case 'candidates.updated':
      {
        const candidateVersion = Number(valueOf(event.payload, 'version', 'candidate_version'));
        const pendingReport = next.pendingReport?.candidateVersion === candidateVersion
          ? next.pendingReport
          : undefined;
        return exposeMatchingReport({
          ...next,
          candidateVersion,
          pendingReport,
          visibleReportId: undefined,
          phase: 'ai_summary',
          phaseLabel: String(event.payload.label ?? 'AI 汇总'),
          activity: '候选清单已更新',
        });
      }
    case 'bi.updated':
      return exposeMatchingReport({
        ...next,
        pendingReport: {
          id: String(valueOf(event.payload, 'reportId', 'report_id')),
          candidateVersion: Number(valueOf(event.payload, 'candidateVersion', 'candidate_version')),
        },
        phase: 'ai_summary',
        phaseLabel: String(event.payload.label ?? 'BI 报告已生成'),
        activity: 'BI 报告已更新',
      });
    default:
      return applyStatusAndPointEvent(next, event);
  }
}
