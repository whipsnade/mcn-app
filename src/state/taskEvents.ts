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
      return { ...state, status: String(event.payload.status ?? 'running') };
    case 'task.completed':
      return { ...state, status: 'completed', connection: 'closed' };
    case 'task.cancelled':
      return { ...state, status: 'cancelled', connection: 'closed' };
    case 'task.failed':
      return {
        ...state,
        status: 'failed',
        errorCode: String(event.payload.code ?? event.payload.errorCode ?? 'task_failed'),
        connection: 'closed',
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
        });
      }
    case 'bi.updated':
      return exposeMatchingReport({
        ...next,
        pendingReport: {
          id: String(valueOf(event.payload, 'reportId', 'report_id')),
          candidateVersion: Number(valueOf(event.payload, 'candidateVersion', 'candidate_version')),
        },
      });
    default:
      return applyStatusAndPointEvent(next, event);
  }
}
