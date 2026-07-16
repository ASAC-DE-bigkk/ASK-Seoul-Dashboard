'use strict';

function showMessage(text, kind = 'info') {
  const box = document.getElementById('message');
  box.className = `message ${kind}`; box.textContent = text; box.hidden = false;
}
function formJson(form) { return Object.fromEntries(new FormData(form).entries()); }
function nextParam() {
  const value = new URLSearchParams(location.search).get('next');
  return value && value.startsWith('/') && !value.startsWith('//') ? value : '/catalog';
}

async function setupLogin() {
  const params = new URLSearchParams(location.search);
  if (params.get('verified')) showMessage('이메일 인증이 완료되었습니다. 로그인하세요.', 'good');
  const state = await AuthUI.session().catch(() => ({ authenticated:false }));
  if (state.authenticated) location.href = nextParam();
  document.getElementById('login-form').onsubmit = async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type=submit]');
    button.disabled = true;
    try {
      const data = formJson(event.currentTarget);
      const result = await AuthUI.api('/api/v1/auth/login', {
        method:'POST',
        body:JSON.stringify({
          email:data.email, password:data.password,
          remember:data.remember === 'on', next:nextParam(),
        }),
      });
      location.href = result.next;
    } catch (error) {
      showMessage(error.message, error.status === 429 ? 'warn' : 'bad');
    } finally { button.disabled = false; }
  };
}

function setupRegister() {
  document.getElementById('register-form').onsubmit = async event => {
    event.preventDefault();
    const data = formJson(event.currentTarget);
    if (data.password !== data.password_confirm) {
      showMessage('비밀번호 확인이 일치하지 않습니다.', 'bad'); return;
    }
    if (data.agree !== 'on') {
      showMessage('서비스 이용 및 개인정보 처리 안내에 동의해야 합니다.', 'bad'); return;
    }
    const button = event.currentTarget.querySelector('button[type=submit]');
    button.disabled = true;
    try {
      const result = await AuthUI.api('/api/v1/auth/register', {
        method:'POST', body:JSON.stringify({email:data.email,password:data.password}),
      });
      showMessage(result.detail, result.email_delivered ? 'good' : 'warn');
      event.currentTarget.reset();
    } catch (error) { showMessage(error.message, 'bad'); }
    finally { button.disabled = false; }
  };
}

function setupForgot() {
  document.getElementById('forgot-form').onsubmit = async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type=submit]'); button.disabled = true;
    try {
      const data = formJson(event.currentTarget);
      const result = await AuthUI.api('/api/v1/auth/forgot-password', {
        method:'POST', body:JSON.stringify({email:data.email}),
      });
      showMessage(result.detail, result.email_configured ? 'good' : 'warn');
    } catch (error) { showMessage(error.message, 'bad'); }
    finally { button.disabled = false; }
  };
}

function setupReset() {
  const token = new URLSearchParams(location.search).get('token') || '';
  if (!token) showMessage('재설정 토큰이 없습니다.', 'bad');
  document.getElementById('reset-form').onsubmit = async event => {
    event.preventDefault();
    const data = formJson(event.currentTarget);
    if (data.password !== data.password_confirm) {
      showMessage('비밀번호 확인이 일치하지 않습니다.', 'bad'); return;
    }
    const button = event.currentTarget.querySelector('button[type=submit]'); button.disabled = true;
    try {
      const result = await AuthUI.api('/api/v1/auth/reset-password', {
        method:'POST', body:JSON.stringify({token,password:data.password}),
      });
      showMessage(result.detail, 'good');
      setTimeout(() => location.href='/auth/login', 1200);
    } catch (error) { showMessage(error.message, 'bad'); }
    finally { button.disabled = false; }
  };
}
