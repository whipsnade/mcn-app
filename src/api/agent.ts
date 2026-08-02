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

export interface ApiAgentMessage {
  id: string;
  role: string;
  content: string;
  sequence: number;
  run_id: string | null;
  created_at: string;
}

export interface ApiAgentRun {
  id: string;
  session_id: string;
  parent_run_id: string | null;
  profile_name: string;
  status: string;
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
): Promise<AgentMessageRunResponse> {
  const init: RequestInit = { method: 'POST', body: JSON.stringify({ content }) };
  if (idempotencyKey) init.headers = { 'Idempotency-Key': idempotencyKey };
  return request<AgentMessageRunResponse>(
    `/api/v1/agent/sessions/${encodeURIComponent(noSlash(sessionId))}/messages`,
    init,
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
