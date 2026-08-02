/** Agent Run SSE 事件归并 reducer（design §15.3，Agent-Runtime 版 taskEvents）。
 *
 * 每条事件以 per-run ``sequence``（SSE ``id`` 字段）为序，reducer 按
 * ``(run_id, sequence)`` 幂等：重放或乱序的旧事件直接忽略，不会重复记账。
 * 覆盖 run/thinking/tool/artifact/review/message 全部事件类型；终态
 * ``run.completed/failed/cancelled`` 到达后上层据此收流。
 */

export type RunEventType = string;

export type RunConnection =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'closed'
  | 'error';

export type RunStatus =
  | 'queued'
  | 'running'
  | 'reviewing'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface RunEvent {
  /** per-run 递增 sequence（SSE ``id`` 字段）。 */
  id: number;
  runId: string;
  type: RunEventType;
  payload: Record<string, unknown>;
}

export type RunStepStatus = 'running' | 'succeeded' | 'failed' | 'unknown';

export interface RunStep {
  id: string;
  label: string;
  status: RunStepStatus;
  detail?: string;
}

export interface RunToolCall {
  id: string;
  name: string;
  status: 'running' | 'succeeded' | 'failed' | 'unknown';
  detail?: string;
}

export interface RunArtifactDraft {
  artifactId: string;
  module: string;
  version: number;
  status: string;
  title?: string;
  parentArtifactId?: string;
}

export type RunReviewStatus =
  | 'running'
  | 'revision_requested'
  | 'approved'
  | 'rejected';

export interface RunReview {
  batchId?: string;
  artifactIds: string[];
  status: RunReviewStatus;
  revisions: number;
}

export interface RunRuntimeState {
  runId: string;
  lastEventId: number;
  connection: RunConnection;
  status?: RunStatus;
  /** 执行流程节点（受理/工具/审核/发布/终态）。 */
  steps: RunStep[];
  /** 工具调用明细（无分母：agent 循环没有固定调用上限）。 */
  toolCalls: RunToolCall[];
  /** 思考文本；仅在收到 thinking.* 事件后累积，无思考事件时保持空串。 */
  thinking: string;
  /** 是否收到过 thinking.* 事件；false 时 UI 不渲染可展开思考区。 */
  hasThinking: boolean;
  thinkingStatus?: 'running' | 'completed' | 'interrupted';
  drafts: RunArtifactDraft[];
  review?: RunReview;
  messageCompleted: boolean;
  errorCode?: string;
  errorMessage?: string;
  activity?: string;
}

export function initialRunRuntime(runId: string): RunRuntimeState {
  return {
    runId,
    lastEventId: 0,
    connection: 'idle',
    steps: [],
    toolCalls: [],
    thinking: '',
    hasThinking: false,
    drafts: [],
    messageCompleted: false,
  };
}

export function isTerminalRunStatus(status: string | undefined): boolean {
  return status === 'completed' || status === 'failed' || status === 'cancelled';
}

function valueOf(payload: Record<string, unknown>, camelName: string, snakeName: string): unknown {
  return payload[camelName] ?? payload[snakeName];
}

function pushStep(steps: RunStep[], step: RunStep): RunStep[] {
  return [...steps, step];
}

function updateStepByPrefix(
  steps: RunStep[],
  prefix: string,
  patch: Partial<RunStep>,
): RunStep[] {
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    if (steps[index].id.startsWith(prefix) && steps[index].status === 'running') {
      const copy = [...steps];
      copy[index] = { ...copy[index], ...patch };
      return copy;
    }
  }
  return steps;
}

function withRunStatus(state: RunRuntimeState, event: RunEvent): RunRuntimeState {
  switch (event.type) {
    case 'run.started':
      return { ...state, status: 'running', activity: '开始执行' };
    case 'run.paused':
      return { ...state, status: 'paused', activity: '已暂停，可恢复执行' };
    case 'run.resumed':
      return { ...state, status: 'running', activity: '恢复执行' };
    case 'run.completed':
      return { ...state, status: 'completed', connection: 'closed', activity: '分析完成' };
    case 'run.failed':
      return {
        ...state,
        status: 'failed',
        connection: 'closed',
        errorCode: String(event.payload.error_code ?? event.payload.errorCode ?? 'run_failed'),
        errorMessage: String(event.payload.message ?? '运行失败，请稍后重试。'),
        activity: '运行失败',
      };
    case 'run.cancelled':
      return { ...state, status: 'cancelled', connection: 'closed', activity: '已取消' };
    default:
      return state;
  }
}

function withThinking(state: RunRuntimeState, event: RunEvent): RunRuntimeState {
  switch (event.type) {
    case 'thinking.started':
      return { ...state, hasThinking: true, thinkingStatus: 'running', status: state.status ?? 'running' };
    case 'thinking.delta':
      return {
        ...state,
        hasThinking: true,
        thinking: state.thinking + String(event.payload.text ?? event.payload.delta ?? ''),
        status: state.status ?? 'running',
      };
    case 'thinking.completed':
      return { ...state, hasThinking: true, thinkingStatus: 'completed' };
    case 'thinking.failed':
      return { ...state, hasThinking: true, thinkingStatus: 'interrupted' };
    default:
      return state;
  }
}

function toolNameOf(payload: Record<string, unknown>): string {
  return String(valueOf(payload, 'toolName', 'internal_tool_name') ?? '工具调用');
}

function withToolCall(state: RunRuntimeState, event: RunEvent): RunRuntimeState {
  switch (event.type) {
    case 'tool.started':
      return {
        ...state,
        status: state.status ?? 'running',
        toolCalls: [...state.toolCalls, {
          id: `tool-${event.id}`,
          name: toolNameOf(event.payload),
          status: 'running',
        }],
      };
    case 'tool.succeeded':
    case 'tool.failed':
    case 'tool.unknown':
      {
        const status: RunToolCall['status'] = event.type === 'tool.succeeded'
          ? 'succeeded'
          : event.type === 'tool.failed'
            ? 'failed'
            : 'unknown';
        const index = state.toolCalls.findIndex(call => call.status === 'running');
        if (index === -1) return state;
        const toolCalls = [...state.toolCalls];
        toolCalls[index] = {
          ...toolCalls[index],
          status,
          detail: String(event.payload.error_type ?? event.payload.message ?? '') || undefined,
        };
        return { ...state, toolCalls };
      }
    default:
      return state;
  }
}

function withDrafts(state: RunRuntimeState, event: RunEvent): RunRuntimeState {
  if (event.type === 'artifact.draft.created' || event.type === 'artifact.draft.updated') {
    const artifactId = valueOf(event.payload, 'artifactId', 'artifact_id');
    if (artifactId === undefined || artifactId === null) return state;
    const version = Number(valueOf(event.payload, 'version', 'version') ?? 0);
    const parentValue = valueOf(event.payload, 'parentArtifactId', 'parent_artifact_id');
    const draft: RunArtifactDraft = {
      artifactId: String(artifactId),
      module: String(valueOf(event.payload, 'module', 'module') ?? ''),
      version,
      status: String(event.payload.status ?? 'draft'),
      title: event.payload.title !== undefined ? String(event.payload.title) : undefined,
      parentArtifactId: parentValue != null ? String(parentValue) : undefined,
    };
    const index = state.drafts.findIndex(item => item.artifactId === draft.artifactId);
    if (index === -1) return { ...state, drafts: [...state.drafts, draft] };
    const drafts = [...state.drafts];
    drafts[index] = { ...drafts[index], ...draft, version: Math.max(drafts[index].version, version) };
    return { ...state, drafts };
  }
  if (event.type === 'artifact.published') {
    const artifactId = valueOf(event.payload, 'artifactId', 'artifact_id');
    const drafts = state.drafts.map(item => (
      (artifactId === undefined || item.artifactId === String(artifactId))
      && item.status !== 'published'
        ? { ...item, status: 'published' }
        : item
    ));
    return { ...state, drafts };
  }
  return state;
}

function withReview(state: RunRuntimeState, event: RunEvent): RunRuntimeState {
  const batchValue = valueOf(event.payload, 'batchId', 'review_batch_id');
  const batchId = batchValue != null ? String(batchValue) : undefined;
  const artifactIds = Array.isArray(event.payload.artifact_ids)
    ? (event.payload.artifact_ids as unknown[]).map(String)
    : (state.review?.artifactIds ?? []);
  const base = state.review ?? { artifactIds: [], status: 'running' as const, revisions: 0 };
  switch (event.type) {
    case 'review.started':
      return {
        ...state,
        status: 'reviewing',
        review: {
          ...base,
          batchId: batchId ?? base.batchId,
          artifactIds,
          status: 'running',
          revisions: batchId === base.batchId ? base.revisions : 0,
        },
      };
    case 'review.revision_requested':
      return {
        ...state,
        status: 'running',
        review: { ...base, batchId: batchId ?? base.batchId, status: 'revision_requested', revisions: base.revisions + 1 },
      };
    case 'review.approved':
      return { ...state, review: { ...base, batchId: batchId ?? base.batchId, status: 'approved' } };
    case 'review.rejected':
      return { ...state, review: { ...base, batchId: batchId ?? base.batchId, status: 'rejected' } };
    default:
      return state;
  }
}

function withSteps(state: RunRuntimeState, event: RunEvent): RunRuntimeState {
  const steps = state.steps;
  switch (event.type) {
    case 'run.started':
      return { ...state, steps: pushStep(steps, { id: 'run', label: '开始执行', status: 'succeeded' }) };
    case 'run.resumed':
      return { ...state, steps: pushStep(steps, { id: `run-${event.id}`, label: '恢复执行', status: 'succeeded' }) };
    case 'tool.started':
      return {
        ...state,
        steps: pushStep(steps, { id: `tool-${event.id}`, label: `查询${toolNameOf(event.payload)}`, status: 'running' }),
      };
    case 'tool.succeeded':
    case 'tool.failed':
    case 'tool.unknown':
      {
        const status: RunStepStatus = event.type === 'tool.succeeded'
          ? 'succeeded'
          : event.type === 'tool.failed'
            ? 'failed'
            : 'unknown';
        const detail = status === 'succeeded' ? undefined : String(event.payload.error_type ?? '') || undefined;
        return { ...state, steps: updateStepByPrefix(steps, 'tool-', { status, detail }) };
      }
    case 'artifact.draft.created':
      return { ...state, steps: pushStep(steps, { id: `draft-${event.id}`, label: '生成产物草稿', status: 'running' }) };
    case 'review.started':
      return { ...state, steps: pushStep(steps, { id: `review-${event.id}`, label: '审核中', status: 'running' }) };
    case 'review.approved':
      return { ...state, steps: updateStepByPrefix(steps, 'review-', { status: 'succeeded' }) };
    case 'review.rejected':
      return { ...state, steps: updateStepByPrefix(steps, 'review-', { status: 'failed' }) };
    case 'artifact.published':
      return { ...state, steps: pushStep(steps, { id: `published-${event.id}`, label: '产物已发布', status: 'succeeded' }) };
    case 'run.completed':
      return { ...state, steps: pushStep(steps, { id: `terminal-${event.id}`, label: '分析完成', status: 'succeeded' }) };
    case 'run.failed':
      return {
        ...state,
        steps: pushStep(steps, {
          id: `terminal-${event.id}`,
          label: '运行失败',
          status: 'failed',
          detail: String(event.payload.message ?? '') || undefined,
        }),
      };
    case 'run.cancelled':
      return { ...state, steps: pushStep(steps, { id: `terminal-${event.id}`, label: '已取消', status: 'unknown' }) };
    default:
      return state;
  }
}

function withMessage(state: RunRuntimeState, event: RunEvent): RunRuntimeState {
  if (event.type === 'message.completed') {
    return { ...state, messageCompleted: true, activity: '已生成回复' };
  }
  return state;
}

export function reduceRunEvent(state: RunRuntimeState, event: RunEvent): RunRuntimeState {
  if (event.runId !== state.runId || event.id <= state.lastEventId) return state;
  const next = { ...state, lastEventId: event.id };
  return withSteps(
    withMessage(
      withReview(
        withDrafts(
          withToolCall(
            withThinking(withRunStatus(next, event), event),
            event,
          ),
          event,
        ),
        event,
      ),
      event,
    ),
    event,
  );
}
