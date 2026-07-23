'use strict';
/* Ask Chat API 래퍼.
   - meta(): AuthUI.api 재사용 (problem+json·401 리다이렉트·403 오버레이 공통 처리)
   - stream(): SSE 는 AuthUI.api 가 다루지 못하므로(무조건 .json()) 전용 fetch 리더.
     CSRF 는 AuthUI.csrfToken() 을 재사용한다. */
const ChatAPI = (() => {
  const BASE = '/api/v1/chat';

  async function meta() {
    return AuthUI.api(BASE + '/meta');
  }

  /* messages: [{role, content}] / handlers: {onEvent(evt), signal}
     서버는 "data: {json}\n\n" 프레임만 보낸다. */
  async function stream(messages, { onEvent, signal }) {
    const response = await fetch(BASE + '/messages', {
      method: 'POST',
      credentials: 'same-origin',
      signal,
      headers: {
        'content-type': 'application/json',
        'x-csrf-token': AuthUI.csrfToken(),
      },
      body: JSON.stringify({ messages }),
    });

    if (!response.ok) {
      const body = await response.json().catch(() => null);
      if (response.status === 401) {
        window.location.href = '/auth/login?next=' + encodeURIComponent('/chat');
        return;
      }
      const error = new Error((body && (body.detail || body.title)) || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        const line = frame.split('\n').find(l => l.startsWith('data:'));
        if (!line) continue;
        let event = null;
        try { event = JSON.parse(line.slice(5).trim()); } catch { continue; }
        if (event) onEvent(event);
      }
    }
  }

  return { meta, stream };
})();
