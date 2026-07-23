import { afterEach, describe, expect, it, vi } from 'vitest';

import { setAccessToken } from './client';
import { getKolDetail, getKolRecommendations, getTopPosts, postEvaluate } from './quick';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

describe('quick api client', () => {
  afterEach(() => {
    setAccessToken(null);
    vi.unstubAllGlobals();
  });

  it('assembles the kol-recommendations query with budget and platforms', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], points_cost: 20 }));
    vi.stubGlobal('fetch', fetchMock);
    setAccessToken('token');

    const result = await getKolRecommendations({ budget: 50_000, platforms: ['xiaohongshu', 'douyin'] });

    expect(result.points_cost).toBe(20);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/v1/quick/kol-recommendations?budget=50000&platforms=xiaohongshu%2Cdouyin');
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer token');
  });

  it('omits the platforms param when not provided', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], points_cost: 20 }));
    vi.stubGlobal('fetch', fetchMock);
    setAccessToken('token');

    await getKolRecommendations({ budget: 10_000 });

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe('/api/v1/quick/kol-recommendations?budget=10000');
  });

  it('assembles the kol-detail query with platform, kw_uid and nickname', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: {}, posts: [], points_cost: 20 }));
    vi.stubGlobal('fetch', fetchMock);
    setAccessToken('token');

    await getKolDetail({ platform: 'xiaohongshu', kw_uid: 'uid-1', nickname: '达人甲' });

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain('/api/v1/quick/kol-detail?');
    expect(url).toContain('platform=xiaohongshu');
    expect(url).toContain('kw_uid=uid-1');
    expect(url).toContain(`nickname=${encodeURIComponent('达人甲')}`);
  });

  it('assembles the top-posts query with platform', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], points_cost: 10 }));
    vi.stubGlobal('fetch', fetchMock);
    setAccessToken('token');

    await getTopPosts('douyin');

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe('/api/v1/quick/top-posts?platform=douyin');
  });

  it('posts the evaluate request as JSON with activity name and kol names', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ title: '评估', analysis_markdown: '**ok**' }));
    vi.stubGlobal('fetch', fetchMock);
    setAccessToken('token');

    const result = await postEvaluate({ activityName: '火锅节活动', kolNames: ['达人甲', '达人乙'] });

    expect(result.title).toBe('评估');
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/v1/quick/evaluate');
    expect(init.method).toBe('POST');
    expect(init.body).toBe(JSON.stringify({ activity_name: '火锅节活动', kol_names: ['达人甲', '达人乙'] }));
    const headers = new Headers(init.headers);
    expect(headers.get('Authorization')).toBe('Bearer token');
    expect(headers.get('Content-Type')).toBe('application/json');
  });

  it('throws the backend detail when the evaluate request fails', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: 'INSUFFICIENT_POINTS' }, 409));
    vi.stubGlobal('fetch', fetchMock);
    setAccessToken('token');

    await expect(postEvaluate({ activityName: '活动', kolNames: ['达人甲'] })).rejects.toThrow('INSUFFICIENT_POINTS');
  });

  it('throws the backend detail for insufficient points', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: 'INSUFFICIENT_POINTS' }, 409));
    vi.stubGlobal('fetch', fetchMock);
    setAccessToken('token');

    await expect(getTopPosts('xiaohongshu')).rejects.toThrow('INSUFFICIENT_POINTS');
  });
});
