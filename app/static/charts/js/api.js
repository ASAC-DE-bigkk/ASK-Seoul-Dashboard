/* charts API 클라이언트 — problem+json 을 Error 로 변환하는 얇은 fetch 래퍼 */
'use strict';

const API = (() => {
  const BASE = '/api/v1/charts';

  async function call(path, opts = {}) {
    const res = await fetch(BASE + path, {
      headers: { 'content-type': 'application/json' },
      ...opts,
    });
    if (res.status === 204) return null;
    const body = await res.json().catch(() => null);
    if (!res.ok) {
      const detail = body && (body.detail || body.title) || `HTTP ${res.status}`;
      const err = new Error(detail);
      err.status = res.status;
      throw err;
    }
    return body;
  }

  return {
    meta: () => call('/meta'),
    sources: (domain = 'all') => call(`/sources?domain=${encodeURIComponent(domain)}`),
    source: name => call(`/sources/${encodeURIComponent(name)}`),
    query: spec => call('/query', { method: 'POST', body: JSON.stringify(spec) }),
    pages: () => call('/layouts'),
    page: id => call(`/layouts/${id}`),
    createPage: name => call('/layouts', { method: 'POST', body: JSON.stringify({ name }) }),
    patchPage: (id, patch) => call(`/layouts/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
    deletePage: id => call(`/layouts/${id}`, { method: 'DELETE' }),
    duplicatePage: id => call(`/layouts/${id}/duplicate`, { method: 'POST' }),
    reorderPages: ids => call('/layouts-reorder', { method: 'POST', body: JSON.stringify({ ids }) }),
  };
})();
