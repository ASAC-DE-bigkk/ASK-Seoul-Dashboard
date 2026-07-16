'use strict';

let profileUser;
let selectedPlan;

function profileMessage(text, kind = 'info') {
  const box = document.getElementById('message');
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
  document.getElementById('nickname').value = user.nickname;
  document.getElementById('billing').hidden = !(user.allowed_pages || []).includes('billing');
  AuthUI.decorate(user);
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
  await Promise.all([loadPreferences(), loadBilling()]);

  document.querySelectorAll('[data-logout]').forEach(button => button.onclick = AuthUI.logout);
  document.querySelectorAll('[data-close-modal]').forEach(button => button.onclick = () => {
    document.getElementById('payment-modal').hidden = true;
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
      profileMessage(result.detail, result.notification.delivered ? 'good' : 'warn');
      await loadBilling();
    } catch (error) { profileMessage(error.message, 'bad'); }
    finally { button.disabled = false; }
  };
}

bootProfile().catch(error => profileMessage(error.message, 'bad'));
