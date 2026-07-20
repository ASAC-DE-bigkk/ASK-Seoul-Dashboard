'use strict';

let profileUser;
let selectedPlan;
let mfaSetupToken = '';
let currentRecoveryCodes = [];

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
function statusBadge(value) {
  const labels = {pending:'승인 대기',approved:'승인',rejected:'거절'};
  const cls = value === 'approved' ? 'good' : value === 'pending' ? 'warn' : '';
  return `<span class="badge ${cls}">${AuthUI.escapeHtml(labels[value] || value)}</span>`;
}

function renderUser(user) {
  profileUser = user;
  document.getElementById('side-avatar').textContent = initials(user.nickname);
  document.getElementById('side-nickname').textContent = user.nickname;
  document.getElementById('side-identity').textContent = `${user.masked_id} · ${user.role_label}`;
  document.getElementById('profile-id').textContent = user.masked_id;
  document.getElementById('profile-role').innerHTML = `<span class="badge good">${AuthUI.escapeHtml(user.role_label)}</span>`;
  document.getElementById('profile-membership').textContent =
    ['operator','admin'].includes(user.role) ? '운영 권한 계정' : dateText(user.membership_ends_at);
  document.getElementById('profile-email').textContent = user.email;
  document.getElementById('profile-verified').textContent = user.email_verified ? '인증됨' : '미인증';
  document.getElementById('profile-login').textContent = dateText(user.last_login_at);
  document.getElementById('profile-mfa').innerHTML = user.mfa_enabled
    ? '<span class="badge good">사용 중</span>'
    : '<span class="badge warn">미설정</span>';
  document.getElementById('nickname').value = user.nickname;
  document.getElementById('billing').hidden = !(user.allowed_pages || []).includes('billing');
  AuthUI.decorate(user);
}

async function loadMfaStatus() {
  const status = await AuthUI.api('/api/v1/me/mfa');
  const enabled = Boolean(status.enabled);
  const badge = document.getElementById('mfa-status');
  badge.className = `badge ${enabled ? 'good' : 'warn'}`;
  badge.textContent = enabled ? 'MFA 사용 중' : 'MFA 미설정';
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
}

async function loadBilling() {
  if (!(profileUser.allowed_pages || []).includes('billing')) return;
  const [plans, requests] = await Promise.all([
    AuthUI.api('/api/v1/billing/plans'), AuthUI.api('/api/v1/billing/requests'),
  ]);
  const canRequest = ['guest','member'].includes(profileUser.role);
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
  document.getElementById('payment-history').innerHTML = requests.length ? requests.map(row => `<tr>
    <td>${dateText(row.requested_at)}</td><td>${AuthUI.escapeHtml(row.plan.label)}</td>
    <td>${statusBadge(row.status)}</td><td>${dateText(row.reviewed_at)}</td>
    <td>${AuthUI.escapeHtml(row.review_note || '-')}</td></tr>`).join('') :
    '<tr><td colspan="5">결제 요청 내역이 없습니다.</td></tr>';
}

async function bootProfile() {
  const state = await AuthUI.bootstrapProtected();
  renderUser(state.user);
  if (new URLSearchParams(location.search).get('denied')) {
    profileMessage('요청한 영역에 접근할 권한이 없습니다.', 'warn');
  }
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
    try {
      await AuthUI.api('/api/v1/me/preferences', {
        method:'PUT',
        body:JSON.stringify({
          ontology:{hidden_chart_types:hidden,default_domain:document.getElementById('default-domain').value},
          ui:{dense:document.getElementById('dense-ui').checked},
        }),
      });
      profileMessage('개인 온톨로지와 화면 설정을 저장했습니다.', 'good');
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
