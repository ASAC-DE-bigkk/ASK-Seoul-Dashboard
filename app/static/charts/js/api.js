/* charts API 클라이언트 — problem+json 을 Error 로 변환하는 얇은 fetch 래퍼 */
'use strict';

const API = (() => {
  const BASE = '/api/v1/charts';
  let querySchedule = Promise.resolve();
  let nextQueryAt = 0;

  async function call(path, opts = {}) {
    return AuthUI.api(BASE + path, opts);
  }

  function query(spec) {
    const start = querySchedule.then(async () => {
      const wait = Math.max(0, nextQueryAt - Date.now());
      if (wait) await new Promise(resolve => setTimeout(resolve, wait));
      nextQueryAt = Date.now() + 150;
    });
    querySchedule = start.catch(() => {});
    return start.then(() =>
      call('/query', { method: 'POST', body: JSON.stringify(spec) }));
  }

  return {
    meta: () => call('/meta'),
    sources: (domain = 'all') => call(`/sources?domain=${encodeURIComponent(domain)}`),
    source: name => call(`/sources/${encodeURIComponent(name)}`),
    query,
    pages: () => call('/layouts'),
    page: id => call(`/layouts/${id}`),
    createPage: name => call('/layouts', { method: 'POST', body: JSON.stringify({ name }) }),
    patchPage: (id, patch) => call(`/layouts/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
    deletePage: id => call(`/layouts/${id}`, { method: 'DELETE' }),
    duplicatePage: id => call(`/layouts/${id}/duplicate`, { method: 'POST' }),
    reorderPages: ids => call('/layouts-reorder', { method: 'POST', body: JSON.stringify({ ids }) }),
  };
})();
