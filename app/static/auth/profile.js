'use strict';

let profileUser;
let selectedPlan;
let mfaSetupToken = '';
let currentRecoveryCodes = [];

const PROFILE_TABS = ['account', 'security', 'sessions', 'display', 'billing'];
const TAB_ALIASES = { ontology: 'display', billing: 'billing', security: 'security', sessions: 'sessions' };
const PAGE_LABELS = {
  catalog: '데이터 마켓플레이스',
  charts: 'Charts Studio',
  billing: '결제',
  api_docs: 'API 문서',
  admin_users: '운영 콘솔 · 회원 관리',
  admin_access: '운영 콘솔 · 접근 관리',
  admin_payments: '운영 콘솔 · 결제 관리',
  admin_policies: '운영 콘솔 · 정책 관리',
  admin_audit: '운영 콘솔 · 감사 로그',
  admin_security: '운영 콘솔 · 자동 차단',
  service_health: '운영 콘솔 · 서비스 헬스',
};

function profileMessage(text, kind = 'info') {
  const box = document.getElementById('message');
  box.setAttribute('role', 'status'); box.setAttribute('aria-live', 'polite');
  box.className = `message ${kind}`; box.textContent = text; box.hidden = false;
  box.scrollIntoView({ behavior:'smooth', block:'nearest' });
}
function dateText(value) {
  return value ? new Intl.DateTimeFormat('ko-KR', { dateStyle:'medium', timeStyle:'short' }).format(new Date(value)) : '설정되지 않음';
}
function initials(value) { return Array.from(value || 'AS').slice(0, 2).join('').toUpperCase(); }
function badgeHtml(kind, label) {
  return `<span class="badge ${kind}"><span class="dot"></span>${AuthUI.escapeHtml(label)}</span>`;
}

function showTab(name) {
  const target = PROFILE_TABS.includes(name) ? name : (TAB_ALIASES[name] || 'account');
  const billingAllowed = (profileUser?.allowed_pages || []).includes('billing');
  // 사용자가 '내 화면 설정'에서 결제를 메뉴에서 숨겼으면 해시 직진입도 계정 탭으로 보낸다.
  const billingHidden = (profileUser?.hidden_pages || []).includes('billing');
  const resolved = target === 'billing' && (!billingAllowed || billingHidden) ? 'account' : target;
  PROFILE_TABS.forEach(tab => {
    const panel = document.getElementById(`panel-${tab}`);
    if (panel) panel.hidden = tab !== resolved;
  });
  document.querySelectorAll('#profile-tabbar [data-tab]').forEach(button => {
    button.classList.toggle('on', button.dataset.tab === resolved);
  });
  history.replaceState(null, '', `#${resolved}`);
}

function renderAccountBadges(user) {
  const badges = [];
  badges.push(badgeHtml('muted', `역할 · ${user.role_label}`));
  const statusMap = {
    active: ['good', '계정 활성'],
    pending: ['warn', '승인 대기'],
    suspended: ['bad', '계정 정지'],
    rejected: ['bad', '가입 거절'],
  };
  const [statusKind, statusLabel] = statusMap[user.status] || ['muted', `상태 · ${user.status}`];
  badges.push(badgeHtml(statusKind, statusLabel));
  badges.push(user.email_verified
    ? badgeHtml('good', '이메일 인증됨')
    : badgeHtml('warn', '이메일 미인증'));
  badges.push(user.mfa_enabled
    ? badgeHtml('good', '2단계 인증 사용 중')
    : badgeHtml('warn', '2단계 인증 미설정'));
  badges.push(`<span id="pass-badge">${
    ['operator','admin'].includes(user.role)
      ? badgeHtml('muted', '운영 권한 계정')
      : badgeHtml('muted', '이용권 확인 중')
  }</span>`);
  document.getElementById('account-badges').innerHTML = badges.join('');
}

function renderUser(user) {
  profileUser = user;
  document.getElementById('side-avatar').textContent = initials(user.nickname);
  document.getElementById('side-nickname').textContent = user.nickname;
  document.getElementById('side-identity').textContent = `${user.masked_id} · ${user.role_label}`;
  document.getElementById('profile-id').textContent = user.masked_id;
  document.getElementById('profile-email').textContent = user.email;
  document.getElementById('profile-login').textContent = dateText(user.last_login_at);
  document.getElementById('nickname').value = user.nickname;
  renderAccountBadges(user);
  const billingAllowed = (user.allowed_pages || []).includes('billing');
  document.getElementById('billing').hidden = !billingAllowed;
  document.getElementById('operator-access-note').hidden = !['operator','admin'].includes(user.role);
  AuthUI.decorate(user);
}

async function loadMfaStatus() {
  const status = await AuthUI.api('/api/v1/me/mfa');
  const enabled = Boolean(status.enabled);
  const badge = document.getElementById('mfa-status');
  const missingRequired = !enabled && status.required_for_role;
  badge.className = `badge ${enabled ? 'good' : missingRequired ? 'bad' : 'warn'}`;
  badge.textContent = enabled ? 'MFA 사용 중' : missingRequired ? 'MFA 필수 · 미설정' : 'MFA 미설정';
  document.getElementById('mfa-recovery-status').textContent = enabled
    ? `남은 복구 코드 ${status.recovery_codes_remaining}개`
    : status.required_for_role ? '이 권한은 MFA 설정이 필수입니다.' : '모든 계정에 설정을 권장합니다.';
  document.getElementById('mfa-current-code-field').hidden = !enabled;
  document.getElementById('mfa-setup-button').textContent = enabled ? 'MFA 기기 교체 시작' : 'MFA 설정 시작';
  document.getElementById('mfa-manage-panel').hidden = !enabled;
  document.getElementById('mfa-disable-button').disabled = enabled && status.required_for_role;
  if (enabled && status.required_for_role) {
    document.getElementById('mfa-disable-button').title = '권한 계정 MFA 강제 정책으로 해제할 수 없습니다.';
  }
  return status;
}

async function loadSessions() {
  const rows = await AuthUI.api('/api/v1/me/sessions');
  document.getElementById('session-list').innerHTML = rows.length ? rows.map(row => `<tr>
    <td>${row.current ? '<span class="badge good">현재 기기</span>' : '<span class="badge">다른 기기</span>'}</td>
    <td>${dateText(row.last_seen_at)}</td><td>${dateText(row.expires_at)}</td>
    <td><code>${AuthUI.escapeHtml(row.ip_fingerprint)}</code></td>
    <td><code>${AuthUI.escapeHtml(row.client_fingerprint)}</code></td>
    <td><button class="btn danger" type="button" data-session-id="${AuthUI.escapeHtml(row.id)}">${row.current ? '로그아웃' : '종료'}</button></td>
  </tr>`).join('') : '<tr><td colspan="6">활성 세션이 없습니다.</td></tr>';
  document.querySelectorAll('[data-session-id]').forEach(button => button.onclick = async () => {
    if (!confirm('이 로그인 세션을 종료할까요?')) return;
    button.disabled = true;
    try {
      const result = await AuthUI.api(`/api/v1/me/sessions/${encodeURIComponent(button.dataset.sessionId)}`, {method:'DELETE'});
      if (result.current) location.href = '/auth/login';
      else {
        profileMessage('선택한 세션을 종료했습니다.', 'good');
        await loadSessions();
      }
    } catch (error) { profileMessage(error.message, 'bad'); button.disabled = false; }
  });
}

function showRecoveryCodes(codes, sessionRevoked = false) {
  currentRecoveryCodes = [...codes];
  document.getElementById('recovery-codes').innerHTML = codes
    .map(code => `<code>${AuthUI.escapeHtml(code)}</code>`).join('');
  document.getElementById('finish-recovery').dataset.sessionRevoked = sessionRevoked ? '1' : '0';
  document.getElementById('recovery-modal').hidden = false;
  document.getElementById('copy-recovery-codes').focus();
}

async function copyText(value, successMessage) {
  try {
    await navigator.clipboard.writeText(value);
    profileMessage(successMessage, 'good');
  } catch (_error) {
    profileMessage('자동 복사에 실패했습니다. 값을 직접 선택해 복사하세요.', 'warn');
  }
}

async function loadPreferences() {
  const data = await AuthUI.api('/api/v1/me/preferences');
  const hidden = new Set(data.personal.ontology.hidden_chart_types || []);
  document.getElementById('default-domain').innerHTML = data.available_domains.map(domain =>
    `<option value="${AuthUI.escapeHtml(domain)}">${AuthUI.escapeHtml(domain === 'all' ? '전체' : domain)}</option>`
  ).join('');
  document.getElementById('default-domain').value = data.personal.ontology.default_domain || 'all';
  document.getElementById('dense-ui').checked = Boolean(data.personal.ui.dense);
  document.getElementById('chart-types').innerHTML = Object.entries(data.available_chart_types).map(([key,label]) =>
    `<label class="check data-card"><input type="checkbox" value="${AuthUI.escapeHtml(key)}" ${hidden.has(key) ? '' : 'checked'}>${AuthUI.escapeHtml(label)}</label>`
  ).join('');
  const hiddenPages = new Set(data.personal.ui.hidden_pages || []);
  const hidablePages = (profileUser.allowed_pages || []).filter(key => key !== 'profile');
  document.getElementById('page-visibility').innerHTML = hidablePages.length
    ? hidablePages.map(key =>
        `<label class="check data-card"><input type="checkbox" data-page-visibility value="${AuthUI.escapeHtml(key)}" ${hiddenPages.has(key) ? '' : 'checked'}>${AuthUI.escapeHtml(PAGE_LABELS[key] || key)}</label>`
      ).join('')
    : '<p class="desc">조정할 수 있는 페이지가 없습니다.</p>';
}

function renderPassBadge(summary) {
  const slot = document.getElementById('pass-badge');
  if (!slot) return;
  if (summary.operator_account) {
    slot.innerHTML = badgeHtml('muted', '운영 권한 계정');
  } else if (summary.active) {
    slot.innerHTML = badgeHtml('good', `이용권 ${summary.days_left}일 남음`);
  } else if (summary.ends_at) {
    slot.innerHTML = badgeHtml('bad', '이용권 만료');
  } else {
    slot.innerHTML = badgeHtml('muted', '이용권 없음');
  }
}

async function loadBilling() {
  if (!(profileUser.allowed_pages || []).includes('billing')) return;
  const [plans, summary] = await Promise.all([
    AuthUI.api('/api/v1/billing/plans'), AuthUI.api('/api/v1/billing/summary'),
  ]);
  renderPassBadge(summary);
  const passSlot = document.getElementById('active-pass');
  if (summary.active && summary.plan) {
    passSlot.innerHTML = `<div class="pass-card">
      <span class="pass-icon">&#127915;</span>
      <span><b>사용 중인 이용권 · ${AuthUI.escapeHtml(summary.plan.label)}</b>
      <small>종료 예정 ${dateText(summary.ends_at)}</small></span>
      <span class="days"><b>${Number(summary.days_left)}일</b><small>남음</small></span>
    </div>`;
  } else if (summary.active) {
    passSlot.innerHTML = `<div class="pass-card">
      <span class="pass-icon">&#127915;</span>
      <span><b>사용 중인 이용권</b><small>종료 예정 ${dateText(summary.ends_at)}</small></span>
      <span class="days"><b>${Number(summary.days_left)}일</b><small>남음</small></span>
    </div>`;
  } else if (summary.operator_account) {
    passSlot.innerHTML = '<div class="message info">운영 권한 계정은 이용권 없이 모든 데이터 화면을 사용합니다.</div>';
  } else if (summary.ends_at) {
    passSlot.innerHTML = `<div class="message bad">이용권이 ${dateText(summary.ends_at)}에 만료되었습니다. 새 이용권을 요청하세요.</div>`;
  } else {
    passSlot.innerHTML = '<div class="message info">사용 중인 이용권이 없습니다. 아래 요금제에서 요청할 수 있습니다.</div>';
  }
  document.getElementById('pending-request').innerHTML = summary.pending_request
    ? `<div class="message warn">승인 대기 중인 요청 — ${AuthUI.escapeHtml(summary.pending_request.plan_label)} (${dateText(summary.pending_request.requested_at)} 요청). 운영자 승인 후 반영됩니다.</div>`
    : '';
  const canRequest = ['guest','member'].includes(profileUser.role) && !summary.pending_request;
  document.getElementById('plans').innerHTML = plans.map(plan => `<article class="plan">
    <b>${AuthUI.escapeHtml(plan.label)}</b>
    <div class="price">${Number(plan.price_amount).toLocaleString('ko-KR')} ${AuthUI.escapeHtml(plan.currency)}</div>
    <small>${plan.duration_days}일 연장 · 운영 승인형 모의 결제</small>
    <button class="btn primary" type="button" data-plan="${AuthUI.escapeHtml(plan.code)}" ${canRequest ? '' : 'disabled'}>요청하기</button>
  </article>`).join('');
  document.querySelectorAll('[data-plan]').forEach(button => button.onclick = () => {
    selectedPlan = plans.find(plan => plan.code === button.dataset.plan);
    document.getElementById('payment-summary').textContent =
      `${selectedPlan.label} · ${selectedPlan.duration_days}일 · ${Number(selectedPlan.price_amount).toLocaleString('ko-KR')} ${selectedPlan.currency}`;
    document.getElementById('payment-modal').hidden = false;
    document.getElementById('confirm-payment').focus();
  });
}

async function bootProfile() {
  const state = await AuthUI.bootstrapProtected();
  renderUser(state.user);
  if (new URLSearchParams(location.search).get('denied')) {
    profileMessage('요청한 영역에 접근할 권한이 없습니다.', 'warn');
  }
  showTab(location.hash.slice(1) || 'account');
  document.querySelectorAll('#profile-tabbar [data-tab]').forEach(button => {
    button.onclick = () => showTab(button.dataset.tab);
  });
  window.addEventListener('hashchange', () => showTab(location.hash.slice(1)));
  await Promise.all([loadPreferences(), loadBilling(), loadMfaStatus(), loadSessions()]);

  document.querySelectorAll('[data-logout]').forEach(button => button.onclick = AuthUI.logout);
  document.querySelectorAll('[data-close-modal]').forEach(button => button.onclick = () => {
    document.getElementById('payment-modal').hidden = true;
  });
  window.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !document.getElementById('payment-modal').hidden) {
      document.getElementById('payment-modal').hidden = true;
    }
  });
  document.getElementById('nickname-form').onsubmit = async event => {
    event.preventDefault();
    try {
      const user = await AuthUI.api('/api/v1/me/nickname', {
        method:'PATCH', body:JSON.stringify({nickname:event.currentTarget.nickname.value}),
      });
      renderUser(user); profileMessage('닉네임을 변경했습니다.', 'good');
      await loadBilling();
    } catch (error) { profileMessage(error.message, 'bad'); }
  };
  document.getElementById('password-form').onsubmit = async event => {
    event.preventDefault();
    try {
      const form = event.currentTarget;
      const result = await AuthUI.api('/api/v1/me/password', {
        method:'PATCH', body:JSON.stringify({
          current_password:form.current_password.value, new_password:form.new_password.value,
        }),
      });
      profileMessage(result.detail, 'good');
      setTimeout(() => location.href='/auth/login', 1200);
    } catch (error) { profileMessage(error.message, 'bad'); }
  };
  document.getElementById('mfa-setup-form').onsubmit = async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button[type=submit]'); button.disabled = true;
    try {
      const result = await AuthUI.api('/api/v1/me/mfa/setup', {
        method:'POST', body:JSON.stringify({
          current_password:form.current_password.value,
          current_code:form.current_code.value,
        }),
      });
      mfaSetupToken = result.setup_token;
      document.getElementById('mfa-secret').textContent = result.secret;
      document.getElementById('mfa-otpauth-link').href = result.otpauth_uri;
      document.getElementById('mfa-enroll-panel').hidden = false;
      document.getElementById('mfa-confirm-code').focus();
      profileMessage('인증 앱 등록 후 6자리 코드를 입력하세요.', 'info');
    } catch (error) { profileMessage(error.message, 'bad'); }
    finally { button.disabled = false; }
  };
  document.getElementById('copy-mfa-secret').onclick = () =>
    copyText(document.getElementById('mfa-secret').textContent, 'MFA 비밀키를 복사했습니다.');
  document.getElementById('mfa-confirm-form').onsubmit = async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button[type=submit]'); button.disabled = true;
    try {
      const result = await AuthUI.api('/api/v1/me/mfa/confirm', {
        method:'POST', body:JSON.stringify({setup_token:mfaSetupToken,code:form.code.value}),
      });
      mfaSetupToken = '';
      document.getElementById('mfa-secret').textContent = '';
      document.getElementById('mfa-otpauth-link').removeAttribute('href');
      showRecoveryCodes(result.recovery_codes, true);
    } catch (error) { profileMessage(error.message, 'bad'); button.disabled = false; }
  };
  document.getElementById('mfa-recovery-form').onsubmit = async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button[type=submit]'); button.disabled = true;
    try {
      const result = await AuthUI.api('/api/v1/me/mfa/recovery-codes', {
        method:'POST', body:JSON.stringify({
          current_password:form.current_password.value, code:form.code.value,
        }),
      });
      form.reset();
      showRecoveryCodes(result.recovery_codes, false);
    } catch (error) { profileMessage(error.message, 'bad'); }
    finally { button.disabled = false; }
  };
  document.getElementById('mfa-disable-form').onsubmit = async event => {
    event.preventDefault();
    if (!confirm('MFA를 해제하고 모든 로그인 세션을 종료할까요?')) return;
    const form = event.currentTarget;
    const button = form.querySelector('button[type=submit]'); button.disabled = true;
    try {
      const result = await AuthUI.api('/api/v1/me/mfa', {
        method:'DELETE', body:JSON.stringify({
          current_password:form.current_password.value, code:form.code.value,
        }),
      });
      profileMessage(result.detail, 'good');
      setTimeout(() => location.href='/auth/login', 1200);
    } catch (error) { profileMessage(error.message, 'bad'); button.disabled = false; }
  };
  document.getElementById('copy-recovery-codes').onclick = () =>
    copyText(currentRecoveryCodes.join('\n'), '복구 코드를 모두 복사했습니다.');
  document.getElementById('finish-recovery').onclick = event => {
    const revoked = event.currentTarget.dataset.sessionRevoked === '1';
    currentRecoveryCodes = [];
    document.getElementById('recovery-codes').textContent = '';
    document.getElementById('recovery-modal').hidden = true;
    if (revoked) location.href = '/auth/login?mfa=enabled';
    else loadMfaStatus().catch(error => profileMessage(error.message, 'bad'));
  };
  document.getElementById('revoke-other-sessions').onclick = async event => {
    if (!confirm('현재 기기를 제외한 모든 로그인 세션을 종료할까요?')) return;
    event.currentTarget.disabled = true;
    try {
      const result = await AuthUI.api('/api/v1/me/sessions/revoke-others', {method:'POST'});
      profileMessage(`${result.revoked}개의 다른 세션을 종료했습니다.`, 'good');
      await loadSessions();
    } catch (error) { profileMessage(error.message, 'bad'); }
    finally { event.currentTarget.disabled = false; }
  };
  document.getElementById('preference-form').onsubmit = async event => {
    event.preventDefault();
    const hidden = [...document.querySelectorAll('#chart-types input:not(:checked)')].map(item => item.value);
    const hiddenPages = [...document.querySelectorAll('#page-visibility input[data-page-visibility]:not(:checked)')]
      .map(item => item.value);
    try {
      await AuthUI.api('/api/v1/me/preferences', {
        method:'PUT',
        body:JSON.stringify({
          ontology:{hidden_chart_types:hidden,default_domain:document.getElementById('default-domain').value},
          ui:{dense:document.getElementById('dense-ui').checked,hidden_pages:hiddenPages},
        }),
      });
      profileMessage('내 화면 설정을 저장했습니다. 다음 화면 로드부터 메뉴에 반영됩니다.', 'good');
    } catch (error) { profileMessage(error.message, 'bad'); }
  };
  document.getElementById('confirm-payment').onclick = async () => {
    if (!selectedPlan) return;
    const button = document.getElementById('confirm-payment'); button.disabled = true;
    try {
      const result = await AuthUI.api('/api/v1/billing/requests', {
        method:'POST', body:JSON.stringify({plan_code:selectedPlan.code}),
      });
      document.getElementById('payment-modal').hidden = true;
      profileMessage(result.detail, result.notification.configured ? 'good' : 'warn');
      await loadBilling();
    } catch (error) { profileMessage(error.message, 'bad'); }
    finally { button.disabled = false; }
  };
}

bootProfile().catch(error => profileMessage(error.message, 'bad'));
