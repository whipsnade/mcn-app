import { ChevronDown, Layers, Loader2 } from 'lucide-react';
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
  getAgentArtifactPayload,
  getArtifact,
  getArtifactVersion,
} from '../../api/agentArtifacts';
import { useAgentRun } from '../../hooks/useAgentRun';
import { ArtifactStatus } from './ArtifactStatus';
import BrandArtifactView from './BrandArtifactView';
import CampaignArtifactView from './CampaignArtifactView';
import InsightBoardView from './InsightBoardView';
import KolAnalysisArtifactView from './KolAnalysisArtifactView';
import KolDetailArtifactDialog from './KolDetailArtifactDialog';
import KolSelectionArtifactView from './KolSelectionArtifactView';

type TopTabId = 'brand' | 'campaign' | 'kol';
type KolSubTabId = 'analysis' | 'selection';

const TOP_TABS: Array<{ id: TopTabId; label: string; module: string }> = [
  { id: 'brand', label: '品牌分析', module: 'brand' },
  { id: 'campaign', label: '活动分析', module: 'campaign' },
  { id: 'kol', label: '达人', module: 'kol' },
];

const KOL_SUB_TABS: Array<{ id: KolSubTabId; label: string; artifactType: string }> = [
  { id: 'analysis', label: 'KOL 分析', artifactType: 'kol_analysis_v2' },
  { id: 'selection', label: '圈选达人', artifactType: 'kol_selection_v3' },
];

const MODULES = ['brand', 'campaign', 'kol'] as const;

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
  const initRef = useRef<Set<string>>(new Set());
  const [selectedVersion, setSelectedVersion] = useState<Record<string, number>>({});
  const [payload, setPayload] = useState<AgentArtifactPayload>();
  const [payloadLoading, setPayloadLoading] = useState(false);

  // KOL 详情弹层：createKolDetail → 订阅辅助 Run → 解析 kol_detail_v2 payload。
  const [pendingDetail, setPendingDetail] = useState<{ platform: string; kolUid: string } | null>(null);
  const [detailRunId, setDetailRunId] = useState<string>();
  const [kolDetailPayload, setKolDetailPayload] = useState<KolDetailPayload>();
  const detailLoadedRef = useRef(false);
  const helperRun = useAgentRun(detailRunId);

  const activeType = topTab === 'brand'
    ? 'brand_report_v3'
    : topTab === 'campaign'
      ? 'campaign_report_v2'
      : kolSubTab === 'selection'
        ? 'kol_selection_v3'
        : 'kol_analysis_v2';

  const activeArtifact = useMemo(
    () => [...artifacts]
      .filter(artifact => artifact.artifact_type === activeType)
      .sort(
        (a, b) => b.activity_sequence - a.activity_sequence || b.updated_at.localeCompare(a.updated_at),
      )[0],
    [artifacts, activeType],
  );

  const maxSeq = useCallback((module: string) => {
    const sequences = artifacts
      .filter(artifact => artifact.module === module)
      .map(artifact => artifact.activity_sequence);
    return sequences.length ? Math.max(...sequences) : null;
  }, [artifacts]);

  // 模块首次出现时把水位初始化为当前最大 sequence：已有产物不标未读，
  // 之后更高的 Draft/发布才显示圆点。
  useEffect(() => {
    for (const module of MODULES) {
      if (initRef.current.has(module)) continue;
      const max = maxSeq(module);
      if (max !== null) {
        initRef.current.add(module);
        setLastSeen(previous => ({ ...previous, [module]: max }));
      }
    }
  }, [maxSeq]);

  const unread = (module: string): boolean => {
    const max = maxSeq(module);
    return max !== null && max > (lastSeen[module] ?? 0);
  };

  const selectTopTab = (tab: TopTabId) => {
    setTopTab(tab);
    const module = TOP_TABS.find(item => item.id === tab)!.module;
    const max = maxSeq(module);
    if (max !== null) {
      setLastSeen(previous => ({ ...previous, [module]: max }));
      markArtifactSeen(module, max);
    }
  };

  const versionNumber = activeArtifact
    ? selectedVersion[activeArtifact.id] ?? activeArtifact.latest_version
    : undefined;

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

  const loadArtifactDetail = async (artifactId: string) => {
    try {
      const meta = await getArtifact(artifactId);
      const version = await getArtifactVersion(artifactId, meta.latest_version);
      const resolved = getAgentArtifactPayload(version);
      if (resolved?.schema_version === 'kol_detail_v2') setKolDetailPayload(resolved);
    } catch {
      // 详情不可用时保持弹层加载态，由弹层兜底。
    }
  };

  const openKolDetail = async (item: KolSelectionItem) => {
    if (!sessionId) return;
    setPendingDetail({ platform: item.platform, kolUid: item.kol_uid });
    setKolDetailPayload(undefined);
    setDetailRunId(undefined);
    detailLoadedRef.current = false;
    const selectionRef: AgentKolDetailSelectionRef = {
      artifact_id: activeArtifact?.id,
      version: versionNumber !== undefined ? String(versionNumber) : undefined,
    };
    try {
      const result = await createKolDetail(sessionId, item.platform, item.kol_uid, selectionRef);
      if (result.artifact_id) {
        detailLoadedRef.current = true;
        void loadArtifactDetail(result.artifact_id);
      } else if (result.run_id) {
        setDetailRunId(result.run_id);
      }
    } catch {
      // 保持弹层加载态。
    }
  };

  // 辅助 Run 到达终态后，从其 drafts 中取已发布 kol_detail 产物并解析 payload。
  useEffect(() => {
    if (!detailRunId || !helperRun || detailLoadedRef.current) return;
    const published = helperRun.drafts.find(
      draft => draft.artifactId && draft.status === 'published',
    );
    if (!published) return;
    detailLoadedRef.current = true;
    getArtifactVersion(published.artifactId, published.version)
      .then(item => {
        const resolved = getAgentArtifactPayload(item);
        if (resolved?.schema_version === 'kol_detail_v2') setKolDetailPayload(resolved);
      })
      .catch(() => undefined);
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
        {TOP_TABS.map(({ id, label, module }) => (
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
            {unread(module) && <span data-testid="unread-dot" className="h-1.5 w-1.5 rounded-full bg-rose-500" />}
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
        )}

        {payloadLoading ? (
          <p role="status" className="flex items-center justify-center gap-2 p-6 text-center text-xs text-slate-400">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            加载中…
          </p>
        ) : payload ? (
          renderView()
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
          onClose={() => {
            setPendingDetail(null);
            setKolDetailPayload(undefined);
            setDetailRunId(undefined);
            detailLoadedRef.current = false;
          }}
        />
      )}
    </aside>
  );
}
