import { ChevronDown, Download, Layers, Loader2 } from 'lucide-react';
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';

import type {
  AgentKolDetailResponse,
  AgentKolDetailSelectionRef,
} from '../../api/agent';
import type {
  AgentArtifactPayload,
  ApiAgentArtifact,
  InsightBoardPayload,
  KolDetailPayload,
  KolSelectionItem,
} from '../../api/agentArtifacts';
import {
  exportArtifact,
  getAgentArtifactPayload,
  getArtifact,
  getArtifactVersion,
  listArtifactReadStates,
} from '../../api/agentArtifacts';
import { useAgentRun } from '../../hooks/useAgentRun';
import { isTerminalRunStatus } from '../../state/agentEvents';
import { ArtifactStatus } from './ArtifactStatus';
import BrandArtifactView from './BrandArtifactView';
import CampaignArtifactView from './CampaignArtifactView';
import InsightBoardView from './InsightBoardView';
import KolAnalysisArtifactView from './KolAnalysisArtifactView';
import KolDetailArtifactDialog from './KolDetailArtifactDialog';
import KolSelectionArtifactView from './KolSelectionArtifactView';

type TopTabId = 'brand' | 'campaign' | 'kol';
type KolSubTabId = 'analysis' | 'selection';

// 一级 Tab → 后端 Artifact 模块名。达人 Tab 聚合 kol-selection / kol-analysis
// 两个模块（任一有未读即点亮）；kol-detail 走详情弹层，其更新不点亮主圆点。
const TOP_TABS: Array<{ id: TopTabId; label: string; modules: string[] }> = [
  { id: 'brand', label: '品牌分析', modules: ['brand'] },
  { id: 'campaign', label: '活动分析', modules: ['campaign'] },
  { id: 'kol', label: '达人', modules: ['kol-selection', 'kol-analysis'] },
];

const KOL_SUB_TABS: Array<{ id: KolSubTabId; label: string; artifactType: string }> = [
  { id: 'analysis', label: 'KOL 分析', artifactType: 'kol_analysis_v2' },
  { id: 'selection', label: '圈选达人', artifactType: 'kol_selection_v3' },
];

// 支持 Excel 导出的产物类型（对齐后端 exporters 分派表）。
const EXPORTABLE_TYPES = new Set(['brand_report_v3', 'campaign_report_v2', 'kol_selection_v3']);

export interface ArtifactWorkspaceProps {
  sessionId?: string;
  /** 当前会话的 Artifact 目录（来自 useAgentWorkspace，随 artifact 事件刷新）。 */
  artifacts: ApiAgentArtifact[];
  /** 用户查看某模块后推进该模块未读水位。 */
  markArtifactSeen: (module: string, lastSeenSequence: number) => void;
  /** 点击圈选达人时创建 kol_detail 轻量 Run。 */
  createKolDetail: (
    sessionId: string,
    platform: string,
    kolUid: string,
    selectionRef?: AgentKolDetailSelectionRef,
  ) => Promise<AgentKolDetailResponse>;
}

/** 子分析（insight_board_v1）条目：延迟拉取标题，可展开渲染 InsightBoardView。 */
function InsightChildItem({ sessionId, artifact }: { sessionId?: string; artifact: ApiAgentArtifact }) {
  const [insight, setInsight] = useState<InsightBoardPayload>();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    getArtifactVersion(artifact.id, artifact.latest_version)
      .then(item => {
        const resolved = getAgentArtifactPayload(item);
        if (!cancelled && resolved?.schema_version === 'insight_board_v1') setInsight(resolved);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [sessionId, artifact.id, artifact.latest_version]);

  return (
    <div className="overflow-hidden rounded-xl border border-slate-100 bg-white shadow-sm">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(value => !value)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition hover:bg-slate-50"
      >
        <Layers className="h-3.5 w-3.5 shrink-0 text-indigo-500" aria-hidden="true" />
        <span className="min-w-0 flex-1 truncate text-[11px] font-medium text-slate-700">
          {insight?.title ?? '钻取分析…'}
        </span>
        <ChevronDown className={`h-3.5 w-3.5 shrink-0 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`} aria-hidden="true" />
      </button>
      {open && insight && (
        <div className="border-t border-slate-100 p-3">
          <InsightBoardView payload={insight} />
        </div>
      )}
    </div>
  );
}

export default function ArtifactWorkspace({
  sessionId,
  artifacts,
  markArtifactSeen,
  createKolDetail,
}: ArtifactWorkspaceProps) {
  const [topTab, setTopTab] = useState<TopTabId>('kol');
  const [kolSubTab, setKolSubTab] = useState<KolSubTabId>('analysis');
  const [lastSeen, setLastSeen] = useState<Record<string, number>>({});
  const [readStatesReady, setReadStatesReady] = useState(false);
  const [selectedVersion, setSelectedVersion] = useState<Record<string, number>>({});
  const [payload, setPayload] = useState<AgentArtifactPayload>();
  const [payloadLoading, setPayloadLoading] = useState(false);
  const [exporting, setExporting] = useState(false);

  // KOL 详情弹层：createKolDetail → 订阅辅助 Run → 解析 kol_detail_v2 payload。
  const [pendingDetail, setPendingDetail] = useState<{
    platform: string;
    kolUid: string;
    selectionRef?: AgentKolDetailSelectionRef;
  } | null>(null);
  const [detailRunId, setDetailRunId] = useState<string>();
  const [kolDetailPayload, setKolDetailPayload] = useState<KolDetailPayload>();
  const [detailError, setDetailError] = useState<string>();
  const detailLoadedRef = useRef(false);
  const helperRun = useAgentRun(detailRunId);

  const activeType = topTab === 'brand'
    ? 'brand_report_v3'
    : topTab === 'campaign'
      ? 'campaign_report_v2'
      : kolSubTab === 'selection'
        ? 'kol_selection_v3'
        : 'kol_analysis_v2';

  // §13.2：Draft/发布只打圆点。展示主体取「非生成中」的最新产物；若当前最新是
  // 生成中的 Draft（latest_version 0 / generating / reviewing），回退到上一个
  // 已发布产物保持内容可见，同时由 UI 展示「生成中」提示（review Fix 2）。
  const isGeneratingArtifact = (artifact: ApiAgentArtifact): boolean =>
    artifact.latest_version === 0
    || artifact.status === 'generating'
    || artifact.status === 'draft'
    || artifact.status === 'reviewing';

  const activeTypeCandidates = useMemo(
    () => artifacts.filter(artifact => artifact.artifact_type === activeType),
    [artifacts, activeType],
  );
  const hasGeneratingDraft = useMemo(
    () => activeTypeCandidates.some(isGeneratingArtifact),
    [activeTypeCandidates],
  );
  const activeArtifact = useMemo(() => {
    const byNewest = (list: ApiAgentArtifact[]) => [...list].sort(
      (a, b) => b.activity_sequence - a.activity_sequence || b.updated_at.localeCompare(a.updated_at),
    );
    const newest = byNewest(activeTypeCandidates)[0];
    if (!newest) return undefined;
    if (!isGeneratingArtifact(newest)) return newest;
    return byNewest(activeTypeCandidates.filter(artifact => !isGeneratingArtifact(artifact)))[0] ?? newest;
  }, [activeTypeCandidates]);

  const maxSeq = useCallback((module: string) => {
    const sequences = artifacts
      .filter(artifact => artifact.module === module)
      .map(artifact => artifact.activity_sequence);
    return sequences.length ? Math.max(...sequences) : null;
  }, [artifacts]);

  // 切换会话时重置本地水位并从服务端拉取已读水位初始化（C2）：刷新/切换
  // 会话不再吞掉离线期间的未读。组件跨会话复用不卸载，旧会话水位必须清零。
  // 服务端无记录的模块按零水位（未查看过即全部未读）；拉取失败同样按零水位，
  // 宁可多打圆点也不吞未读。水位就绪前不打点，避免先全亮再熄灭的闪烁。
  useEffect(() => {
    setLastSeen({});
    if (!sessionId) {
      setReadStatesReady(true);
      return;
    }
    setReadStatesReady(false);
    let cancelled = false;
    listArtifactReadStates(sessionId)
      .then(states => {
        if (cancelled) return;
        const next: Record<string, number> = {};
        for (const state of states) next[state.module] = state.last_seen_sequence;
        setLastSeen(next);
        setReadStatesReady(true);
      })
      .catch(() => {
        if (!cancelled) setReadStatesReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  const unread = (modules: string[]): boolean => {
    if (!readStatesReady) return false;
    return modules.some(module => {
      const max = maxSeq(module);
      return max !== null && max > (lastSeen[module] ?? 0);
    });
  };

  // 点击 Tab 按实际模块名逐个上报水位（服务端 max(旧, 新) 语义）。
  const selectTopTab = (tab: TopTabId) => {
    setTopTab(tab);
    const modules = TOP_TABS.find(item => item.id === tab)!.modules;
    for (const module of modules) {
      const max = maxSeq(module);
      if (max === null) continue;
      setLastSeen(previous => ({ ...previous, [module]: max }));
      markArtifactSeen(module, max);
    }
  };

  const versionNumber = activeArtifact
    ? selectedVersion[activeArtifact.id] ?? activeArtifact.latest_version
    : undefined;

  // Excel 导出（C2）：仅 published 且后端支持的类型（restricted 也可导出，沿用
  // 后端语义）；导出版本与当前下拉查看版本一致。
  const canExport = activeArtifact !== undefined
    && activeArtifact.status === 'published'
    && EXPORTABLE_TYPES.has(activeArtifact.artifact_type)
    && versionNumber !== undefined;

  const handleExport = () => {
    if (!activeArtifact || versionNumber === undefined || exporting) return;
    setExporting(true);
    // 导出失败恢复可重试态；低频操作，错误细节由控制台/网络面板兜底。
    void exportArtifact(activeArtifact.id, versionNumber)
      .catch(() => undefined)
      .finally(() => setExporting(false));
  };

  // 选中产物版本 → 拉取 payload（按 schema_version 收窄渲染）。
  useEffect(() => {
    if (!sessionId || !activeArtifact) {
      setPayload(undefined);
      setPayloadLoading(false);
      return;
    }
    const version = selectedVersion[activeArtifact.id] ?? activeArtifact.latest_version;
    let cancelled = false;
    setPayloadLoading(true);
    getArtifactVersion(activeArtifact.id, version)
      .then(item => {
        if (!cancelled) setPayload(getAgentArtifactPayload(item));
      })
      .catch(() => {
        if (!cancelled) setPayload(undefined);
      })
      .finally(() => {
        if (!cancelled) setPayloadLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, activeArtifact?.id, activeArtifact?.latest_version, activeArtifact?.updated_at, versionNumber]);

  // 详情版本统一走 getArtifact().latest_version（Draft 事件携带的 version 可能是
  // 未提交的 0）；失败时落错误态，避免 detailLoadedRef 卡死无限加载（review Fix 3）。
  const loadArtifactDetail = async (artifactId: string) => {
    try {
      const meta = await getArtifact(artifactId);
      const version = await getArtifactVersion(artifactId, meta.latest_version);
      const resolved = getAgentArtifactPayload(version);
      if (resolved?.schema_version === 'kol_detail_v2') {
        setKolDetailPayload(resolved);
        return;
      }
      setDetailError('达人详情数据不可用，请稍后重试');
    } catch {
      setDetailError('达人详情加载失败，请稍后重试');
    }
  };

  const runKolDetailFlow = async (platform: string, kolUid: string, selectionRef: AgentKolDetailSelectionRef) => {
    if (!sessionId) return;
    setKolDetailPayload(undefined);
    setDetailError(undefined);
    setDetailRunId(undefined);
    detailLoadedRef.current = false;
    try {
      const result = await createKolDetail(sessionId, platform, kolUid, selectionRef);
      if (result.artifact_id) {
        detailLoadedRef.current = true;
        void loadArtifactDetail(result.artifact_id);
      } else if (result.run_id) {
        setDetailRunId(result.run_id);
      }
    } catch (reason) {
      setDetailError(reason instanceof Error && reason.message ? reason.message : '达人详情创建失败，请稍后重试');
    }
  };

  const openKolDetail = (item: KolSelectionItem) => {
    if (!sessionId) return;
    const selectionRef: AgentKolDetailSelectionRef = {
      artifact_id: activeArtifact?.id,
      version: versionNumber !== undefined ? String(versionNumber) : undefined,
    };
    setPendingDetail({ platform: item.platform, kolUid: item.kol_uid, selectionRef });
    void runKolDetailFlow(item.platform, item.kol_uid, selectionRef);
  };

  // 辅助 Run 到达终态后，从其 drafts 中取已发布 kol_detail 产物并解析 payload；
  // 终态却没有已发布产物（review reject / run failed）时落错误态而非无限加载。
  useEffect(() => {
    if (!detailRunId || !helperRun || detailLoadedRef.current) return;
    if (!isTerminalRunStatus(helperRun.status)) return;
    detailLoadedRef.current = true;
    const published = helperRun.drafts.find(
      draft => draft.artifactId && draft.status === 'published',
    );
    if (published) {
      void loadArtifactDetail(published.artifactId);
      return;
    }
    setDetailRunId(undefined);
    setDetailError('达人详情生成失败，请稍后重试');
  }, [detailRunId, helperRun]);

  const childInsights = useMemo(
    () => (activeArtifact
      ? artifacts.filter(artifact => (
        artifact.parent_artifact_id === activeArtifact.id && artifact.artifact_type === 'insight_board_v1'
      ))
      : []),
    [artifacts, activeArtifact],
  );

  const emptyText = topTab === 'brand'
    ? '完成一次品牌分析后在此展示'
    : topTab === 'campaign'
      ? '完成一次活动分析后在此展示'
      : kolSubTab === 'selection'
        ? '完成圈选后在此展示'
        : '完成 KOL 分析后在此展示';

  const renderView = () => {
    if (!payload) return null;
    switch (payload.schema_version) {
      case 'brand_report_v3':
        return <BrandArtifactView payload={payload} />;
      case 'campaign_report_v2':
        return <CampaignArtifactView payload={payload} />;
      case 'kol_selection_v3':
        return <KolSelectionArtifactView payload={payload} onOpenDetail={item => void openKolDetail(item)} />;
      case 'kol_analysis_v2':
        return <KolAnalysisArtifactView payload={payload} />;
      case 'insight_board_v1':
        return <InsightBoardView payload={payload} />;
      case 'kol_detail_v2':
        return null;
      default:
        return null;
    }
  };

  return (
    <aside className="flex h-full w-full shrink-0 flex-col overflow-hidden border-l border-slate-200 bg-white shadow-sm xl:w-[420px]">
      <div role="tablist" aria-label="分析报告" className="flex h-11 shrink-0 border-b border-slate-200 bg-white px-4">
        {TOP_TABS.map(({ id, label, modules }) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={topTab === id}
            onClick={() => selectTopTab(id)}
            className={topTab === id
              ? 'flex shrink-0 items-center gap-1.5 border-b-2 border-indigo-600 px-3 text-[11px] font-semibold text-indigo-600'
              : 'flex shrink-0 items-center gap-1.5 px-3 text-[11px] font-medium text-slate-500 transition hover:text-slate-800'}
          >
            {label}
            {/* 注（review M3）：未读圆点按模块聚合且仅用颜色区分，无独立可访问名称；
                已在 data-testid 上留查询锚点，无障碍优化列为后续打磨项。 */}
            {unread(modules) && (
              <span className="flex items-center gap-1" aria-hidden="true">
                <span className="text-[9px] font-semibold text-rose-500">更新</span>
                <span data-testid="unread-dot" className="h-1.5 w-1.5 rounded-full bg-rose-500" />
              </span>
            )}
          </button>
        ))}
      </div>

      {topTab === 'kol' && (
        <div role="tablist" aria-label="达人分析" className="flex h-9 shrink-0 border-b border-slate-200 bg-white px-4">
          {KOL_SUB_TABS.map(({ id, label }) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={kolSubTab === id}
              onClick={() => setKolSubTab(id)}
              className={kolSubTab === id
                ? 'flex shrink-0 items-center border-b-2 border-indigo-600 px-3 text-[11px] font-semibold text-indigo-600'
                : 'flex shrink-0 items-center px-3 text-[11px] font-medium text-slate-500 transition hover:text-slate-800'}
            >
              {label}
            </button>
          ))}
        </div>
      )}

      <div className="flex-1 overflow-y-auto bg-slate-50/40 p-3">
        {activeArtifact && (
          <div className="mb-3 flex items-center justify-between gap-2 px-1">
            <ArtifactStatus status={activeArtifact.status} dataStatus={payload?.data_status} />
            <div className="flex shrink-0 items-center gap-2">
              {canExport && (
                <button
                  type="button"
                  onClick={handleExport}
                  disabled={exporting}
                  className="flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2 py-1 text-[10px] font-semibold text-slate-600 shadow-sm transition hover:bg-slate-50 disabled:opacity-50"
                >
                  {exporting
                    ? <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                    : <Download className="h-3 w-3" aria-hidden="true" />}
                  导出 Excel
                </button>
              )}
              {activeArtifact.latest_version > 1 && (
                <select
                  aria-label="版本选择"
                  value={versionNumber !== undefined ? String(versionNumber) : ''}
                  onChange={event => {
                    if (!activeArtifact) return;
                    setSelectedVersion(previous => ({
                      ...previous,
                      [activeArtifact.id]: Number(event.target.value),
                    }));
                  }}
                  className="shrink-0 rounded-lg border border-slate-200 bg-white px-2 py-1 text-[10px] font-semibold text-slate-600 shadow-sm"
                >
                  {Array.from({ length: activeArtifact.latest_version }, (_, index) => index + 1).map(version => (
                    <option key={version} value={version}>v{version}</option>
                  ))}
                </select>
              )}
            </div>
          </div>
        )}

        {payloadLoading ? (
          <p role="status" className="flex items-center justify-center gap-2 p-6 text-center text-xs text-slate-400">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            加载中…
          </p>
        ) : payload ? (
          <>
            {hasGeneratingDraft && (
              <p className="mb-3 flex items-center gap-1.5 rounded-lg bg-indigo-50 px-2.5 py-2 text-[11px] font-medium text-indigo-700">
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                新产物生成中，正在展示最近一版分析
              </p>
            )}
            {renderView()}
          </>
        ) : hasGeneratingDraft ? (
          <div className="flex min-h-[120px] items-center justify-center p-6 text-center text-xs leading-5 text-slate-500">
            <span className="flex items-center gap-2">
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
              正在生成分析…
            </span>
          </div>
        ) : (
          <div className="flex min-h-[120px] items-center justify-center p-6 text-center text-xs leading-5 text-slate-500">
            {emptyText}
          </div>
        )}

        {childInsights.length > 0 && (
          <section className="mt-4">
            <h3 className="mb-2 px-1 text-[12px] font-bold text-slate-700">钻取分析</h3>
            <div className="space-y-2">
              {childInsights.map(child => (
                <Fragment key={child.id}>
                  <InsightChildItem sessionId={sessionId} artifact={child} />
                </Fragment>
              ))}
            </div>
          </section>
        )}
      </div>

      {pendingDetail && (
        <KolDetailArtifactDialog
          payload={kolDetailPayload}
          error={detailError}
          onRetry={() => void runKolDetailFlow(
            pendingDetail.platform,
            pendingDetail.kolUid,
            pendingDetail.selectionRef ?? {},
          )}
          onClose={() => {
            setPendingDetail(null);
            setKolDetailPayload(undefined);
            setDetailError(undefined);
            setDetailRunId(undefined);
            detailLoadedRef.current = false;
          }}
        />
      )}
    </aside>
  );
}
