import { act, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from './App';
import type { ApiQuickKolRecommendations, ApiQuickTopPosts } from './api/contracts';
import { listFavorites } from './api/favorites';
import { getKolRecommendations, getTopPosts, postEvaluate } from './api/quick';
import { getWallet } from './api/wallet';
import { useAuth } from './auth/AuthProvider';
import EvaluatePanel from './components/EvaluatePanel';
import KolRecommendPanel from './components/KolRecommendPanel';
import TopPostsPanel from './components/TopPostsPanel';
import { useWorkspace } from './hooks/useWorkspace';
import { QuickFeatureCacheProvider } from './state/QuickFeatureCache';

vi.mock('./api/quick', () => ({
  getTopPosts: vi.fn(),
  getKolRecommendations: vi.fn(),
  postEvaluate: vi.fn(),
  quickErrorMessage: (error: unknown, fallback = '查询失败，请稍后重试') =>
    error instanceof Error && error.message === 'INSUFFICIENT_POINTS' ? '积分不足，请充值' : fallback,
}));

vi.mock('./api/favorites', () => ({
  createFavoriteByKey: vi.fn(),
  deleteFavoriteByKey: vi.fn(),
  listFavorites: vi.fn(),
}));

vi.mock('./api/wallet', () => ({
  getWallet: vi.fn(),
}));

vi.mock('./auth/AuthProvider', () => ({
  useAuth: vi.fn(),
}));

vi.mock('./hooks/useWorkspace', () => ({
  useWorkspace: vi.fn(),
}));

// App 集成测试只保留 WorkspaceTabs / KolRecommendPanel / QuickFeatureCacheProvider 为真实组件，
// 其余重组件全部替身化，避免牵扯会话流、报告等无关模块。
vi.mock('./components/SessionList', () => ({ default: () => null }));
vi.mock('./components/MobileWorkspaceNav', () => ({ default: () => null }));
vi.mock('./components/ChatArea', () => ({ default: () => <div>会话区</div> }));
vi.mock('./components/FavoritesPanel', () => ({ default: () => <div>收藏面板</div> }));
vi.mock('./components/UniversalReport', async importOriginal => {
  // 保留真实命名导出（EvaluatePanel 依赖 MarkdownBlock），仅替身化默认导出。
  const actual = await importOriginal<typeof import('./components/UniversalReport')>();
  return { ...actual, default: () => null };
});
vi.mock('./components/RechargeModal', () => ({ default: () => null }));
vi.mock('./components/AdminPanel', () => ({ default: () => null }));
vi.mock('./components/KolDetailView', () => ({ default: () => null }));

const mockGetTopPosts = vi.mocked(getTopPosts);
const mockGetKolRecommendations = vi.mocked(getKolRecommendations);
const mockPostEvaluate = vi.mocked(postEvaluate);
const mockListFavorites = vi.mocked(listFavorites);
const mockGetWallet = vi.mocked(getWallet);
const mockUseAuth = vi.mocked(useAuth);
const mockUseWorkspace = vi.mocked(useWorkspace);

const XHS_RESULT: ApiQuickTopPosts = {
  items: [
    {
      title: '小红书爆贴标题',
      nickname: '小红书作者',
      interact: 120_000,
      like: 80_000,
      comment: 9_000,
      collect: 6_000,
      publish_time: '2026-07-01T10:00:00Z',
      url: 'https://example.com/xhs-1',
      platform: 'xiaohongshu',
    },
  ],
  points_cost: 10,
};

const DY_RESULT: ApiQuickTopPosts = {
  items: [
    {
      title: '抖音爆贴标题',
      nickname: '抖音作者',
      interact: 88_000,
      like: 60_000,
      comment: 5_000,
      collect: 2_000,
      publish_time: '2026-07-02T10:00:00Z',
      url: 'https://example.com/dy-1',
      platform: 'douyin',
    },
  ],
  points_cost: 30,
};

const KOL_RESULT: ApiQuickKolRecommendations = {
  items: [
    {
      platform: 'xiaohongshu',
      kw_uid: 'uid-1',
      nickname: '推荐达人甲',
      fans: 125_000,
      price: 30_000,
      engagement_rate: 5.2,
      score: 88,
      city: '上海',
      tags: ['美食'],
    },
  ],
  points_cost: 20,
};

const EVALUATE_RESULT = { title: '火锅活动评估', analysis_markdown: '**热度很高**' };

type QuickTab = 'posts-xhs' | 'posts-dy' | 'kol' | 'evaluate';

// 最小 Tab 容器：复刻 App.tsx 的快捷 Tab 结构——Provider 挂在容器外层，
// 四个快捷面板按 active tab 条件渲染（切 Tab 即卸载面板），
// 以此在集成层验证「切换 Tab 不丢状态、不重复请求」。
function QuickTabsHarness() {
  const [tab, setTab] = useState<QuickTab>('posts-xhs');
  return (
    <QuickFeatureCacheProvider userId="test-user">
      <div>
        <button type="button" role="tab" aria-selected={tab === 'posts-xhs'} onClick={() => setTab('posts-xhs')}>
          小红书爆贴
        </button>
        <button type="button" role="tab" aria-selected={tab === 'posts-dy'} onClick={() => setTab('posts-dy')}>
          抖音爆贴
        </button>
        <button type="button" role="tab" aria-selected={tab === 'kol'} onClick={() => setTab('kol')}>
          达人推荐
        </button>
        <button type="button" role="tab" aria-selected={tab === 'evaluate'} onClick={() => setTab('evaluate')}>
          活动评估
        </button>
      </div>
      {tab === 'posts-xhs' && <TopPostsPanel platform="xiaohongshu" />}
      {tab === 'posts-dy' && <TopPostsPanel platform="douyin" />}
      {tab === 'kol' && <KolRecommendPanel onSelectKol={() => undefined} />}
      {tab === 'evaluate' && <EvaluatePanel />}
    </QuickFeatureCacheProvider>
  );
}

function switchTab(name: string) {
  fireEvent.click(screen.getByRole('tab', { name }));
}

async function flush() {
  await act(async () => {});
}

async function advanceDebounce(ms = 800) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function submitEvaluate() {
  fireEvent.change(screen.getByLabelText('活动名称'), { target: { value: '火锅节活动' } });
  const kolInput = screen.getByLabelText('达人名称');
  fireEvent.change(kolInput, { target: { value: '达人甲' } });
  fireEvent.keyDown(kolInput, { key: 'Enter' });
  fireEvent.click(screen.getByRole('button', { name: '开始评估' }));
}

describe('快捷 Tab 容器集成（QuickFeatureCacheProvider）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetTopPosts.mockImplementation(async platform =>
      platform === 'xiaohongshu' ? XHS_RESULT : DY_RESULT,
    );
    mockGetKolRecommendations.mockResolvedValue(KOL_RESULT);
    mockPostEvaluate.mockResolvedValue(EVALUATE_RESULT);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('小红书/抖音爆贴按平台分键缓存，切 Tab 回来不重复请求', async () => {
    render(<QuickTabsHarness />);

    // 小红书爆贴：手动查询后列表出现
    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    expect(await screen.findByText('小红书爆贴标题')).toBeTruthy();
    expect(mockGetTopPosts).toHaveBeenCalledTimes(1);
    expect(mockGetTopPosts).toHaveBeenLastCalledWith('xiaohongshu');

    // 切到抖音爆贴：独立查询，读不到小红书缓存
    switchTab('抖音爆贴');
    expect(screen.queryByText('小红书爆贴标题')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    expect(await screen.findByText('抖音爆贴标题')).toBeTruthy();
    expect(mockGetTopPosts).toHaveBeenCalledTimes(2);
    expect(mockGetTopPosts).toHaveBeenLastCalledWith('douyin');

    // 经第三个 Tab 切回小红书：结果直接恢复，请求数不变
    switchTab('活动评估');
    switchTab('小红书爆贴');
    expect(screen.getByText('小红书爆贴标题')).toBeTruthy();
    expect(screen.getByText(/消耗 10 积分/)).toBeTruthy();
    expect(mockGetTopPosts).toHaveBeenCalledTimes(2);

    // 抖音同理
    switchTab('抖音爆贴');
    expect(screen.getByText('抖音爆贴标题')).toBeTruthy();
    expect(screen.getByText(/消耗 30 积分/)).toBeTruthy();
    expect(mockGetTopPosts).toHaveBeenCalledTimes(2);
  });

  it('达人推荐切 Tab 后预算与名单保留，不重复请求', async () => {
    vi.useFakeTimers();
    render(<QuickTabsHarness />);
    switchTab('达人推荐');

    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await advanceDebounce();
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
    expect(screen.getByText('推荐达人甲')).toBeTruthy();

    // 切走再切回：预算、名单与积分消耗从缓存恢复
    switchTab('小红书爆贴');
    switchTab('达人推荐');
    expect(screen.getByText('推荐达人甲')).toBeTruthy();
    expect(screen.getByText('¥5.0万')).toBeTruthy();
    expect(screen.getByText('上次消耗 20 积分', { exact: false })).toBeTruthy();

    // 防抖窗口过后也不发起新请求
    await advanceDebounce();
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
  });

  it('活动评估切 Tab 后表单与报告保留，不重复提交', async () => {
    render(<QuickTabsHarness />);
    switchTab('活动评估');

    submitEvaluate();
    expect(await screen.findByText('火锅活动评估')).toBeTruthy();
    expect(mockPostEvaluate).toHaveBeenCalledTimes(1);

    switchTab('抖音爆贴');
    switchTab('活动评估');

    // 报告从缓存恢复展示，不再发起评估请求
    expect(screen.getByText('火锅活动评估')).toBeTruthy();
    expect(screen.getByText(/热度很高/)).toBeTruthy();
    expect(mockPostEvaluate).toHaveBeenCalledTimes(1);

    // 重新评估回到表单，已填写的活动名与达人名单也保留
    fireEvent.click(screen.getByRole('button', { name: '重新评估' }));
    expect((screen.getByLabelText('活动名称') as HTMLInputElement).value).toBe('火锅节活动');
    expect(screen.getByText('达人甲')).toBeTruthy();
  });

  it('四个快捷 Tab 状态共存：全部查询后循环切换均不丢状态、不重复请求', async () => {
    vi.useFakeTimers();
    render(<QuickTabsHarness />);

    // 依次在四个 Tab 完成一次成功查询
    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await flush();
    expect(screen.getByText('小红书爆贴标题')).toBeTruthy();

    switchTab('抖音爆贴');
    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await flush();
    expect(screen.getByText('抖音爆贴标题')).toBeTruthy();

    switchTab('达人推荐');
    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await advanceDebounce();
    expect(screen.getByText('推荐达人甲')).toBeTruthy();

    switchTab('活动评估');
    submitEvaluate();
    await flush();
    expect(screen.getByText('火锅活动评估')).toBeTruthy();

    // 循环切回每个 Tab：四个页面的状态同时共存，请求数全部不变
    switchTab('小红书爆贴');
    expect(screen.getByText('小红书爆贴标题')).toBeTruthy();

    switchTab('抖音爆贴');
    expect(screen.getByText('抖音爆贴标题')).toBeTruthy();

    switchTab('达人推荐');
    expect(screen.getByText('推荐达人甲')).toBeTruthy();
    await advanceDebounce();

    switchTab('活动评估');
    expect(screen.getByText('火锅活动评估')).toBeTruthy();

    expect(mockGetTopPosts).toHaveBeenCalledTimes(2);
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
    expect(mockPostEvaluate).toHaveBeenCalledTimes(1);
  });
});

// ---- App 级集成：Provider 挂载位置 ----

// 可变 workspace：activeSession 通过 ref 控制，配合 rerender 模拟「会话被删除/恢复」。
const workspaceRef: { current: { activeSession: unknown } } = {
  current: { activeSession: null },
};

function workspaceValue() {
  return {
    sessions: [],
    activeSessionId: null,
    activeSession: workspaceRef.current.activeSession,
    loading: false,
    error: null,
    taskRuntime: undefined,
    isAnalyzing: false,
    isClarifying: false,
    cancelRequested: false,
    createSession: vi.fn(),
    selectSession: vi.fn(),
    updateSession: vi.fn(),
    deleteSession: vi.fn(),
    appendMessage: vi.fn(),
    cancelActiveTask: vi.fn(),
    retryMessage: vi.fn(),
    retryFollowups: vi.fn(),
    setAnalysisReport: vi.fn(),
    markArtifactSeen: vi.fn(),
  };
}

describe('App 集成：无活跃会话时快捷缓存不随条件块卸载', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetKolRecommendations.mockResolvedValue(KOL_RESULT);
    mockListFavorites.mockResolvedValue([]);
    mockGetWallet.mockResolvedValue({ available: 100 } as never);
    mockUseAuth.mockReturnValue({
      user: { id: 'user-1', nickname: '测试用户', role: 'user' },
      status: 'authenticated',
      logout: vi.fn(),
    } as never);
    mockUseWorkspace.mockImplementation(() => workspaceValue() as never);
  });

  it('快捷 Tab 查询后切到「智能会话」（无会话空态）再切回，缓存不丢、不重复请求', async () => {
    vi.useFakeTimers();
    workspaceRef.current = { activeSession: { id: 'session-1' } };
    const view = render(<App />);

    // 切到达人推荐并完成一次查询
    fireEvent.click(screen.getByRole('tab', { name: '达人推荐' }));
    fireEvent.click(screen.getByRole('button', { name: /查询\/刷新/ }));
    await advanceDebounce();
    expect(screen.getByText('推荐达人甲')).toBeTruthy();
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);

    // 会话被删除（activeSession = null），当前仍在快捷 Tab，面板不受影响
    workspaceRef.current = { activeSession: null };
    view.rerender(<App />);
    expect(screen.getByText('推荐达人甲')).toBeTruthy();

    // 切到「智能会话」：无活跃会话 → 条件块整体卸载
    // （修复前 Provider 挂在条件块内，随之一并卸载、缓存全丢）
    fireEvent.click(screen.getByRole('tab', { name: '智能会话' }));
    expect(screen.getByText('请选择或新建一个 KOL 筛选会话')).toBeTruthy();

    // 恢复会话后切回达人推荐：结果与积分消耗仍在，不重复请求
    workspaceRef.current = { activeSession: { id: 'session-2' } };
    view.rerender(<App />);
    fireEvent.click(screen.getByRole('tab', { name: '达人推荐' }));
    expect(screen.getByText('推荐达人甲')).toBeTruthy();
    expect(screen.getByText('上次消耗 20 积分', { exact: false })).toBeTruthy();
    await advanceDebounce();
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
  });
});
