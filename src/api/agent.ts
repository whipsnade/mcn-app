/** 统一 Agent API 客户端（design §15.1 / Task 19）。
 *
 * Session CRUD、messages → Run、Run 生命周期、kol-details 与 Run SSE。
 * 事件流的 ``id`` 是 per-run ``sequence``，配合 ``Last-Event-ID`` 做断线续传。
 */

import { authorizedFetch, request } from './client';
import { parseSseStream } from './taskStream';
import type { RunEvent } from '../state/agentEvents';


export interface ApiAgentSession {
  id: string;
  title: string;
  status: string;
  created_at: string;
  updated_at: string;
}

/** assistant 消息 metadata（镜像 backend ask_user 澄清 metadata：type/question/options；
 *  Run 终态后 utility 追问建议写入 suggestions）。 */
export interface ApiAgentMessageMetadata {
  type?: string;
  question?: string;
  options?: string[];
  /** utility 生成的后续追问建议（父 Run 最新 assistant 消息，best-effort 可缺失）。 */
  suggestions?: string[];
}

export interface ApiAgentMessage {
  id: string;
  role: string;
  content: string;
  sequence: number;
  run_id: string | null;
  created_at: string;
  /** ask_user 澄清等结构化元数据（backend AgentMessageRead.metadata）。 */
  metadata?: ApiAgentMessageMetadata | null;
}

export interface ApiAgentRun {
  id: string;
  session_id: string;
  parent_run_id: string | null;
  profile_name: string;
  status: string;
  cancel_requested: boolean;
  outcome: string | null;
  decision_count: number;
  review_count: number;
  revision_count: number;
  error_code: string | null;
  started_at: string | null;
  paused_at: string | null;
  completed_at: string | null;
}

export interface ApiAgentSessionDetail extends ApiAgentSession {
  messages: ApiAgentMessage[];
  runs: ApiAgentRun[];
}

export interface AgentMessageRunResponse {
  run_id: string;
  session_id: string;
  message_id: string;
  status: string;
  reused: boolean;
}

export interface AgentKolDetailResponse {
  run_id: string | null;
  artifact_id: string | null;
  cached: boolean;
  detail: Record<string, unknown> | null;
}

export interface AgentKolDetailSelectionRef {
  artifact_id?: string;
  version?: string;
}

/** 发送消息的引用选项（Gate D Task 1）：父 Run / 已发布 Artifact Version /
 * 本 Session 已解析上传，按后端 AgentMessageCreate 映射为 snake_case。 */
export interface AgentMessageOptions {
  /** 幂等键：映射为 ``Idempotency-Key`` 请求头（后端按哈希幂等）。 */
  idempotencyKey?: string;
  /** 澄清/钻取来源 Run（只表达来源，不复用执行状态）。 */
  parentRunId?: string;
  /** 用户确认引用的已发布 Artifact Version id（≤10）。 */
  artifactVersionIds?: string[];
  /** 用户确认引用的本 Session 已解析上传 id（≤10）。 */
  uploadIds?: string[];
}

/** 上传状态（镜像后端 UploadRead/数据库约束：uploaded/parsed/failed）。 */
export type AgentUploadStatus = 'uploaded' | 'parsed' | 'failed';

/** 上传 DTO（镜像 backend AgentMessageCreate 同文件的 UploadRead：
 * id/original_filename/mime_type/size_bytes/sha256/status/error_code/created_at/completed_at）。 */
export interface ApiAgentUpload {
  id: string;
  original_filename: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  status: AgentUploadStatus;
  error_code: string | null;
  created_at: string;
  completed_at: string | null;
}

function noSlash(value: string): string {
  return value.replace(/^\/+/, '');
}

export function createSession(title?: string): Promise<ApiAgentSession> {
  return request<ApiAgentSession>('/api/v1/agent/sessions', {
    method: 'POST',
    body: JSON.stringify(title ? { title } : {}),
  });
}

export function listSessions(): Promise<ApiAgentSession[]> {
  return request<ApiAgentSession[]>('/api/v1/agent/sessions');
}

export function getSession(sessionId: string): Promise<ApiAgentSessionDetail> {
  return request<ApiAgentSessionDetail>(`/api/v1/agent/sessions/${encodeURIComponent(noSlash(sessionId))}`);
}

export function patchSession(
  sessionId: string,
  changes: { title?: string },
): Promise<ApiAgentSession> {
  return request<ApiAgentSession>(`/api/v1/agent/sessions/${encodeURIComponent(noSlash(sessionId))}`, {
    method: 'PATCH',
    body: JSON.stringify(changes),
  });
}

export function deleteSession(sessionId: string): Promise<void> {
  return request<void>(`/api/v1/agent/sessions/${encodeURIComponent(noSlash(sessionId))}`, {
    method: 'DELETE',
  });
}

export function sendMessage(
  sessionId: string,
  content: string,
  idempotencyKey?: string,
): Promise<AgentMessageRunResponse>;
export function sendMessage(
  sessionId: string,
  content: string,
  options?: AgentMessageOptions,
): Promise<AgentMessageRunResponse>;
export function sendMessage(
  sessionId: string,
  content: string,
  optionsOrKey: AgentMessageOptions | string = {},
): Promise<AgentMessageRunResponse> {
  // 归一化历史 string 幂等键与新的 options 对象：字符串绝不能被当作 options
  // 静默丢弃（旧签名 sendMessage(id, content, idempotencyKey?: string)）。
  const options = typeof optionsOrKey === 'string'
    ? { idempotencyKey: optionsOrKey }
    : optionsOrKey;
  const body: Record<string, unknown> = { content };
  // 只发送非空引用：无 options 的历史调用保持 ``{content}`` 兼容。
  if (options.parentRunId) body.parent_run_id = options.parentRunId;
  if (options.artifactVersionIds?.length) body.artifact_version_ids = options.artifactVersionIds;
  if (options.uploadIds?.length) body.upload_ids = options.uploadIds;
  const init: RequestInit = { method: 'POST', body: JSON.stringify(body) };
  if (options.idempotencyKey) init.headers = { 'Idempotency-Key': options.idempotencyKey };
  return request<AgentMessageRunResponse>(
    `/api/v1/agent/sessions/${encodeURIComponent(noSlash(sessionId))}/messages`,
    init,
  );
}

/** 上传资料文件（multipart 字段名 ``file``，镜像后端 UploadFile）；只能走
 * ``authorizedFetch``——request() 会强制 JSON Content-Type，multipart boundary
 * 必须由浏览器生成。返回 ``ApiAgentUpload``（镜像后端 UploadRead）。 */
export async function uploadAgentFile(sessionId: string, file: File): Promise<ApiAgentUpload> {
  const form = new FormData();
  form.append('file', file);
  const response = await authorizedFetch(
    `/api/v1/agent/sessions/${encodeURIComponent(noSlash(sessionId))}/uploads`,
    { method: 'POST', body: form },
  );
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string } | null;
    throw new Error(body?.detail ?? `HTTP_${response.status}`);
  }
  return response.json() as Promise<ApiAgentUpload>;
}

/** 重试 failed/paused Run：后端创建新的 user-visible Child Run，返回
 * ``MessageRunResponse``（镜像 POST /runs/{id}/retry）。 */
export function retryRun(runId: string): Promise<AgentMessageRunResponse> {
  return request<AgentMessageRunResponse>(
    `/api/v1/agent/runs/${encodeURIComponent(noSlash(runId))}/retry`,
    { method: 'POST' },
  );
}

export function getRun(runId: string): Promise<ApiAgentRun> {
  return request<ApiAgentRun>(`/api/v1/agent/runs/${encodeURIComponent(noSlash(runId))}`);
}

export function cancelRun(runId: string): Promise<ApiAgentRun> {
  return request<ApiAgentRun>(`/api/v1/agent/runs/${encodeURIComponent(noSlash(runId))}/cancel`, {
    method: 'POST',
  });
}

export function resumeRun(runId: string): Promise<ApiAgentRun> {
  return request<ApiAgentRun>(`/api/v1/agent/runs/${encodeURIComponent(noSlash(runId))}/resume`, {
    method: 'POST',
  });
}

export function createKolDetailRun(
  sessionId: string,
  platform: string,
  kolUid: string,
  selectionRef?: AgentKolDetailSelectionRef,
): Promise<AgentKolDetailResponse> {
  return request<AgentKolDetailResponse>(
    `/api/v1/agent/sessions/${encodeURIComponent(noSlash(sessionId))}/kol-details`,
    {
      method: 'POST',
      body: JSON.stringify({
        platform,
        kol_uid: kolUid,
        selection_artifact_id: selectionRef?.artifact_id,
        selection_version: selectionRef?.version,
      }),
    },
  );
}

function toRunEvent(runId: string, raw: { id?: string; event?: string; data: string }): RunEvent {
  const id = Number(raw.id);
  if (!Number.isSafeInteger(id) || id < 1 || !raw.event) {
    throw new Error('SSE_INVALID_EVENT');
  }
  const payload = JSON.parse(raw.data || '{}') as Record<string, unknown>;
  return { id, runId, type: raw.event, payload };
}

export async function fetchRunEvents(
  runId: string,
  lastEventId: number,
  signal: AbortSignal,
  onEvent: (event: RunEvent) => void,
): Promise<void> {
  const headers = new Headers({ Accept: 'text/event-stream' });
  if (lastEventId > 0) headers.set('Last-Event-ID', String(lastEventId));
  const response = await authorizedFetch(
    `/api/v1/agent/runs/${encodeURIComponent(noSlash(runId))}/events`,
    { headers, signal },
  );
  if (!response.ok || !response.body) throw new Error(`SSE_${response.status}`);
  await parseSseStream(response.body, raw => onEvent(toRunEvent(runId, raw)));
}
