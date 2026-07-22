'use strict';

function showMessage(text, kind = 'info') {
  const box = document.getElementById('message');
  box.setAttribute('role', 'status'); box.setAttribute('aria-live', 'polite');
  box.className = `message ${kind}`; box.textContent = text; box.hidden = false;
}
function formJson(form) { return Object.fromEntries(new FormData(form).entries()); }
function nextParam() {
  const value = new URLSearchParams(location.search).get('next');
  if (!value || !value.startsWith('/') || value.startsWith('//') || value.includes('\\')) return '/catalog';
  try {
    const target = new URL(value, location.origin);
    return target.origin === location.origin
      ? `${target.pathname}${target.search}${target.hash}`
      : '/catalog';
  } catch (_error) {
    return '/catalog';
  }
}

async function setupLogin() {
  const params = new URLSearchParams(location.search);
  if (params.get('verified')) showMessage('이메일 인증이 완료되었습니다. 로그인하세요.', 'good');
  if (params.get('mfa') === 'enabled') showMessage('MFA 설정이 완료되었습니다. 다시 로그인하세요.', 'good');
  if (params.get('expired')) {
    const at = params.get('at');
    let when = '';
    if (at) {
      const parsed = new Date(at);
      if (!Number.isNaN(parsed.getTime())) when = parsed.toLocaleString('ko-KR');
    }
    showMessage(
      when
        ? `${when}에 접속이 끊겼습니다. 다시 로그인해 주세요.`
        : '세션이 만료되어 접속이 끊겼습니다. 다시 로그인해 주세요.',
      'warn',
    );
  }
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
      if (result.mfa_required) {
        const fragment = new URLSearchParams({
          challenge:result.challenge,
          next:result.next || nextParam(),
        });
        location.href = `/auth/mfa#${fragment}`;
        return;
      }
      location.href = result.next;
    } catch (error) {
      showMessage(error.message, error.status === 429 ? 'warn' : 'bad');
    } finally { button.disabled = false; }
  };
}

function setupMfa() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const challenge = fragment.get('challenge') || '';
  const next = fragment.get('next') || '/catalog';
  if (location.hash) history.replaceState(null, '', location.pathname);
  const form = document.getElementById('mfa-form');
  if (!challenge) {
    showMessage('MFA 요청이 없거나 만료되었습니다. 다시 로그인하세요.', 'bad');
    form.querySelector('button[type=submit]').disabled = true;
    return;
  }
  form.onsubmit = async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type=submit]');
    button.disabled = true;
    try {
      const data = formJson(event.currentTarget);
      const result = await AuthUI.api('/api/v1/auth/mfa/verify', {
        method:'POST',
        body:JSON.stringify({challenge,code:data.code,next}),
      });
      location.href = result.next;
    } catch (error) {
      showMessage(error.message, error.status === 429 ? 'warn' : 'bad');
      button.disabled = false;
      document.getElementById('mfa-code').select();
    }
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
        method:'POST', body:JSON.stringify({
          email:data.email,password:data.password,terms_accepted:true,
        }),
      });
      showMessage(result.detail, 'good');
      event.currentTarget.reset();
    } catch (error) { showMessage(error.message, 'bad'); }
    finally { button.disabled = false; }
  };
}

function setupResendVerification() {
  document.getElementById('resend-form').onsubmit = async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type=submit]');
    button.disabled = true;
    try {
      const data = formJson(event.currentTarget);
      const result = await AuthUI.api('/api/v1/auth/resend-verification', {
        method:'POST', body:JSON.stringify({email:data.email}),
      });
      showMessage(result.detail, 'good');
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

function fragmentToken() {
  const token = new URLSearchParams(location.hash.slice(1)).get('token') || '';
  if (token) history.replaceState(null, '', location.pathname + location.search);
  return token;
}

function setupVerify() {
  const token = fragmentToken();
  const button = document.getElementById('verify-button');
  if (!token) {
    showMessage('인증 토큰이 없습니다. 최신 인증 이메일을 다시 확인하세요.', 'bad');
    button.disabled = true;
    return;
  }
  button.onclick = async () => {
    button.disabled = true;
    try {
      const result = await AuthUI.api('/api/v1/auth/verify-email', {
        method:'POST', body:JSON.stringify({token}),
      });
      showMessage(result.detail, 'good');
      setTimeout(() => location.href='/auth/login?verified=1', 1000);
    } catch (error) {
      showMessage(error.message, 'bad'); button.disabled = false;
    }
  };
}

function setupReset() {
  const token = fragmentToken();
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
