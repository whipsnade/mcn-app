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

export type TaskFlowNodeStatus = 'running' | 'succeeded' | 'failed' | 'unknown';

export interface TaskFlowNode {
  id: string;
  label: string;
  status: TaskFlowNodeStatus;
  /** 失败/未确认节点的用户可读原因（报错信息）。 */
  detail?: string;
}

export interface TaskRuntimeState {
  taskId: string;
  lastEventId: number;
  assistantDraft: string;
  /** 执行流程节点；测试夹具可省略，reducer 按空数组处理。 */
  nodes?: TaskFlowNode[];
  candidateVersion?: number;
  pendingReport?: { id: string; candidateVersion: number };
  visibleReportId?: string;
  visibleAnalysisReportId?: string;
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
    nodes: [],
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

function pushNode(nodes: TaskFlowNode[], node: TaskFlowNode): TaskFlowNode[] {
  return [...nodes, node];
}

function updateNode(
  nodes: TaskFlowNode[],
  index: number,
  patch: Partial<TaskFlowNode>,
): TaskFlowNode[] {
  if (index < 0) return nodes;
  const copy = [...nodes];
  copy[index] = { ...copy[index], ...patch };
  return copy;
}

function latestRunningToolNode(nodes: TaskFlowNode[], stepIndex: number): number {
  // pipeline 分批执行时 step_index 每批从 1 重新计数：优先匹配最近一个
  // 仍处于 running 的同类节点，避免误更新到上一批的同名节点。
  for (let index = nodes.length - 1; index >= 0; index -= 1) {
    const node = nodes[index];
    if (
      node.status === 'running'
      && (node.id === `tool-${stepIndex}` || node.id.startsWith(`tool-${stepIndex}-`))
    ) {
      return index;
    }
  }
  return -1;
}

function withFlowNode(state: TaskRuntimeState, event: TaskEvent): TaskRuntimeState {
  const nodes = state.nodes ?? [];
  const stepIndex = Number(valueOf(event.payload, 'stepIndex', 'step_index'));
  const platform = String(event.payload.platform ?? '社媒');
  switch (event.type) {
    case 'task.pending':
      return { ...state, nodes: [{ id: 'accepted', label: '任务已受理', status: 'succeeded' }] };
    case 'plan.ready':
      return { ...state, nodes: pushNode(nodes, { id: 'plan', label: '分析规划完成', status: 'succeeded' }) };
    case 'replan.ready':
      return { ...state, nodes: pushNode(nodes, { id: `replan-${event.id}`, label: '自动重新规划', status: 'succeeded' }) };
    case 'tool.started':
      {
        const base = `tool-${stepIndex}`;
        const id = nodes.some(node => node.id === base) ? `${base}-${event.id}` : base;
        return { ...state, nodes: pushNode(nodes, { id, label: `查询${platform}数据`, status: 'running' }) };
      }
    case 'tool.succeeded':
    case 'tool.failed':
    case 'tool.unknown':
      {
        const status: TaskFlowNodeStatus = event.type === 'tool.succeeded'
          ? 'succeeded'
          : event.type === 'tool.failed'
            ? 'failed'
            : 'unknown';
        const detail = status === 'succeeded' ? undefined : String(event.payload.message ?? '') || undefined;
        const index = latestRunningToolNode(nodes, stepIndex);
        if (index === -1) {
          return { ...state, nodes: pushNode(nodes, { id: `tool-${stepIndex}-late-${event.id}`, label: `查询${platform}数据`, status, detail }) };
        }
        return { ...state, nodes: updateNode(nodes, index, { status, detail }) };
      }
    case 'candidates.updated':
      return { ...state, nodes: pushNode(nodes, { id: 'candidates', label: '候选清单已生成', status: 'succeeded' }) };
    case 'bi.updated':
      return { ...state, nodes: pushNode(nodes, { id: 'bi', label: 'BI 报告已生成', status: 'succeeded' }) };
    case 'report.updated':
      return { ...state, nodes: pushNode(nodes, { id: 'report', label: '分析报告已生成', status: 'succeeded' }) };
    case 'task.completed':
      return { ...state, nodes: pushNode(nodes, { id: 'terminal', label: '分析完成', status: 'succeeded' }) };
    case 'task.completed_with_warnings':
      return { ...state, nodes: pushNode(nodes, { id: 'terminal', label: '分析完成（部分渠道失败）', status: 'unknown', detail: String(event.payload.message ?? '') || undefined }) };
    case 'task.failed':
      return { ...state, nodes: pushNode(nodes, { id: 'terminal', label: '任务失败', status: 'failed', detail: String(event.payload.message ?? '') || undefined }) };
    case 'task.cancelled':
      return { ...state, nodes: pushNode(nodes, { id: 'terminal', label: '任务已取消', status: 'unknown' }) };
    default:
      return state;
  }
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
      return { ...state, status: String(event.payload.status ?? 'pending'), phase: 'accepting_data', phaseLabel: String(event.payload.label ?? '接受数据'), activity: String(event.payload.label ?? '接受数据'), visibleAnalysisReportId: undefined };
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
        return withFlowNode(exposeMatchingReport({
          ...next,
          candidateVersion,
          pendingReport,
          visibleReportId: undefined,
          phase: 'ai_summary',
          phaseLabel: String(event.payload.label ?? 'AI 汇总'),
          activity: '候选清单已更新',
        }), event);
      }
    case 'bi.updated':
      {
        const candidateVersion = Number(valueOf(event.payload, 'candidateVersion', 'candidate_version'));
        const scope = String(event.payload.analysis_scope ?? '');
        const isEmptyBrandReport = candidateVersion === 0 && (scope === 'brand' || scope === 'hybrid');
        return withFlowNode(exposeMatchingReport({
          ...next,
          candidateVersion: isEmptyBrandReport ? candidateVersion : next.candidateVersion,
          pendingReport: {
            id: String(valueOf(event.payload, 'reportId', 'report_id')),
            candidateVersion,
          },
          phase: 'ai_summary',
          phaseLabel: String(event.payload.label ?? 'BI 报告已生成'),
          activity: 'BI 报告已更新',
        }), event);
      }
    case 'report.updated':
      {
        // agent 任务没有候选版本概念，报告 id 直接对外可见，无需版本匹配。
        const reportId = valueOf(event.payload, 'reportId', 'report_id');
        if (reportId === undefined || reportId === null) return next;
        return withFlowNode({
          ...next,
          visibleAnalysisReportId: String(reportId),
          phase: 'ai_summary',
          phaseLabel: String(event.payload.label ?? '分析报告已生成'),
          activity: '分析报告已更新',
        }, event);
      }
    default:
      return withFlowNode(applyStatusAndPointEvent(next, event), event);
  }
}
