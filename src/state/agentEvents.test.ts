import { describe, expect, it } from 'vitest';

import {
  initialRunRuntime,
  isTerminalRunStatus,
  reduceRunEvent,
  type RunEvent,
} from './agentEvents';


function event(
  type: string,
  id: number,
  payload: Record<string, unknown> = {},
  runId = 'run-1',
): RunEvent {
  return { id, runId, type, payload };
}

describe('reduceRunEvent', () => {
  it('builds run card state from a full run event sequence', () => {
    let state = initialRunRuntime('run-1');
    state = reduceRunEvent(state, event('run.started', 1, { run_kind: 'user' }));
    state = reduceRunEvent(state, event('thinking.started', 2, { attempt: 1 }));
    state = reduceRunEvent(state, event('thinking.delta', 3, { text: '正在检索' }));
    state = reduceRunEvent(state, event('tool.started', 4, { internal_tool_name: 'brand_search' }));
    state = reduceRunEvent(state, event('tool.succeeded', 5, { internal_tool_name: 'brand_search' }));
    state = reduceRunEvent(state, event('artifact.draft.created', 6, {
      artifact_id: 'art-1',
      module: 'brand',
      status: 'draft',
      title: '品牌报告 v1',
    }));
    state = reduceRunEvent(state, event('review.started', 7, { review_batch_id: 'batch-1' }));
    state = reduceRunEvent(state, event('review.approved', 8, { review_batch_id: 'batch-1' }));
    state = reduceRunEvent(state, event('artifact.published', 9, { artifact_id: 'art-1', module: 'brand' }));
    // §5.8 事件顺序：message.completed 先于 run.completed，终态事件最后。
    state = reduceRunEvent(state, event('message.completed', 10, { type: 'completion' }));
    state = reduceRunEvent(state, event('run.completed', 11, { outcome: 'completed' }));

    expect(state.status).toBe('completed');
    expect(state.connection).toBe('closed');
    expect(state.hasThinking).toBe(true);
    expect(state.thinking).toBe('正在检索');
    expect(state.thinkingStatus).toBe('running');
    expect(state.toolCalls).toEqual([
      { id: expect.any(String), name: 'brand_search', status: 'succeeded' },
    ]);
    expect(state.drafts).toHaveLength(1);
    expect(state.drafts[0]).toMatchObject({
      artifactId: 'art-1',
      module: 'brand',
      title: '品牌报告 v1',
      status: 'published',
    });
    expect(state.review).toMatchObject({ batchId: 'batch-1', status: 'approved', revisions: 0 });
    expect(state.messageCompleted).toBe(true);
    expect(state.steps.length).toBeGreaterThan(0);
  });

  it('is idempotent by (run_id, sequence): replays and older events do not double-apply', () => {
    let state = initialRunRuntime('run-1');
    state = reduceRunEvent(state, event('thinking.delta', 3, { text: 'hello ' }));
    const afterDelta = state;

    state = reduceRunEvent(state, event('thinking.delta', 3, { text: 'IGNORED' }));
    state = reduceRunEvent(state, event('thinking.delta', 2, { text: 'IGNORED2' }));

    expect(state).toBe(afterDelta);
    expect(state.thinking).toBe('hello ');
  });

  it('shows a generic processing status with no expandable thinking when no thinking events arrive', () => {
    let state = initialRunRuntime('run-1');
    state = reduceRunEvent(state, event('run.started', 1, {}));
    state = reduceRunEvent(state, event('tool.started', 2, { internal_tool_name: 'brand_search' }));

    expect(state.status).toBe('running');
    expect(state.hasThinking).toBe(false);
    expect(state.thinking).toBe('');
  });

  it('ignores events belonging to another run', () => {
    const state = initialRunRuntime('run-1');
    expect(reduceRunEvent(state, event('tool.started', 1, {}, 'run-2'))).toBe(state);
  });

  it('marks the stream closed on a terminal run event', () => {
    let state = initialRunRuntime('run-1');
    state = reduceRunEvent(state, event('run.failed', 1, { error_code: 'model_failed' }));

    expect(state.status).toBe('failed');
    expect(state.connection).toBe('closed');
    expect(isTerminalRunStatus(state.status)).toBe(true);
  });

  it('ends the stream on cancel', () => {
    let state = initialRunRuntime('run-1');
    state = reduceRunEvent(state, event('run.started', 1, {}));
    state = reduceRunEvent(state, event('run.cancelled', 2, {}));

    expect(state.status).toBe('cancelled');
    expect(state.connection).toBe('closed');
    expect(isTerminalRunStatus(state.status)).toBe(true);
  });

  it('tracks pause/resume and review revisions', () => {
    let state = initialRunRuntime('run-1');
    state = reduceRunEvent(state, event('run.started', 1, {}));
    state = reduceRunEvent(state, event('run.paused', 2, { attempt_id: 'attempt-1' }));
    expect(state.status).toBe('paused');

    state = reduceRunEvent(state, event('run.resumed', 3, { attempt: 2 }));
    expect(state.status).toBe('running');

    state = reduceRunEvent(state, event('review.started', 4, { review_batch_id: 'batch-1' }));
    expect(state.status).toBe('reviewing');
    state = reduceRunEvent(state, event('review.revision_requested', 5, { review_batch_id: 'batch-1' }));
    expect(state.review).toMatchObject({ batchId: 'batch-1', status: 'revision_requested', revisions: 1 });
    state = reduceRunEvent(state, event('review.rejected', 6, { review_batch_id: 'batch-1' }));
    expect(state.review?.status).toBe('rejected');
  });

  it('produces clarification_requested from an ask_user message.completed event', () => {
    let state = initialRunRuntime('run-1');
    state = reduceRunEvent(state, event('run.started', 1, {}));
    state = reduceRunEvent(state, event('message.completed', 2, { type: 'clarification' }));

    expect(state.status).toBe('clarification_requested');
    expect(state.messageCompleted).toBe(true);
    // 澄清非执行终态：SSE 保持连接，等待用户回答后由新 Run 接续。
    expect(isTerminalRunStatus(state.status)).toBe(false);
  });

  it('does not set clarification_requested for a normal completion message', () => {
    let state = initialRunRuntime('run-1');
    state = reduceRunEvent(state, event('run.completed', 1, { outcome: 'completed' }));
    state = reduceRunEvent(state, event('message.completed', 2, { type: 'completion' }));

    expect(state.status).toBe('completed');
    expect(state.messageCompleted).toBe(true);
  });

  it('tracks artifact payload fields and bumps artifactsVersion only on artifact/review events', () => {
    let state = initialRunRuntime('run-1');
    state = reduceRunEvent(state, event('thinking.delta', 1, { text: 'x' }));
    state = reduceRunEvent(state, event('tool.started', 2, { internal_tool_name: 't' }));
    expect(state.artifactsVersion).toBe(0);

    // §15.3：draft 事件带 artifact_id/module/parent_artifact_id/status（+version 草稿修订号）
    state = reduceRunEvent(state, event('artifact.draft.created', 3, {
      artifact_id: 'art-1',
      module: 'kol-detail',
      parent_artifact_id: 'art-parent',
      status: 'draft',
      version: 1,
    }));
    expect(state.artifactsVersion).toBe(1);
    expect(state.drafts[0]).toMatchObject({
      artifactId: 'art-1',
      module: 'kol-detail',
      parentArtifactId: 'art-parent',
      status: 'draft',
      version: 1,
    });

    state = reduceRunEvent(state, event('artifact.draft.updated', 4, {
      artifact_id: 'art-1',
      module: 'kol-detail',
      parent_artifact_id: 'art-parent',
      status: 'draft',
      version: 2,
    }));
    expect(state.artifactsVersion).toBe(2);
    expect(state.drafts).toHaveLength(1);
    expect(state.drafts[0]).toMatchObject({ version: 2, parentArtifactId: 'art-parent' });

    state = reduceRunEvent(state, event('review.started', 5, { review_batch_id: 'b-1' }));
    expect(state.artifactsVersion).toBe(3);

    // 发布事件另带 version（发布版本号）
    state = reduceRunEvent(state, event('artifact.published', 6, {
      artifact_id: 'art-1',
      module: 'kol-detail',
      parent_artifact_id: 'art-parent',
      status: 'published',
      version: 1,
    }));
    expect(state.artifactsVersion).toBe(4);
    expect(state.drafts[0].status).toBe('published');
  });

  it('folds thinking on thinking.completed and marks interrupted on thinking.failed', () => {
    let state = initialRunRuntime('run-1');
    state = reduceRunEvent(state, event('thinking.started', 1, { attempt: 1 }));
    state = reduceRunEvent(state, event('thinking.delta', 2, { text: '推理中' }));
    state = reduceRunEvent(state, event('thinking.completed', 3, { attempt: 1, duration_ms: 120 }));

    expect(state.hasThinking).toBe(true);
    expect(state.thinking).toBe('推理中');
    expect(state.thinkingStatus).toBe('completed');

    let failedState = initialRunRuntime('run-1');
    failedState = reduceRunEvent(failedState, event('thinking.started', 1, { attempt: 1 }));
    failedState = reduceRunEvent(failedState, event('thinking.delta', 2, { text: '半截' }));
    failedState = reduceRunEvent(
      failedState,
      event('thinking.failed', 3, { attempt: 1, error_code: 'MODEL_PLAN_INVALID' }),
    );

    expect(failedState.thinkingStatus).toBe('interrupted');
    expect(failedState.thinking).toBe('半截');
  });
});
