/* 전 화면 공용 세션·CSRF·동적 권한 UI */
'use strict';

window.AuthUI = (() => {
  let cachedSession = null;

  function csrfToken() {
    const item = document.cookie.split('; ').find(part => part.split('=', 1)[0].endsWith('askseoul_csrf'));
    return item ? decodeURIComponent(item.slice(item.indexOf('=') + 1)) : '';
  }

  async function api(path, options = {}) {
    const method = (options.method || 'GET').toUpperCase();
    const headers = new Headers(options.headers || {});
    if (options.body && !headers.has('content-type')) headers.set('content-type', 'application/json');
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const csrf = csrfToken();
      if (csrf) headers.set('x-csrf-token', csrf);
    }
    const response = await fetch(path, { credentials: 'same-origin', ...options, method, headers });
    const body = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) {
      const error = new Error(body?.detail || body?.title || `HTTP ${response.status}`);
      error.status = response.status; error.body = body;
      if (response.status === 403 && body?.title === 'access blocked') {
        // 자동/수동 차단 — '[사유]로 정지되었습니다…' 전면 안내.
        showBlockedOverlay(body.detail || '접근이 정지되었습니다.');
        throw error;
      }
      if (response.status === 401 && !location.pathname.startsWith('/auth/')) {
        let target = `/auth/login?next=${encodeURIComponent(location.pathname + location.search)}`;
        if (body?.title === 'session expired') {
          target += '&expired=1';
          if (body.expired_at) target += `&at=${encodeURIComponent(body.expired_at)}`;
        }
        location.href = target;
      }
      throw error;
    }
    return body;
  }

  function showBlockedOverlay(message) {
    if (document.getElementById('blocked-overlay')) return;
    const overlay = document.createElement('div');
    overlay.id = 'blocked-overlay';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;display:grid;place-items:center;'
      + 'background:rgba(11,18,32,.72);backdrop-filter:blur(3px);';
    overlay.innerHTML = `<div style="max-width:26rem;background:#fff;border-radius:14px;padding:26px;text-align:center">
      <div style="font-size:2rem;margin-bottom:.6rem">&#9940;</div>
      <p style="font-size:13px;line-height:1.7;color:#171c26">${escapeHtml(message)}</p>
    </div>`;
    document.body.appendChild(overlay);
  }

  async function session(refresh = false) {
    if (!refresh && cachedSession) return cachedSession;
    cachedSession = await api('/api/v1/auth/session');
    return cachedSession;
  }

  function initials(nickname) {
    return Array.from(nickname || 'AS').slice(0, 2).join('').toUpperCase();
  }

  function profileCard(user) {
    return `<a class="user-profile-card" href="/profile" title="프로필 열기">
      <span class="profile-avatar">${escapeHtml(initials(user.nickname))}</span>
      <span class="profile-copy"><b>${escapeHtml(user.nickname)}</b>
        <span>${escapeHtml(user.masked_id)} · ${escapeHtml(user.role_label)}</span></span>
    </a>`;
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => (
      {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
    ));
  }

  function applyPermissions(user) {
    const allowed = new Set(user?.allowed_pages || []);
    // '내 화면 설정'에서 사용자가 스스로 숨긴 페이지(표시용, 접근권한과 무관).
    const hidden = new Set(user?.hidden_pages || []);
    document.querySelectorAll('[data-page-key]').forEach(element => {
      const keys = element.dataset.pageKey.split(',').map(v => v.trim());
      const accessible = keys.some(key => allowed.has(key));
      // hidden_pages(표시용 숨김)는 사이드 네비·탭바 같은 '메뉴' 항목에만 적용한다.
      // 콘텐츠 섹션(#billing 등)은 접근권으로만 게이팅해 빈 패널이 뜨지 않게 한다.
      const isMenu = element.closest('.console-nav, .tabbar, .side');
      const menuHidden = Boolean(isMenu) && keys.every(key => hidden.has(key));
      element.hidden = !accessible || menuHidden;
    });
  }

  function decorate(user) {
    applyPermissions(user);
    const ws = document.querySelector('.side .ws');
    if (ws && !document.querySelector('.user-profile-card')) {
      ws.insertAdjacentHTML('afterend', profileCard(user));
    }
    document.querySelectorAll('.avatar').forEach(el => {
      el.textContent = initials(user.nickname);
      el.title = `${user.nickname} · ${user.role_label}`;
      el.style.cursor = 'pointer';
      el.onclick = () => location.href = '/profile';
    });
    const slot = document.getElementById('profile-action-slot');
    if (slot) {
      slot.innerHTML = `<a class="profile-top-action" href="/profile">
        <span class="profile-avatar">${escapeHtml(initials(user.nickname))}</span>
        ${escapeHtml(user.nickname)} · ${escapeHtml(user.role_label)}</a>`;
    }
  }

  // 운영자·최고관리자가 하위 역할 화면을 미리 보는 '권한별 화면 관리' 미리보기.
  // 화면 표시(네비·차트 편집 가능 여부)만 바꾸며 서버 권한·데이터는 그대로다.
  const PREVIEW_ROLES = {
    guest: { role_label: '게스트', can_edit_charts: false, allowed_pages: ['catalog', 'profile', 'billing'] },
    member: { role_label: '일반회원', can_edit_charts: true, allowed_pages: ['catalog', 'charts', 'profile', 'billing'] },
  };

  function activePreviewRole(user) {
    const target = new URLSearchParams(location.search).get('preview');
    const onDataScreen = ['/catalog', '/charts'].includes(location.pathname);
    const isOperator = ['operator', 'admin'].includes(user && user.role);
    return onDataScreen && isOperator && PREVIEW_ROLES[target] ? target : null;
  }

  function applyPreview(user) {
    const target = activePreviewRole(user);
    if (!target) return user;
    showPreviewBanner(PREVIEW_ROLES[target].role_label);
    return { ...user, ...PREVIEW_ROLES[target], role: target, previewing: target };
  }

  function showPreviewBanner(label) {
    if (document.getElementById('preview-banner')) return;
    const bar = document.createElement('div');
    bar.id = 'preview-banner';
    bar.style.cssText = 'position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:9998;'
      + 'display:flex;align-items:center;gap:12px;padding:10px 16px;border-radius:999px;'
      + 'background:#1c5cab;color:#fff;font:13px/1.4 system-ui,sans-serif;box-shadow:0 10px 34px rgba(8,16,32,.4)';
    const text = document.createElement('span');
    text.textContent = `\u{1F50D} ${label} 권한 화면 미리보기 (운영자 미리보기)`;
    const exit = document.createElement('button');
    exit.type = 'button';
    exit.textContent = '미리보기 종료';
    exit.style.cssText = 'border:1px solid rgba(255,255,255,.55);background:transparent;color:#fff;'
      + 'border-radius:7px;padding:4px 11px;cursor:pointer;font:inherit';
    exit.addEventListener('click', () => {
      const url = new URL(location.href);
      url.searchParams.delete('preview');
      location.href = url.pathname + url.search;
    });
    bar.append(text, exit);
    document.body.appendChild(bar);
  }

  async function bootstrapProtected() {
    const state = await session();
    if (state.authenticated) {
      const user = applyPreview(state.user);
      decorate(user);
      return { ...state, user };
    }
    return state;
  }

  async function bootstrapLanding() {
    const state = await session().catch(() => ({ authenticated: false }));
    const slot = document.getElementById('landing-auth-slot');
    if (slot) {
      slot.innerHTML = state.authenticated
        ? `<a href="/profile">${escapeHtml(state.user.nickname)} · ${escapeHtml(state.user.role_label)}</a>`
        : '<a href="/auth/login">로그인</a><a href="/auth/register">회원가입</a>';
    }
    const use = document.getElementById('use-solution');
    if (use) use.addEventListener('click', event => {
      event.preventDefault();
      if (!state.authenticated) {
        location.href = '/auth/login?next=%2Fcatalog';
        return;
      }
      const allowed = new Set(state.user.allowed_pages || []);
      location.href = allowed.has('catalog') ? '/catalog' : '/profile?denied=1';
    });
    return state;
  }

  async function logout() {
    await api('/api/v1/auth/logout', { method: 'POST' });
    cachedSession = null;
    location.href = '/';
  }

  return { api, session, decorate, bootstrapProtected, bootstrapLanding, logout, escapeHtml, csrfToken };
})();
