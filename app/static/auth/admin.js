'use strict';

let adminState;
let accessData;
let users = [];

function adminMessage(text, kind = 'info') {
  const box = document.getElementById('message');
  box.className = `message ${kind}`; box.textContent = text; box.hidden = false;
}
function dateText(value) {
  return value ? new Intl.DateTimeFormat('ko-KR', { dateStyle:'short', timeStyle:'short' }).format(new Date(value)) : '-';
}
function initials(value) { return Array.from(value || 'AS').slice(0, 2).join('').toUpperCase(); }
function badge(value) {
  const good = ['active','approved','delivered'].includes(value);
  const warn = ['pending','not_configured'].includes(value);
  const labels = {active:'활성',pending:'대기',suspended:'정지',rejected:'거절',approved:'승인'};
  return `<span class="badge ${good ? 'good' : warn ? 'warn' : ''}">${AuthUI.escapeHtml(labels[value] || value)}</span>`;
}
function showPanel(name) {
  const allowed = new Set(adminState.user.allowed_pages || []);
  const map = {users:'admin_users',access:'admin_access',payments:'admin_payments',policies:'admin_policies'};
  if (!allowed.has(map[name])) name = Object.keys(map).find(key => allowed.has(map[key])) || 'users';
  document.querySelectorAll('.admin-panel').forEach(panel => panel.hidden = panel.id !== `panel-${name}`);
  document.querySelectorAll('[data-tab]').forEach(link => link.classList.toggle('on', link.dataset.tab === name));
  if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
  ({users:loadUsers,access:loadAccess,payments:loadPayments,policies:loadPolicies}[name])().catch(error => adminMessage(error.message, 'bad'));
}

async function loadUsers() {
  const status = document.getElementById('user-status').value;
  users = await AuthUI.api(`/api/v1/admin/users${status ? `?status=${encodeURIComponent(status)}` : ''}`);
  document.getElementById('users-body').innerHTML = users.length ? users.map(user => `<tr>
    <td><b>${AuthUI.escapeHtml(user.nickname)}</b><br><small>${AuthUI.escapeHtml(user.email)}</small></td>
    <td title="${AuthUI.escapeHtml(user.id)}">${AuthUI.escapeHtml(user.masked_id)}</td>
    <td>${AuthUI.escapeHtml(user.role_label)}</td><td>${badge(user.status)}</td>
    <td>${user.email_verified ? '인증' : '미인증'}</td><td>${dateText(user.membership_ends_at)}</td>
    <td><button class="btn" type="button" data-edit-user="${AuthUI.escapeHtml(user.id)}">관리</button></td>
  </tr>`).join('') : '<tr><td colspan="7">조건에 맞는 회원이 없습니다.</td></tr>';
  document.querySelectorAll('[data-edit-user]').forEach(button => button.onclick = () => openUser(button.dataset.editUser));
}

async function loadAccess() {
  accessData = await AuthUI.api('/api/v1/admin/access');
  const roles = accessData.manageable_roles;
  document.getElementById('access-matrix').innerHTML = `<table><thead><tr><th>페이지</th>${roles.map(role => `<th>${AuthUI.escapeHtml(role)}</th>`).join('')}</tr></thead><tbody>
    ${accessData.pages.map(page => `<tr><td><b>${AuthUI.escapeHtml(page.label)}</b><br><small>${AuthUI.escapeHtml(page.path)}</small></td>
      ${roles.map(role => `<td><input type="checkbox" data-role="${role}" data-page="${AuthUI.escapeHtml(page.key)}" ${accessData.role_permissions[role]?.[page.key] ? 'checked' : ''}></td>`).join('')}</tr>`).join('')}
    </tbody></table><div class="toolbar">${roles.map(role => `<button class="btn primary" type="button" data-save-role="${role}">${AuthUI.escapeHtml(role)} 기본값 저장</button>`).join('')}</div>`;
  document.querySelectorAll('[data-save-role]').forEach(button => button.onclick = async () => {
    const role = button.dataset.saveRole;
    const permissions = [...document.querySelectorAll(`[data-role="${role}"]`)].map(input => ({page_key:input.dataset.page,allowed:input.checked}));
    try {
      await AuthUI.api(`/api/v1/admin/access/roles/${role}`, {method:'PUT',body:JSON.stringify({permissions})});
      adminMessage(`${role} 역할의 기본 접근 권한을 저장했습니다.`, 'good');
    } catch (error) { adminMessage(error.message, 'bad'); }
  });
}

async function openUser(id) {
  const user = users.find(item => item.id === id);
  if (!user) return;
  if (!accessData) accessData = await AuthUI.api('/api/v1/admin/access');
  const form = document.getElementById('user-form');
  form.public_id.value = user.id; form.role.value = user.role; form.status.value = user.status;
  form.membership_ends_at.value = user.membership_ends_at ? user.membership_ends_at.slice(0,16) : '';
  if (adminState.user.role === 'operator') {
    [...form.role.options].forEach(option => option.disabled = ['operator','admin'].includes(option.value));
  }
  document.getElementById('user-modal-title').textContent = `${user.nickname} · ${user.email} · ${user.masked_id}`;
  document.getElementById('user-permissions').innerHTML = accessData.pages.map(page => `<label class="data-card">
    <span class="k">${AuthUI.escapeHtml(page.label)}</span>
    <select data-user-page="${AuthUI.escapeHtml(page.key)}">
      <option value="" ${user.permission_overrides[page.key] === undefined ? 'selected' : ''}>역할 기본값</option>
      <option value="allow" ${user.permission_overrides[page.key] === true ? 'selected' : ''}>개별 허용</option>
      <option value="deny" ${user.permission_overrides[page.key] === false ? 'selected' : ''}>개별 차단</option>
    </select>
  </label>`).join('');
  document.getElementById('user-modal').hidden = false;
}

async function saveUser(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const patch = {role:form.role.value,status:form.status.value};
  if (form.membership_ends_at.value) patch.membership_ends_at = new Date(form.membership_ends_at.value).toISOString();
  try {
    await AuthUI.api(`/api/v1/admin/users/${form.public_id.value}`, {method:'PATCH',body:JSON.stringify(patch)});
    const permissions = [...document.querySelectorAll('[data-user-page]')]
      .filter(input => input.value)
      .map(input => ({page_key:input.dataset.userPage,allowed:input.value === 'allow'}));
    await AuthUI.api(`/api/v1/admin/access/users/${form.public_id.value}`, {method:'PUT',body:JSON.stringify({permissions})});
    document.getElementById('user-modal').hidden = true;
    adminMessage('회원 역할·상태·개별 접근 권한을 저장했습니다.', 'good');
    await loadUsers();
  } catch (error) { adminMessage(error.message, 'bad'); }
}

async function loadPayments() {
  const status = document.getElementById('payment-status').value;
  const rows = await AuthUI.api(`/api/v1/admin/payments?status=${encodeURIComponent(status)}`);
  document.getElementById('payments-body').innerHTML = rows.length ? rows.map(row => `<tr>
    <td>${dateText(row.requested_at)}</td><td>${AuthUI.escapeHtml(row.user.nickname)}<br><small>${AuthUI.escapeHtml(row.user.masked_id)}</small></td>
    <td>${AuthUI.escapeHtml(row.plan.label)} · ${row.plan.duration_days}일</td><td>${badge(row.status)}</td>
    <td>${row.notification_result?.delivered ? '전송됨' : row.notification_result?.configured ? '전송 실패' : '미설정'}</td>
    <td>${row.status === 'pending' ? `<button class="btn primary" data-review="${row.id}" data-approve="1">승인</button> <button class="btn danger" data-review="${row.id}" data-approve="0">거절</button>` : AuthUI.escapeHtml(row.review_note || '-')}</td>
  </tr>`).join('') : '<tr><td colspan="6">결제 요청이 없습니다.</td></tr>';
  document.querySelectorAll('[data-review]').forEach(button => button.onclick = async () => {
    const approve = button.dataset.approve === '1';
    const note = prompt(approve ? '승인 메모(선택)' : '거절 사유', '') ?? '';
    try {
      await AuthUI.api(`/api/v1/admin/payments/${button.dataset.review}/review`, {method:'POST',body:JSON.stringify({approve,note})});
      adminMessage(approve ? '결제 요청을 승인했습니다.' : '결제 요청을 거절했습니다.', 'good');
      await loadPayments();
    } catch (error) { adminMessage(error.message, 'bad'); }
  });
}

async function loadPolicies() {
  const rows = await AuthUI.api('/api/v1/admin/policies');
  document.getElementById('policies-body').innerHTML = rows.length ? rows.map(row => `<tr>
    <td>${AuthUI.escapeHtml(row.name)}</td><td>${AuthUI.escapeHtml(row.policy_type)} / ${AuthUI.escapeHtml(row.scope_type)} ${AuthUI.escapeHtml(row.scope_role || '')}</td>
    <td>${row.priority}</td><td><pre class="policy-json">${AuthUI.escapeHtml(JSON.stringify(row.config,null,2))}</pre></td><td>${badge(row.active ? 'active' : 'disabled')}</td>
  </tr>`).join('') : '<tr><td colspan="5">표시할 정책이 없습니다.</td></tr>';
  if (adminState.user.role === 'admin') await loadIpBlocks();
}

async function loadIpBlocks() {
  const rows = await AuthUI.api('/api/v1/admin/ip-blocks');
  document.getElementById('ip-body').innerHTML = rows.length ? rows.map(row => `<tr>
    <td>${AuthUI.escapeHtml(row.network)}</td><td>${AuthUI.escapeHtml(row.reason || '-')}</td><td>${dateText(row.expires_at)}</td>
    <td>${badge(row.active ? 'active' : 'disabled')}</td><td>${row.active ? `<button class="btn danger" data-disable-ip="${row.id}">해제</button>` : '-'}</td></tr>`).join('') :
    '<tr><td colspan="5">IP 차단 정책이 없습니다.</td></tr>';
  document.querySelectorAll('[data-disable-ip]').forEach(button => button.onclick = async () => {
    try { await AuthUI.api(`/api/v1/admin/ip-blocks/${button.dataset.disableIp}`, {method:'DELETE'}); await loadIpBlocks(); }
    catch (error) { adminMessage(error.message, 'bad'); }
  });
}

async function bootAdmin() {
  adminState = await AuthUI.bootstrapProtected();
  const user = adminState.user;
  document.getElementById('side-avatar').textContent = initials(user.nickname);
  document.getElementById('side-nickname').textContent = user.nickname;
  document.getElementById('side-identity').textContent = `${user.masked_id} · ${user.role_label}`;
  document.querySelectorAll('[data-admin-only]').forEach(element => element.hidden = user.role !== 'admin');
  if (user.role === 'operator') {
    const policyForm = document.getElementById('policy-form');
    [...policyForm.scope_type.options].forEach(option => option.disabled = option.value === 'system');
    [...policyForm.scope_role.options].forEach(option => option.disabled = ['operator','admin'].includes(option.value));
  }
  document.querySelectorAll('[data-logout]').forEach(button => button.onclick = AuthUI.logout);
  document.querySelectorAll('[data-tab]').forEach(link => link.onclick = event => { event.preventDefault(); showPanel(link.dataset.tab); });
  document.getElementById('refresh-users').onclick = () => loadUsers().catch(error => adminMessage(error.message,'bad'));
  document.getElementById('user-status').onchange = document.getElementById('refresh-users').onclick;
  document.getElementById('refresh-payments').onclick = () => loadPayments().catch(error => adminMessage(error.message,'bad'));
  document.getElementById('payment-status').onchange = document.getElementById('refresh-payments').onclick;
  document.getElementById('user-form').onsubmit = saveUser;
  document.querySelectorAll('[data-close-user]').forEach(button => button.onclick = () => document.getElementById('user-modal').hidden = true);
  document.getElementById('policy-form').onsubmit = async event => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      const payload = {
        name:form.name.value, policy_type:form.policy_type.value, scope_type:form.scope_type.value,
        scope_role:form.scope_role.value || null, scope_user_public_id:form.scope_user_public_id.value || null,
        priority:Number(form.priority.value), config:JSON.parse(form.config.value), active:true,
      };
      await AuthUI.api('/api/v1/admin/policies', {method:'POST',body:JSON.stringify(payload)});
      form.reset(); adminMessage('정책을 추가했습니다.', 'good'); await loadPolicies();
    } catch (error) { adminMessage(error instanceof SyntaxError ? '정책 JSON 형식이 올바르지 않습니다.' : error.message, 'bad'); }
  };
  document.getElementById('ip-form').onsubmit = async event => {
    event.preventDefault(); const form = event.currentTarget;
    try {
      await AuthUI.api('/api/v1/admin/ip-blocks', {method:'POST',body:JSON.stringify({network:form.network.value,reason:form.reason.value})});
      form.reset(); adminMessage('IP 차단 정책을 추가했습니다.', 'good'); await loadIpBlocks();
    } catch (error) { adminMessage(error.message, 'bad'); }
  };
  window.addEventListener('hashchange', () => showPanel(location.hash.slice(1) || 'users'));
  showPanel(location.hash.slice(1) || 'users');
}

bootAdmin().catch(error => adminMessage(error.message, 'bad'));
