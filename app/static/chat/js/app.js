'use strict';
/* Ask Chat 화면 — 대화 목록(localStorage) + SSE 스트리밍 상태 머신.
   서버는 무상태: 매 턴 클라이언트가 이력(최근 N개)을 보낸다. */
(() => {
  const STORE_KEY = 'askseoul_chat_v1';
  const MAX_CONVOS = 30;
  const HISTORY_SENT = 20; // 서버로 보내는 최근 메시지 수 (바디 256KiB 상한 대비)

  const el = {
    convos: document.getElementById('convos'),
    newChat: document.getElementById('new-chat'),
    messages: document.getElementById('messages'),
    hello: document.getElementById('hello'),
    input: document.getElementById('input'),
    send: document.getElementById('send'),
    stop: document.getElementById('stop'),
    crumb: document.getElementById('crumb-title'),
    banner: document.getElementById('config-banner'),
    footModel: document.getElementById('foot-model'),
    footData: document.getElementById('foot-data'),
  };

  let state = { conversations: [], activeId: null };
  let streaming = null; // {abort: AbortController}
  let metaInfo = null;

  /* ── 저장소 ── */
  function loadStore() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      const parsed = raw ? JSON.parse(raw) : null;
      if (parsed && Array.isArray(parsed.conversations)) state = parsed;
    } catch { /* 손상된 저장소는 무시하고 새로 시작 */ }
  }
  function saveStore() {
    state.conversations = state.conversations.slice(0, MAX_CONVOS);
    try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch { /* 용량 초과 등 */ }
  }
  function activeConvo() {
    return state.conversations.find(c => c.id === state.activeId) || null;
  }
  function newConvo() {
    const convo = { id: 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
                    title: '새 대화', updated_at: Date.now(), messages: [] };
    state.conversations.unshift(convo);
    state.activeId = convo.id;
    saveStore();
    return convo;
  }

  /* ── 대화 목록 렌더 ── */
  function renderConvos() {
    el.convos.replaceChildren();
    if (!state.conversations.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = '저장된 대화가 없습니다';
      el.convos.appendChild(empty);
      return;
    }
    for (const convo of state.conversations) {
      const button = document.createElement('button');
      button.className = 'convo' + (convo.id === state.activeId ? ' active' : '');
      button.type = 'button';
      const title = document.createElement('span');
      title.className = 'title';
      title.textContent = convo.title;
      const del = document.createElement('span');
      del.className = 'del';
      del.title = '대화 삭제';
      del.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
      del.addEventListener('click', (event) => {
        event.stopPropagation();
        // 스트리밍 중에는 삭제 금지 — 라이브 말풍선 분리·턴 유실·좀비 스트림 방지.
        if (streaming) return;
        state.conversations = state.conversations.filter(c => c.id !== convo.id);
        if (state.activeId === convo.id) state.activeId = state.conversations[0]?.id || null;
        saveStore(); renderConvos(); renderMessages();
      });
      button.append(title, del);
      button.addEventListener('click', () => {
        if (streaming) return; // 스트리밍 중 전환 방지
        state.activeId = convo.id;
        saveStore(); renderConvos(); renderMessages();
      });
      el.convos.appendChild(button);
    }
  }

  /* ── 메시지 렌더 ── */
  function evidenceHtml(queries) {
    if (!queries || !queries.length) return '';
    const items = queries.map(q => {
      const table = ChatRender.esc(q.table || '(스펙 오류)');
      const meta = q.ok
        ? `${q.row_count ?? '?'}행${q.truncated ? ' · 잘림' : ''}`
        : `실패 — ${ChatRender.esc(q.error || '')}`;
      const sql = q.sql ? `<pre>${ChatRender.esc(q.sql)}</pre>` : '';
      return `<div class="q"><b>${table}</b><span class="meta">${meta}</span>${sql}</div>`;
    }).join('');
    return `<details class="evidence"><summary>조회 근거 ${queries.length}건</summary>${items}</details>`;
  }

  function messageNode(message) {
    const wrap = document.createElement('div');
    wrap.className = 'msg ' + message.role;
    const who = document.createElement('div');
    who.className = 'who';
    who.textContent = message.role === 'user' ? 'YOU' : 'ASK SEOUL';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    if (message.role === 'user') bubble.textContent = message.content;
    else bubble.innerHTML = ChatRender.markdown(message.content);
    wrap.append(who, bubble);
    if (message.role === 'assistant' && message.queries?.length) {
      const holder = document.createElement('div');
      holder.innerHTML = evidenceHtml(message.queries);
      wrap.appendChild(holder.firstElementChild);
    }
    if (message.error) {
      const note = document.createElement('div');
      note.className = 'error-note';
      note.textContent = message.error;
      wrap.appendChild(note);
    }
    return wrap;
  }

  function renderMessages() {
    const convo = activeConvo();
    el.crumb.textContent = convo ? convo.title : '새 대화';
    el.messages.replaceChildren();
    if (!convo || !convo.messages.length) {
      el.messages.appendChild(el.hello);
      el.hello.hidden = false;
      return;
    }
    el.hello.hidden = true;
    const col = document.createElement('div');
    col.className = 'msg-col';
    for (const message of convo.messages) col.appendChild(messageNode(message));
    el.messages.appendChild(col);
    el.messages.scrollTop = el.messages.scrollHeight;
  }

  /* ── 스트리밍 턴 ── */
  function setBusy(busy) {
    el.send.hidden = busy;
    el.stop.hidden = !busy;
    el.input.disabled = busy;
  }

  function stepNode(text, cls) {
    const chip = document.createElement('div');
    chip.className = 'step ' + (cls || '');
    chip.innerHTML = `<span class="dot"></span><span>${text}</span>`;
    return chip;
  }

  async function send(text) {
    const question = (text ?? el.input.value).trim();
    if (!question || streaming) return;
    if (metaInfo && !metaInfo.llm_configured) {
      showBanner('error', 'LLM API 키가 설정되지 않아 질문을 처리할 수 없습니다. ' + (metaInfo.detail || ''));
      return;
    }
    el.input.value = '';
    autoGrow();

    let convo = activeConvo();
    if (!convo) convo = newConvo();
    convo.messages.push({ role: 'user', content: question });
    if (convo.title === '새 대화') {
      convo.title = question.length > 24 ? question.slice(0, 24) + '…' : question;
    }
    convo.updated_at = Date.now();
    saveStore(); renderConvos(); renderMessages();

    // 진행 중 어시스턴트 말풍선 + 단계 칩
    const col = el.messages.querySelector('.msg-col');
    const live = document.createElement('div');
    live.className = 'msg assistant';
    live.innerHTML = '<div class="who">ASK SEOUL</div>';
    const steps = document.createElement('div');
    steps.className = 'steps';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.hidden = true;
    live.append(steps, bubble);
    col.appendChild(live);

    const assistant = { role: 'assistant', content: '', queries: [], error: null };
    const abort = new AbortController();
    streaming = { abort };
    setBusy(true);

    let thinkingChip = null;
    let pendingToolChip = null;
    const scroll = () => { el.messages.scrollTop = el.messages.scrollHeight; };

    const onEvent = (event) => {
      switch (event.event) {
        case 'thinking':
          if (!thinkingChip) {
            thinkingChip = stepNode('생각 중…', 'pulse-anim');
            steps.appendChild(thinkingChip);
          }
          scroll();
          break;
        case 'delta':
          if (thinkingChip) { thinkingChip.remove(); thinkingChip = null; }
          assistant.content += event.text;
          bubble.hidden = false;
          bubble.innerHTML = ChatRender.markdown(assistant.content);
          scroll();
          break;
        case 'tool': {
          if (thinkingChip) { thinkingChip.remove(); thinkingChip = null; }
          const table = ChatRender.esc(event.table || event.name || 'query');
          pendingToolChip = stepNode(`조회 중 <code>${table}</code>`, 'pulse-anim');
          steps.appendChild(pendingToolChip);
          scroll();
          break;
        }
        case 'tool_result': {
          const chip = pendingToolChip;
          pendingToolChip = null;
          if (chip) {
            chip.classList.remove('pulse-anim');
            if (event.ok) {
              chip.classList.add('ok');
              chip.lastElementChild.innerHTML =
                `조회 완료 <code>${ChatRender.esc(event.table || '')}</code> · ${event.row_count ?? '?'}행${event.truncated ? ' (잘림)' : ''}`;
            } else {
              chip.classList.add('fail');
              chip.lastElementChild.textContent = `조회 실패 — ${event.error || ''}`;
            }
          }
          scroll();
          break;
        }
        case 'done':
          assistant.queries = event.queries || [];
          break;
        case 'error':
          assistant.error = event.detail || event.title || '오류가 발생했습니다.';
          break;
        default:
          break;
      }
    };

    try {
      const historyMessages = convo.messages
        .filter(m => m.content && m.content.trim()) // 오류만 남은 빈 메시지는 이력에서 제외
        .slice(-HISTORY_SENT)
        .map(m => ({ role: m.role, content: m.content }));
      await ChatAPI.stream(historyMessages, { onEvent, signal: abort.signal });
    } catch (error) {
      if (error.name !== 'AbortError') {
        assistant.error = error.message || '요청에 실패했습니다.';
      } else if (!assistant.content) {
        assistant.error = '사용자가 응답을 중지했습니다.';
      }
    } finally {
      streaming = null;
      setBusy(false);
      if (!assistant.content && !assistant.error) {
        assistant.error = '응답이 비어 있습니다 — 다시 시도해 주세요.';
      }
      convo.messages.push(assistant);
      convo.updated_at = Date.now();
      saveStore();
      renderMessages();
      el.input.focus();
    }
  }

  /* ── 설정 배너·메타 ── */
  function showBanner(kind, text) {
    el.banner.hidden = false;
    el.banner.className = 'config-banner' + (kind === 'error' ? ' error' : '');
    el.banner.textContent = text;
  }

  async function loadMeta() {
    try {
      metaInfo = await ChatAPI.meta();
    } catch (error) {
      showBanner('error', '채팅 상태를 확인할 수 없습니다: ' + (error.message || ''));
      return;
    }
    el.footModel.textContent = metaInfo.model || '—';
    el.footData.textContent = metaInfo.data_configured
      ? `${metaInfo.data_backend} · ${metaInfo.table_count}테이블`
      : '미설정';
    if (!metaInfo.llm_configured || !metaInfo.data_configured) {
      showBanner(metaInfo.llm_configured ? '' : 'error',
        (metaInfo.detail || '설정이 필요합니다.') + ' 설정 방법은 docs/chat-design.md 를 참조하세요.');
    } else {
      el.banner.hidden = true;
    }
  }

  /* ── 입력창 ── */
  function autoGrow() {
    el.input.style.height = 'auto';
    el.input.style.height = Math.min(el.input.scrollHeight, 180) + 'px';
  }

  el.input.addEventListener('input', autoGrow);
  el.input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  });
  el.send.addEventListener('click', () => send());
  el.stop.addEventListener('click', () => { streaming?.abort.abort(); });
  el.newChat.addEventListener('click', () => {
    if (streaming) return;
    state.activeId = null;
    saveStore(); renderConvos(); renderMessages();
    el.input.focus();
  });
  el.messages.addEventListener('click', (event) => {
    const hint = event.target.closest('.hint');
    if (hint) send(hint.dataset.hint);
  });

  /* ── 부트스트랩 ── */
  AuthUI.bootstrapProtected().then(() => {
    loadStore();
    renderConvos();
    renderMessages();
    loadMeta();
    el.input.focus();
  });
})();
