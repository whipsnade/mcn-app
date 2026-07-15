export type TaskEventType = string;

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
  errorCode?: string;
  activity?: string;
  connection: TaskConnection;
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
  switch (event.type) {
    case 'task.pending':
      return { ...state, status: String(event.payload.status ?? 'pending') };
    case 'plan.ready':
      return { ...state, status: String(event.payload.status ?? 'running'), activity: '分析计划已确认，正在执行' };
    case 'tool.started':
      return { ...state, activity: '正在获取达人数据' };
    case 'tool.succeeded':
      return { ...state, activity: '达人数据获取完成，正在整理结果' };
    case 'tool.failed':
      return { ...state, activity: '部分数据暂不可用，正在继续分析' };
    case 'tool.unknown':
      return { ...state, activity: '正在确认数据调用结果' };
    case 'points.reserved':
      return { ...state, activity: '已预留本次分析积分' };
    case 'points.settled':
      return { ...state, activity: '本次调用积分已结算' };
    case 'points.released':
      return { ...state, activity: '未使用积分已释放' };
    case 'task.completed':
      return { ...state, status: 'completed', connection: 'closed', activity: '分析完成' };
    case 'task.cancelled':
      return { ...state, status: 'cancelled', connection: 'closed' };
    case 'task.failed':
      return {
        ...state,
        status: 'failed',
        errorCode: String(event.payload.code ?? event.payload.errorCode ?? 'task_failed'),
        connection: 'closed',
        activity: '分析未完成，请稍后重试',
      };
    default:
      return state;
  }
}

export function isTerminalTaskStatus(status: string | undefined): boolean {
  return status === 'completed'
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
        activity: 'BI 报告已更新',
      });
    default:
      return applyStatusAndPointEvent(next, event);
  }
}
