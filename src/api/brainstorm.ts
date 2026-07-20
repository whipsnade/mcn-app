import type { Session } from '../types';
import { request } from './client';
import type { ApiBrainstormResponse } from './contracts';


export function postBrainstorm(sessionId: string, content: string): Promise<ApiBrainstormResponse> {
  return request<ApiBrainstormResponse>(`/api/v1/sessions/${sessionId}/brainstorm`, {
    method: 'POST',
    body: JSON.stringify({ content }),
  });
}


/**
 * 画像 ready 判定：以会话最近一条带 brainstorm metadata 的消息为准。
 * 没有任何 brainstorm 消息（空白会话）视为未 ready。
 */
export function isBrainstormProfileReady(session: Session): boolean {
  for (let index = session.messages.length - 1; index >= 0; index -= 1) {
    const brainstorm = session.messages[index].brainstorm;
    if (brainstorm) return brainstorm.ready;
  }
  return false;
}
