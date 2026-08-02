import { useCallback, useEffect, useRef, useState } from 'react';

import type { AgentKolDetailResponse, AgentKolDetailSelectionRef } from '../api/agent';
import {
  getAgentArtifactPayload,
  getArtifact,
  getArtifactVersion,
  type KolDetailPayload,
} from '../api/agentArtifacts';
import { isTerminalRunStatus } from '../state/agentEvents';
import { useAgentRun } from './useAgentRun';

export type CreateKolDetail = (
  sessionId: string,
  platform: string,
  kolUid: string,
  selectionRef?: AgentKolDetailSelectionRef,
) => Promise<AgentKolDetailResponse>;

export type RunKolDetail = (
  sessionId: string,
  platform: string,
  kolUid: string,
  selectionRef?: AgentKolDetailSelectionRef,
) => Promise<void>;

export interface KolDetailFlowState {
  /** kol_detail_v2 payload：缓存命中直接给 detail，或从已发布 artifact 解析。 */
  payload?: KolDetailPayload;
  /** 辅助 Run 失败/详情不可用时的错误文案；提供时展示错误态而非无限加载。 */
  error?: string;
}

/**
 * 达人详情轻量 Run 流程（Task 23 §13.2）：createKolDetail → (缓存 detail | 已发布
 * artifact | 辅助 Run 订阅) → kol_detail_v2 payload。
 *
 * 原 favorites 弹窗走旧 selection detail 路由（已删除）；收藏/圈选详情统一改走
 * 新 /agent/sessions/{id}/kol-details（review Fix 1）。
 */
export function useKolDetailFlow(createKolDetail: CreateKolDetail): KolDetailFlowState & {
  run: RunKolDetail;
} {
  const [runId, setRunId] = useState<string>();
  const [payload, setPayload] = useState<KolDetailPayload>();
  const [error, setError] = useState<string>();
  const loadedRef = useRef(false);
  const helperRun = useAgentRun(runId);

  const loadArtifactDetail = useCallback(async (artifactId: string) => {
    try {
      const meta = await getArtifact(artifactId);
      const version = await getArtifactVersion(artifactId, meta.latest_version);
      const resolved = getAgentArtifactPayload(version);
      if (resolved?.schema_version === 'kol_detail_v2') {
        setPayload(resolved);
        return;
      }
      setError('达人详情数据不可用，请稍后重试');
    } catch {
      setError('达人详情加载失败，请稍后重试');
    }
  }, []);

  const run = useCallback<RunKolDetail>(async (sessionId, platform, kolUid, selectionRef) => {
    setPayload(undefined);
    setError(undefined);
    setRunId(undefined);
    loadedRef.current = false;
    try {
      const result = selectionRef
        ? await createKolDetail(sessionId, platform, kolUid, selectionRef)
        : await createKolDetail(sessionId, platform, kolUid);
      if (result.detail) {
        // 缓存命中：detail 即完整 kol_detail_v2 payload（data.cache.hit=true）。
        loadedRef.current = true;
        setPayload(result.detail as unknown as KolDetailPayload);
      } else if (result.artifact_id) {
        loadedRef.current = true;
        void loadArtifactDetail(result.artifact_id);
      } else if (result.run_id) {
        setRunId(result.run_id);
      } else {
        setError('达人详情生成失败，请稍后重试');
      }
    } catch (reason) {
      setError(reason instanceof Error && reason.message ? reason.message : '达人详情创建失败，请稍后重试');
    }
  }, [createKolDetail, loadArtifactDetail]);

  // 辅助 Run 到达终态后，从其 drafts 中取已发布 kol_detail 产物并解析 payload；
  // 终态却没有已发布产物（review reject / run failed）时落错误态而非无限加载。
  useEffect(() => {
    if (!runId || !helperRun || loadedRef.current) return;
    if (!isTerminalRunStatus(helperRun.status)) return;
    loadedRef.current = true;
    const published = helperRun.drafts.find(
      draft => draft.artifactId && draft.status === 'published',
    );
    if (published) {
      void loadArtifactDetail(published.artifactId);
      return;
    }
    setRunId(undefined);
    setError('达人详情生成失败，请稍后重试');
  }, [runId, helperRun, loadArtifactDetail]);

  return { payload, error, run };
}
