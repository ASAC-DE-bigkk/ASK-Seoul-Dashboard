/* Charts Studio 오케스트레이터 — 레이아웃 페이지·그리드·편집모드·구성 드로어.
 * 온톨로지 원칙: 차트 저장물은 {type, source, bindings(슬롯→필드), agg, options} 뿐이고,
 * 렌더 시점마다 소스의 현재 필드/role 로 재해석한다. 필드가 사라지면 같은 role 로 폴백. */
'use strict';

const S = {
  meta: null, sources: [], srcDetails: {},
  pages: [], pageId: null, page: null,
  edit: false, dirty: false,
  grid: null, tiles: {},          // chartId → {el, inst, chart}
  modes: {},                      // chartId → live|cache|stale
};

/* 타일 크기 변화 → 해당 ECharts 재계산. 캐시 응답이 그리드 배치보다 먼저
 * 그려져도(초기 관찰 콜백 포함) 항상 실제 크기로 수렴한다. */
const plotRO = new ResizeObserver(entries => {
  entries.forEach(en => {
    const inst = echarts.getInstanceByDom(en.target);
    if (inst) inst.resize();
  });
});
const $ = id => document.getElementById(id);
const uid = () => Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ── 공용 UI: 토스트/모달/컨텍스트 ───────────────────────── */
let toastTimer;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg; t.classList.add('on');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('on'), 2000);
}

function modal({ title, desc = '', input = null, okLabel = '확인', danger = false }) {
  return new Promise(resolve => {
    $('modal-title').textContent = title;
    $('modal-desc').textContent = desc;
    const wrap = $('modal-input-wrap'), inp = $('modal-input');
    wrap.hidden = input == null;
    if (input != null) { inp.value = input; setTimeout(() => { inp.focus(); inp.select(); }, 30); }
    const ok = $('modal-ok'), cancel = $('modal-cancel');
    ok.textContent = okLabel;
    ok.className = 'btn primary' + (danger ? ' danger' : '');
    $('modal').hidden = false;
    const done = v => { $('modal').hidden = true; ok.onclick = cancel.onclick = inp.onkeydown = null; resolve(v); };
    ok.onclick = () => done(input != null ? inp.value.trim() : true);
    cancel.onclick = () => done(null);
    inp.onkeydown = e => { if (e.key === 'Enter') ok.onclick(); };
  });
}

function showCtx(x, y, items) {
  const ctx = $('ctx');
  ctx.innerHTML = items.map((it, i) => it === 'hr' ? '<hr>' :
    `<button data-i="${i}" ${it.disabled ? 'disabled' : ''} class="${it.danger ? 'danger' : ''}">${it.icon || ''}${esc(it.label)}</button>`).join('');
  ctx.hidden = false;
  const rect = { w: ctx.offsetWidth, h: ctx.offsetHeight };
  ctx.style.left = Math.min(x, innerWidth - rect.w - 8) + 'px';
  ctx.style.top = Math.min(y, innerHeight - rect.h - 8) + 'px';
  ctx.onclick = e => {
    const b = e.target.closest('button'); if (!b) return;
    hideCtx(); const it = items[Number(b.dataset.i)]; it && it.run && it.run();
  };
}
function hideCtx() { $('ctx').hidden = true; }
document.addEventListener('click', e => { if (!e.target.closest('#ctx')) hideCtx(); });
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { hideCtx(); if (!$('modal').hidden) $('modal-cancel').click(); else if (CFG.open) closeCfg(); }
});

/* ── 온톨로지 바인딩 해석 ─────────────────────────────────── */
function typeDef(t) { return S.meta.chart_types[t]; }
function fieldsByRole(src, accepts) { return src.fields.filter(f => accepts.includes(f.role)); }

function resolveBindings(chart, src) {
  const def = typeDef(chart.type);
  if (!def) throw new Error(`알 수 없는 도표 타입: ${chart.type}`);
  const b = {}, rebound = [];
  for (const slot of def.slots) {
    const want = (chart.bindings || {})[slot.name];
    const f = want && src.fields.find(f => f.name === want);
    if (f && slot.accepts.includes(f.role)) { b[slot.name] = f.name; continue; }
    if (!want && !slot.required) continue;      // 선택 슬롯은 명시 바인딩 없으면 비워둔다
    const cands = fieldsByRole(src, slot.accepts);
    if (cands.length) {
      b[slot.name] = cands[0].name;
      if (want) rebound.push(`${want}→${cands[0].name}`);
    } else if (slot.required) {
      throw new Error(`'${src.label}' 에 ${slot.label}(${slot.accepts.join('/')}) 역할 필드가 없습니다`);
    }
  }
  return { b, rebound, def };
}

function fieldLabel(src, name) {
  const f = src.fields.find(f => f.name === name);
  return f ? f.label : name;
}
const AGG_LABEL = { sum: '합계', avg: '평균', count: '건수', count_distinct: '고유수', min: '최소', max: '최대' };

function buildSpec(chart, src, b) {
  const agg = chart.agg || 'sum';
  const o = chart.options || {};
  const alias = agg === 'count' ? 'count' : `${agg}_${b.value || b.x || 'v'}`;
  const m = f => ({ field: agg === 'count' ? null : f, agg, alias: agg === 'count' ? 'count' : `${agg}_${f}` });
  const spec = { source: chart.source, dims: [], measures: [], filters: chart.filters || [], order_by: [], limit: 1000 };
  const t = chart.type;

  if (t === 'stat') { spec.measures = [m(b.value)]; }
  else if (t === 'bar' || t === 'line' || t === 'area') {
    spec.dims = b.series ? [b.axis, b.series] : [b.axis];
    spec.measures = [m(b.value)];
    // limit 은 서버 상한(5000)까지 — 절단이 피벗/합계를 왜곡하는 것을 최소화 (도달 시 타일에 경고)
    if (t === 'bar') { spec.order_by = [{ field: m(b.value).alias, dir: 'desc' }]; spec.limit = 5000; }
    else { spec.order_by = [{ field: b.axis, dir: 'asc' }]; spec.limit = 5000; }
  }
  else if (t === 'pie') {
    spec.dims = [b.axis]; spec.measures = [m(b.value)];
    spec.order_by = [{ field: m(b.value).alias, dir: 'desc' }]; spec.limit = 500;
  }
  else if (t === 'scatter') {
    // count 는 X·Y 가 같은 값으로 붕괴(대각선)하므로 산점도에서는 avg 로 대체
    const sAgg = agg === 'count' || agg === 'count_distinct' ? 'avg' : agg;
    const sm = f => ({ field: f, agg: sAgg, alias: `${sAgg}_${f}` });
    spec.dims = [b.axis]; spec.measures = [sm(b.x), sm(b.y)];
    spec.order_by = [{ field: sm(b.x).alias, dir: 'desc' }];
    spec.limit = o.top_n || 300;
  }
  else if (t === 'heatmap') { spec.dims = [b.x, b.y]; spec.measures = [m(b.value)]; spec.limit = 5000; }
  else if (t === 'race') {
    spec.dims = [b.time, b.axis]; spec.measures = [m(b.value)];
    spec.order_by = [{ field: b.time, dir: 'asc' }]; spec.limit = 5000;
  }
  else if (t === 'map_points') {
    spec.dims = [b.lat, b.lng]; spec.measures = [m(b.value)]; spec.limit = o.top_n || 4000;
  }
  else if (t.startsWith('map_')) { spec.dims = [b.region]; spec.measures = [m(b.value)]; spec.limit = 800; }
  else if (t === 'table') {
    spec.dims = [b.axis]; spec.measures = [m(b.value)];
    spec.order_by = [{ field: m(b.value).alias, dir: 'desc' }]; spec.limit = o.top_n || 50;
  }
  return { spec, alias };
}

/* ── 타일 ─────────────────────────────────────────────────── */
const ICONS = {
  refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M20 11a8 8 0 10.6 4.5M20 5v6h-6"/></svg>',
  edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 20l4-1L20 7l-3-3L5 16l-1 4z"/></svg>',
  del: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/></svg>',
};

function tileDom(chart) {
  const g = chart.grid || {};
  const el = document.createElement('div');
  el.className = 'grid-stack-item';
  if (g.w) { el.setAttribute('gs-x', g.x ?? 0); el.setAttribute('gs-y', g.y ?? 0);
             el.setAttribute('gs-w', g.w); el.setAttribute('gs-h', g.h); }
  else { el.setAttribute('gs-auto-position', 'true'); el.setAttribute('gs-w', defW(chart.type)); el.setAttribute('gs-h', defH(chart.type)); }
  el.dataset.chartId = chart.id;
  el.innerHTML = `<div class="grid-stack-item-content"><div class="tile">
    <div class="tile-head">
      <div class="cat"><span class="dot"></span>
        <div class="tt"><b>${esc(chart.title || '차트')}</b><span>${esc(chart.source)}</span></div></div>
      <div class="acts">
        <button class="rf" title="다시 조회">${ICONS.refresh}</button>
        <button class="ed" title="차트 편집" ${S.edit ? '' : 'hidden'}>${ICONS.edit}</button>
        <button class="del" title="차트 삭제" ${S.edit ? '' : 'hidden'}>${ICONS.del}</button>
      </div>
    </div>
    <div class="tile-body"><div class="plot"></div></div>
    <div class="tile-foot"><span class="mode" hidden><i></i><span class="mtxt"></span></span>
      <span class="note"></span><span class="ms num"></span></div>
  </div></div>`;
  el.querySelector('.rf').onclick = e => { e.stopPropagation(); loadTile(chart, true); };
  el.querySelector('.ed').onclick = e => { e.stopPropagation(); openCfg('edit', chart); };
  el.querySelector('.del').onclick = async e => {
    e.stopPropagation();
    const ok = await modal({ title: '차트 삭제', desc: `'${chart.title}' 차트를 이 레이아웃에서 삭제할까요?`, okLabel: '삭제', danger: true });
    if (!ok) return;
    removeTile(chart.id); S.dirty = true;
  };
  return el;
}
const defW = t => t === 'stat' ? 3 : 6;
const defH = t => t === 'stat' ? 2 : (t.startsWith('map_') ? 5 : 4);

function tileState(el, html) {
  const body = el.querySelector('.tile-body');
  let st = body.querySelector('.tile-state');
  if (html == null) { st && st.remove(); return; }
  if (!st) { st = document.createElement('div'); st.className = 'tile-state'; body.appendChild(st); }
  st.innerHTML = html; return st;
}

async function loadTile(chart, force, quiet) {
  const rec = S.tiles[chart.id]; if (!rec) return;
  const el = rec.el;
  const plot = el.querySelector('.plot');
  // quiet(자동 갱신) 이고 이미 그려져 있으면 스피너로 화면을 가리지 않는다 — 무깜빡임 갱신
  if (!quiet || !rec.inst) tileState(el, '<div class="spin"></div>');
  try {
    const src = S.srcDetails[chart.source] || (S.srcDetails[chart.source] = await API.source(chart.source));
    const { b, rebound, def } = resolveBindings(chart, src);
    const { spec, alias } = buildSpec(chart, src, b);
    if (force) spec.force = true;               // 신선 캐시 무시 ('다시 조회')
    const res = await API.query(spec);
    if (S.tiles[chart.id] !== rec) return;      // 페이지 전환 등으로 타일이 사라진 늦은 응답 폐기
    tileState(el, null);

    const ctx = {
      el, chart, b, src,
      rows: res.rows, cols: res.columns,
      geo: def.geo || null,
      regionRole: b.region ? (src.fields.find(f => f.name === b.region) || {}).role : null,
      valueLabel: chart.agg === 'count' ? '건수' : `${fieldLabel(src, b.value || b.x)} ${AGG_LABEL[chart.agg || 'sum'] || ''}`.trim(),
      xLabel: b.x ? fieldLabel(src, b.x) : '', yLabel: b.y ? fieldLabel(src, b.y) : '',
      colLabels: {},
    };
    ctx.colLabels = Object.fromEntries(res.columns.map(c => [c, c === alias ? ctx.valueLabel : fieldLabel(src, c)]));
    if (!res.rows.length) {
      const old = echarts.getInstanceByDom(plot);
      if (old) old.dispose();                   // 직전 렌더 잔상이 '데이터 없음' 뒤로 비치지 않게
      plot.innerHTML = '';
      rec.inst = null;
      tileState(el, '<span>데이터가 없습니다 — 필터를 확인하세요</span>');
    } else {
      rec.inst = await RENDER.render(plot, ctx);
      if (S.tiles[chart.id] !== rec) { if (rec.inst) rec.inst.dispose(); return; }
      if (rec.inst) { plotRO.observe(plot); requestAnimationFrame(() => rec.inst && rec.inst.resize()); }
    }

    S.modes[chart.id] = res.mode;
    const modeEl = el.querySelector('.mode');
    modeEl.hidden = false;
    modeEl.className = 'mode ' + res.mode;
    modeEl.querySelector('.mtxt').textContent = res.mode === 'live' ? 'live' : res.mode === 'cache' ? 'cached' : 'stale';
    const notes = [];
    if (ctx.coverage) notes.push(ctx.coverage);
    if (ctx.note) notes.push(ctx.note);
    // 안전 상한(1000+)에 닿았을 때만 경고 — 의도된 top-N(테이블 50행 등)은 제외
    if (spec.limit >= 1000 && res.row_count >= spec.limit) notes.push('행 상한 도달 — 부분 데이터일 수 있음');
    if (rebound.length) notes.push(`재바인딩 ${rebound.join(', ')}`);
    el.querySelector('.note').textContent = notes.join(' · ');
    el.querySelector('.note').title = res.sql || '';
    el.querySelector('.ms').textContent = res.mode === 'live' ? `${res.elapsed_ms}ms` : '';
    updateSync();
  } catch (err) {
    const st = tileState(el, `<div class="tile-state err"><b>렌더 실패</b>
      <span class="detail" title="${esc(err.message)}">${esc(err.message)}</span>
      <button class="btn ghost retry">다시 시도</button></div>`);
    st.querySelector('.retry').onclick = () => loadTile(chart, true);
    st.className = 'tile-state err';
  }
}

function addTile(chart, load = true) {
  const el = tileDom(chart);
  S.grid.el.appendChild(el);
  S.grid.makeWidget(el);
  S.tiles[chart.id] = { el, inst: null, chart };
  if (load) loadTile(chart);
  $('empty-board').hidden = S.page.charts.length > 0;
}

function removeTile(chartId) {
  const rec = S.tiles[chartId]; if (!rec) return;
  if (rec.inst) { rec.inst.dispose(); }
  S.grid.removeWidget(rec.el);
  delete S.tiles[chartId];
  delete S.modes[chartId];
  S.page.charts = S.page.charts.filter(c => c.id !== chartId);
  $('empty-board').hidden = S.page.charts.length > 0;
  updateSync();
}

function collectGrid() {
  S.grid.engine.nodes.forEach(n => {
    const id = n.el.dataset.chartId;
    const chart = S.page.charts.find(c => c.id === id);
    if (chart) chart.grid = { x: n.x, y: n.y, w: n.w, h: n.h };
  });
}

/* ── 페이지 렌더/전환 ─────────────────────────────────────── */
async function renderPage() {
  Object.values(S.tiles).forEach(r => r.inst && r.inst.dispose());
  S.tiles = {};
  S.modes = {};
  S.grid.removeAll();
  $('crumb-page').textContent = S.page ? S.page.name : '—';
  refreshBaseline();   // 페이지 로드 직후가 기준점 — 다음 갱신은 간격 후
  if (!S.page) { $('empty-board').hidden = false; return; }
  $('empty-board').hidden = S.page.charts.length > 0;
  S.grid.batchUpdate();
  S.page.charts.forEach(c => addTile(c, false));
  S.grid.batchUpdate(false);
  S.page.charts.forEach(c => loadTile(c));
}

let switchGen = 0;   // 연타 경합 가드 — 늦게 도착한 이전 페이지 응답이 상태를 덮지 않게

async function switchPage(id, { force } = {}) {
  if (!force && S.edit && S.dirty) {
    const ok = await modal({ title: '저장하지 않은 변경', desc: '레이아웃 변경 내용을 저장하지 않고 이동할까요?', okLabel: '이동', danger: true });
    if (!ok) return false;
  }
  if (S.edit) exitEdit();
  const gen = ++switchGen;
  S.pageId = id;
  localStorage.setItem('charts.pageId', id || '');
  const page = id ? await API.page(id) : null;
  if (gen !== switchGen) return false;   // 그 사이 다른 전환이 시작됨 — 이 응답은 폐기
  S.page = page;
  renderSidebar();
  await renderPage();
  return true;
}

/* ── 사이드탭 ─────────────────────────────────────────────── */
const PAGE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M10 10v10"/></svg>';

function renderSidebar() {
  const box = $('pages');
  box.innerHTML = '';
  S.pages.forEach((p, i) => {
    const b = document.createElement('button');
    b.className = 'page-item' + (p.id === S.pageId ? ' on' : '');
    b.innerHTML = `${PAGE_ICON}<span class="nm">${esc(p.name)}</span><span class="cnt">${p.chart_count}</span>`;
    b.onclick = () => { if (p.id !== S.pageId) switchPage(p.id); };
    b.oncontextmenu = e => { e.preventDefault(); pageCtx(e, p, i); };
    box.appendChild(b);
  });
}

function pageCtx(e, p, i) {
  showCtx(e.clientX, e.clientY, [
    { label: '위로 이동', disabled: i === 0, run: () => movePage(i, -1) },
    { label: '아래로 이동', disabled: i === S.pages.length - 1, run: () => movePage(i, 1) },
    'hr',
    { label: '이름 변경', run: async () => {
        const name = await modal({ title: '레이아웃 이름 변경', input: p.name, okLabel: '변경' });
        if (!name) return;
        await API.patchPage(p.id, { name });
        p.name = name;
        if (p.id === S.pageId) { S.page.name = name; $('crumb-page').textContent = name; }
        renderSidebar(); toast('이름을 변경했습니다');
      } },
    { label: '복제', run: async () => {
        const copy = await API.duplicatePage(p.id);
        S.pages = await API.pages(); renderSidebar();
        toast(`'${copy.name}' 레이아웃을 만들었습니다`);
      } },
    'hr',
    { label: '삭제', danger: true, run: async () => {
        const ok = await modal({ title: '레이아웃 삭제', desc: `'${p.name}' 레이아웃과 차트 ${p.chart_count}개 구성이 삭제됩니다.`, okLabel: '삭제', danger: true });
        if (!ok) return;
        await API.deletePage(p.id);
        S.pages = await API.pages();
        if (p.id === S.pageId) await switchPage(S.pages.length ? S.pages[0].id : null, { force: true });
        else renderSidebar();
        toast('레이아웃을 삭제했습니다');
      } },
  ]);
}

async function movePage(i, delta) {
  const ids = S.pages.map(p => p.id);
  const j = i + delta;
  [ids[i], ids[j]] = [ids[j], ids[i]];
  S.pages = await API.reorderPages(ids);
  renderSidebar();
}

/* ── 편집 모드 ────────────────────────────────────────────── */
function setActButtons() {
  document.querySelectorAll('.tile .acts .ed, .tile .acts .del')
    .forEach(b => { b.hidden = !S.edit; });
}
function enterEdit() {
  S.edit = true; S.dirty = false;
  S.grid.setStatic(false);
  $('canvas').classList.add('editing');
  $('grid').classList.add('gs-editing');
  $('btn-edit-label').textContent = '레이아웃 저장';
  $('btn-add-chart').hidden = false;
  $('btn-cancel-edit').hidden = false;
  setActButtons();
}
function exitEdit() {
  S.edit = false; S.dirty = false;
  S.grid.setStatic(true);
  $('canvas').classList.remove('editing');
  $('grid').classList.remove('gs-editing');
  $('btn-edit-label').textContent = '레이아웃 변경';
  $('btn-add-chart').hidden = true;
  $('btn-cancel-edit').hidden = true;
  setActButtons();
}
async function saveLayout() {
  collectGrid();
  const btn = $('btn-edit');
  btn.classList.add('saving');
  try {
    await API.patchPage(S.pageId, { charts: S.page.charts });
    S.pages = await API.pages(); renderSidebar();
    exitEdit(); toast('레이아웃을 저장했습니다');
  } catch (err) {
    toast('저장 실패: ' + err.message);
  } finally { btn.classList.remove('saving'); }
}

/* ── 구성 드로어 (소스 → 도표 → 연결) ─────────────────────── */
const CFG = { open: false, mode: 'add', chartId: null, step: 'source',
              source: null, src: null, type: null, bindings: {}, agg: 'sum',
              title: '', titleTouched: false, filters: [], options: {}, domain: 'commerce', search: '' };

const TYPE_ICONS = {
  stat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 12h6M7 15.5h4" stroke-linecap="round"/></svg>',
  bar: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M5 20V10M12 20V4M19 20v-7"/></svg>',
  line: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 17l5-6 4 3 6-8"/><path d="M3 21h18" opacity=".4"/></svg>',
  area: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 19l5-7 4 3 6-8v12H3z" fill="currentColor" fill-opacity=".15"/></svg>',
  pie: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="8"/><path d="M12 4v8l6 5"/></svg>',
  scatter: '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="7" cy="15" r="1.7"/><circle cx="11" cy="9" r="1.7"/><circle cx="16" cy="13" r="1.7"/><circle cx="18" cy="6" r="1.7"/></svg>',
  race: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 6h9M4 12h13M4 18h6"/><path d="M17 15l4 3-4 3z" fill="currentColor" stroke="none"/></svg>',
  heatmap: '<svg viewBox="0 0 24 24" fill="currentColor" opacity=".8"><rect x="4" y="4" width="5" height="5" rx="1"/><rect x="10" y="4" width="5" height="5" rx="1" opacity=".45"/><rect x="16" y="4" width="5" height="5" rx="1"/><rect x="4" y="10" width="5" height="5" rx="1" opacity=".3"/><rect x="10" y="10" width="5" height="5" rx="1"/><rect x="16" y="10" width="5" height="5" rx="1" opacity=".55"/><rect x="4" y="16" width="5" height="5" rx="1" opacity=".7"/><rect x="10" y="16" width="5" height="5" rx="1" opacity=".25"/><rect x="16" y="16" width="5" height="5" rx="1" opacity=".9"/></svg>',
  table: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M3 14h18M10 9v11"/></svg>',
  map: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M9 4L3 6v14l6-2 6 2 6-2V4l-6 2-6-2z"/><path d="M9 4v14M15 6v14"/></svg>',
};

function openCfg(mode, chart) {
  CFG.open = true; CFG.mode = mode;
  if (mode === 'edit' && chart) {
    Object.assign(CFG, {
      chartId: chart.id, source: chart.source, type: chart.type,
      bindings: { ...(chart.bindings || {}) }, agg: chart.agg || 'sum',
      title: chart.title, titleTouched: true,
      filters: JSON.parse(JSON.stringify(chart.filters || [])),
      options: optionsForType(chart.type, chart.options || {}), step: 'bind',
    });
    $('cfg-mode-label').textContent = '차트 편집';
    $('cfg-apply').textContent = '적용';
  } else {
    Object.assign(CFG, { chartId: null, source: null, src: null, type: null, bindings: {}, agg: 'sum',
                         title: '', titleTouched: false, filters: [], options: {}, step: 'source', search: '',
                         comboIdx: null });
    $('cfg-mode-label').textContent = '차트 추가';
    $('cfg-apply').textContent = '추가';
  }
  $('cfg-overlay').classList.add('on');
  $('cfg-drawer').classList.add('on');
  if (mode === 'edit') {
    // 캐시가 있으면 동기 설정 — 최초 편집 클릭에서 CFG.src=null 렌더 크래시 방지
    CFG.src = S.srcDetails[CFG.source] || null;
    if (!CFG.src) {
      API.source(CFG.source).then(src => {
        S.srcDetails[src.name] = src;
        if (CFG.open && CFG.source === src.name) { CFG.src = src; renderCfg(); }
      });
    }
  }
  renderCfg();
}
function disposePreview() {
  const plot = document.querySelector('.preview-box .plot');
  if (!plot) return;
  const pv = echarts.getInstanceByDom(plot);
  if (pv) pv.dispose();
}
function closeCfg() {
  disposePreview();
  CFG.open = false;
  $('cfg-overlay').classList.remove('on');
  $('cfg-drawer').classList.remove('on');
}
$('cfg-close').onclick = closeCfg;
$('cfg-overlay').onclick = closeCfg;

function cfgStepOk(step) {
  if (step === 'source') return true;
  if (step === 'type') return !!CFG.source;
  if (step === 'bind') return !!CFG.source && !!CFG.type;
  return false;
}

function renderCfgTabs() {
  document.querySelectorAll('#cfg-tabs button').forEach(b => {
    b.classList.toggle('on', b.dataset.step === CFG.step);
    b.disabled = !cfgStepOk(b.dataset.step);
    b.onclick = () => { CFG.step = b.dataset.step; renderCfg(); };
  });
  $('cfg-title').textContent = CFG.title || (CFG.src ? `${CFG.src.label}` : '새 차트');
  $('cfg-apply').disabled = !(CFG.source && CFG.type && requiredBound());
}
function requiredBound() {
  if (!CFG.src || !CFG.type) return false;
  try { resolveBindings(currentDraft(), CFG.src); return true; } catch { return false; }
}
function currentDraft() {
  return { id: CFG.chartId || 'draft', title: CFG.title, type: CFG.type, source: CFG.source,
           bindings: CFG.bindings, agg: CFG.agg, filters: CFG.filters, options: CFG.options };
}
function optionsForType(type, options = {}) {
  const contract = (typeDef(type) || {}).options || {};
  return Object.fromEntries(
    Object.keys(contract)
      .filter(key => Object.hasOwn(options, key))
      .map(key => [key, options[key]])
  );
}

function renderCfg() {
  disposePreview();      // body 재작성 전에 미리보기 인스턴스 정리 (detached DOM 누수 방지)
  renderCfgTabs();
  const body = $('cfg-body');
  if (CFG.step === 'source') return renderCfgSource(body);
  if ((CFG.step === 'type' || CFG.step === 'bind') && !CFG.src) {
    body.innerHTML = '<section><h3>소스 정보 로딩 중…</h3><div class="tile-state" style="position:static;padding:30px"><div class="spin"></div></div></section>';
    return;
  }
  if (CFG.step === 'type') return renderCfgType(body);
  return renderCfgBind(body);
}

function renderCfgSource(body) {
  const domains = ['all', ...Object.keys(S.meta.domains)];
  const q = CFG.search.toLowerCase();
  const list = S.sources
    .filter(s => CFG.domain === 'all' || s.domain === CFG.domain)
    .filter(s => !q || (s.name + s.label + s.description).toLowerCase().includes(q));
  body.innerHTML = `
    <section><h3>데이터 소스 — gold 카탈로그</h3>
      <div class="src-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
        <input id="src-q" type="search" placeholder="소스 검색" value="${esc(CFG.search)}"></div>
      <div class="src-doms">${domains.map(d =>
        `<button class="chip ${CFG.domain === d ? 'on' : ''}" data-d="${esc(d)}">${esc(d)}</button>`).join('')}</div>
      <div class="src-list">${list.map(s => `
        <button class="src-item ${CFG.source === s.name ? 'on' : ''}" data-s="${esc(s.name)}">
          <div class="info"><b>${esc(s.label)}</b><span>${esc(s.name)}</span></div>
          <span class="rows num">${Number(s.row_count).toLocaleString('ko-KR')} rows</span>
        </button>`).join('') || '<div style="color:var(--ink-4);font-size:12px;padding:16px">검색 결과 없음</div>'}
      </div>
    </section>`;
  $('src-q').oninput = e => { CFG.search = e.target.value; renderCfgSource(body); };
  body.querySelectorAll('.chip').forEach(c => c.onclick = () => { CFG.domain = c.dataset.d; renderCfgSource(body); });
  body.querySelectorAll('.src-item').forEach(el => el.onclick = async () => {
    CFG.source = el.dataset.s;
    CFG.src = S.srcDetails[CFG.source] || (S.srcDetails[CFG.source] = await API.source(CFG.source));
    CFG.type = null; CFG.bindings = {}; CFG.options = {};
    CFG.step = 'type'; renderCfg();
  });
}

/* 추천 이유 툴팁 — 도표 카드/조합 칩 공용 */
function recoTip() {
  let tip = $('reco-tip');
  if (!tip) { tip = document.createElement('div'); tip.id = 'reco-tip'; document.body.appendChild(tip); }
  return tip;
}
function bindRecoTip(el, reason) {
  el.addEventListener('mouseenter', () => {
    const tip = recoTip();
    tip.innerHTML = `<b>이 소스엔 이 도표</b>${esc(reason)}`;
    tip.classList.add('on');
    const r = el.getBoundingClientRect();
    tip.style.left = Math.min(r.left, innerWidth - 270) + 'px';
    tip.style.top = (r.bottom + 6) + 'px';
  });
  el.addEventListener('mouseleave', () => recoTip().classList.remove('on'));
}

function renderCfgType(body) {
  const src = CFG.src;
  const reco = RECO.types(src);   // role 기반 — 어떤 소스가 와도 그 자리에서 계산
  body.innerHTML = `
    <section><h3>도표 타입 — '${esc(src.label)}' 가 지원하는 형태</h3>
      <div class="type-grid">${Object.entries(S.meta.chart_types).map(([t, def]) => {
        const isReco = reco[t] && reco[t].score >= 2;
        return `
        <button class="type-card ${CFG.type === t ? 'on' : ''} ${isReco ? 'reco' : ''}" data-t="${t}"
                ${src.supports.includes(t) ? '' : 'disabled'}>
          ${TYPE_ICONS[def.icon] || TYPE_ICONS.bar}<b>${esc(def.label)}</b>
        </button>`;
      }).join('')}</div>
      <p class="bind-hint" style="margin-top:10px">비활성 = 이 소스에 필요한 역할(시간축/지역/측정값)이 없는 도표.
        <span style="color:var(--accent-deep)">추천</span> 배지에 마우스를 올리면 컬럼 형태 기반 추천 이유가 보입니다.</p>
    </section>`;
  body.querySelectorAll('.type-card:not(:disabled)').forEach(el => {
    const t = el.dataset.t;
    el.onclick = () => {
      CFG.type = t;
      CFG.options = {};
      autoBind();
      CFG.step = 'bind';
      renderCfg();
    };
    if (reco[t] && reco[t].score >= 2) bindRecoTip(el, reco[t].reason);
  });
}

function autoBind() {
  const def = typeDef(CFG.type), src = CFG.src;
  const b = {};
  const dc = src.default_chart;
  const curatedHit = dc && dc.type === CFG.type;
  // 우선순위: 큐레이션 힌트 → 추천 엔진 1순위 조합 → role 첫 후보
  const combo = !curatedHit ? (RECO.combos(src, CFG.type)[0] || null) : null;
  for (const slot of def.slots) {
    const want = (curatedHit && dc.bindings && dc.bindings[slot.name])
              || (combo && combo.bindings[slot.name]);
    const wf = want && src.fields.find(f => f.name === want && slot.accepts.includes(f.role));
    if (wf) { b[slot.name] = wf.name; continue; }
    if (!slot.required) continue;               // 선택 슬롯은 명시적 제안이 있을 때만
    const cands = fieldsByRole(src, slot.accepts);
    if (cands.length) b[slot.name] = cands[0].name;
  }
  CFG.bindings = b;
  CFG.comboIdx = combo ? 0 : null;
  if (curatedHit && dc.agg) CFG.agg = dc.agg;
  else if (combo) { CFG.agg = combo.agg; CFG.options = { ...CFG.options, ...combo.options }; }
  if (!CFG.titleTouched) CFG.title = combo ? combo.label : `${src.label} · ${def.label}`;
}

function renderCfgBind(body) {
  const def = typeDef(CFG.type), src = CFG.src;
  const slotRow = slot => {
    const cands = fieldsByRole(src, slot.accepts);
    const cur = CFG.bindings[slot.name] || '';
    return `<div class="bind-row">
      <label>${esc(slot.label)} ${slot.required ? '<span class="req">*</span>' : ''}</label>
      <select data-slot="${esc(slot.name)}">
        ${slot.required ? '' : '<option value="">(없음)</option>'}
        ${cands.map(f => `<option value="${esc(f.name)}" ${f.name === cur ? 'selected' : ''}>${esc(f.label)} — ${esc(f.name)} (${esc(f.role)})</option>`).join('')}
      </select></div>`;
  };
  const filterRow = (f, i) => `
    <div class="bind-grid" data-fi="${i}" style="grid-template-columns: 1.2fr .7fr 1fr auto; align-items:end">
      <div class="bind-row"><label>필드</label><select class="ff">${src.fields.map(x =>
        `<option value="${esc(x.name)}" ${x.name === f.field ? 'selected' : ''}>${esc(x.label)} — ${esc(x.name)}</option>`).join('')}</select></div>
      <div class="bind-row"><label>조건</label><select class="fo">${['eq', 'neq', 'gte', 'lte', 'like', 'in'].map(o =>
        `<option ${o === f.op ? 'selected' : ''}>${o}</option>`).join('')}</select></div>
      <div class="bind-row"><label>값</label><input type="text" class="fv" value="${esc(f.op === 'in' && Array.isArray(f.value) ? f.value.join(',') : (f.value ?? ''))}"></div>
      <button class="btn ghost fdel" title="필터 삭제" style="height:33px">✕</button>
    </div>`;

  body.innerHTML = `
    <section><h3>온톨로지 연결 — 슬롯(역할) ↔ 필드</h3>
      <div class="bind-form">
        <div class="bind-row"><label>제목</label><input type="text" id="cfg-name" value="${esc(CFG.title)}" maxlength="60"></div>
        <div class="bind-grid">${def.slots.map(slotRow).join('')}
          <div class="bind-row"><label>집계</label><select id="cfg-agg">
            ${Object.entries(AGG_LABEL)
              .filter(([a]) => CFG.type !== 'scatter' || (a !== 'count' && a !== 'count_distinct'))
              .map(([a, l]) => `<option value="${a}" ${a === CFG.agg ? 'selected' : ''}>${l} (${a})</option>`).join('')}
          </select></div>
          ${def.options ? Object.keys(def.options).filter(k => typeof def.options[k] === 'number').map(k => {
            const lim = k === 'interval_ms' ? [200, 5000] : [3, 500];
            const nm = { top_n: '상위 N', interval_ms: '프레임 간격(ms)' }[k] || k;
            return `<div class="bind-row"><label>${nm}</label>
            <input type="number" data-numopt="${esc(k)}" min="${lim[0]}" max="${lim[1]}" value="${esc(CFG.options[k] ?? def.options[k])}"></div>`;
          }).join('') : ''}
        </div>
        ${renderOptToggles(def)}
      </div>
    </section>
    <section><h3>필터 <button class="btn ghost" id="f-add" style="margin-left:8px;padding:2px 8px;font-size:10.5px">+ 추가</button></h3>
      <div id="f-list">${CFG.filters.map(filterRow).join('') || '<p class="bind-hint">필터 없음 — 소스 전체를 집계합니다.</p>'}</div>
    </section>
    ${(() => {
      const combos = RECO.combos(src, CFG.type);   // 소스×도표 형태에서 즉석 계산 — 테이블이 바뀌어도 그 모양에 맞게
      if (!combos.length) return '';
      return `<section><h3>추천 조합 — 누르면 위 설정에 바로 적용됩니다</h3>
        <div class="combo-row">${combos.map((c, i) => `
          <button class="combo-chip ${CFG.comboIdx === i ? 'on' : ''}" data-ci="${i}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M13 2L4 14h6l-1 8 9-12h-6l1-8z"/></svg>
            ${esc(c.label)}</button>`).join('')}
        </div></section>`;
    })()}
    <section><h3>미리보기</h3>
      <div class="preview-box"><div class="plot"></div><div class="hint">아래 '미리보기'를 누르면 여기에 그려집니다</div></div>
    </section>`;

  $('cfg-name').oninput = e => { CFG.title = e.target.value; CFG.titleTouched = true; renderCfgTabs(); };
  body.querySelectorAll('select[data-slot]').forEach(s => s.onchange = () => {
    if (s.value) CFG.bindings[s.dataset.slot] = s.value; else delete CFG.bindings[s.dataset.slot];
    CFG.comboIdx = null;   // 수동으로 만졌으면 추천 조합 선택 표시 해제
    body.querySelectorAll('.combo-chip').forEach(ch => ch.classList.remove('on'));
    renderCfgTabs();
  });
  body.querySelectorAll('.combo-chip').forEach(ch => ch.onclick = () => {
    const combo = RECO.combos(src, CFG.type)[Number(ch.dataset.ci)];
    if (!combo) return;
    CFG.bindings = { ...combo.bindings };
    CFG.agg = combo.agg;
    CFG.options = { ...CFG.options, ...combo.options };
    CFG.comboIdx = Number(ch.dataset.ci);
    if (!CFG.titleTouched) CFG.title = combo.label;
    renderCfgBind(body);          // 위 폼(슬롯·집계·상위 N·토글)에 반영
    $('cfg-preview').click();     // 적용 즉시 미리보기
  });
  $('cfg-agg').onchange = e => { CFG.agg = e.target.value; };
  body.querySelectorAll('input[data-numopt]').forEach(inp => inp.onchange = e => {
    const k = inp.dataset.numopt;
    const lo = Number(inp.min), hi = Number(inp.max);
    CFG.options[k] = Math.max(lo, Math.min(hi, Number(e.target.value) || Number(inp.min)));
  });
  body.querySelectorAll('.opt-toggle input').forEach(t => t.onchange = () => { CFG.options[t.dataset.opt] = t.checked; });
  $('f-add').onclick = () => { CFG.filters.push({ field: src.fields[0].name, op: 'eq', value: '' }); renderCfgBind(body); };
  body.querySelectorAll('#f-list [data-fi]').forEach(row => {
    const i = Number(row.dataset.fi), f = CFG.filters[i];
    row.querySelector('.ff').onchange = e => { f.field = e.target.value; };
    row.querySelector('.fo').onchange = e => { f.op = e.target.value; };
    row.querySelector('.fv').onchange = e => { f.value = parseFilterValue(f, e.target.value); };
    row.querySelector('.fdel').onclick = () => { CFG.filters.splice(i, 1); renderCfgBind(body); };
  });
  renderCfgTabs();
}

function renderOptToggles(def) {
  if (!def.options) return '';
  const toggles = Object.keys(def.options).filter(k => typeof def.options[k] === 'boolean');
  if (!toggles.length) return '';
  const NAMES = { horizontal: '가로 막대', stacked: '누적', area: '면적 채움', smooth: '곡선', donut: '도넛',
                  cumulative: '누적 값(경주)' };
  return `<div style="display:flex;gap:14px;flex-wrap:wrap">` + toggles.map(k => `
    <label class="opt-toggle" style="display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--ink-2)">
      <input type="checkbox" data-opt="${k}" ${CFG.options[k] ?? def.options[k] ? 'checked' : ''}>${NAMES[k] || k}</label>`).join('') + '</div>';
}

function parseFilterValue(f, raw) {
  if (f.op === 'in') return raw.split(',').map(s => s.trim()).filter(Boolean);
  const src = CFG.src, fl = src.fields.find(x => x.name === f.field);
  if (fl && (fl.role === 'measure' || fl.role === 'sequence') && raw !== '' && !Number.isNaN(Number(raw))) return Number(raw);
  return raw;
}

$('cfg-preview').onclick = async () => {
  if (!requiredBound()) { toast('필수 슬롯을 먼저 연결하세요'); return; }
  const box = document.querySelector('.preview-box');
  const hint = box.querySelector('.hint');
  hint.textContent = '조회 중…';
  try {
    const draft = currentDraft();
    const { b, def } = resolveBindings(draft, CFG.src);
    const { spec, alias } = buildSpec(draft, CFG.src, b);
    const res = await API.query(spec);
    if (!res.rows.length) { hint.textContent = '데이터 0행 — 필터를 확인하세요'; return; }
    hint.style.display = 'none';
    await RENDER.render(box.querySelector('.plot'), {
      el: box, chart: draft, b, src: CFG.src, rows: res.rows, cols: res.columns,
      geo: def.geo || null,
      regionRole: b.region ? (CFG.src.fields.find(f => f.name === b.region) || {}).role : null,
      valueLabel: draft.agg === 'count' ? '건수' : `${fieldLabel(CFG.src, b.value || b.x)} ${AGG_LABEL[draft.agg] || ''}`.trim(),
      xLabel: b.x ? fieldLabel(CFG.src, b.x) : '', yLabel: b.y ? fieldLabel(CFG.src, b.y) : '',
      colLabels: {},
    });
    $('cfg-note').textContent = `${res.row_count}행 · ${res.mode}${res.mode === 'live' ? ` · ${res.elapsed_ms}ms` : ''}`;
  } catch (err) {
    hint.style.display = ''; hint.textContent = '실패: ' + err.message;
  }
};

$('cfg-apply').onclick = async () => {
  if (!requiredBound()) return;
  if (!S.page) { toast('레이아웃 페이지를 먼저 추가하세요'); return; }
  const cfg = currentDraft();
  if (CFG.mode === 'add') {
    const chart = { ...cfg, id: uid(), grid: {} };
    if (!S.edit) enterEdit();
    S.page.charts.push(chart);
    addTile(chart);
    S.dirty = true;
    toast('차트를 추가했습니다 — 배치 후 레이아웃 저장을 누르세요');
  } else {
    const chart = S.page.charts.find(c => c.id === CFG.chartId);
    Object.assign(chart, { title: cfg.title, type: cfg.type, source: cfg.source,
                           bindings: cfg.bindings, agg: cfg.agg, filters: cfg.filters, options: cfg.options });
    const rec = S.tiles[chart.id];
    if (rec) {
      rec.el.querySelector('.tt b').textContent = chart.title || '차트';
      rec.el.querySelector('.tt span').textContent = chart.source;
      if (rec.inst) { rec.inst.dispose(); rec.inst = null; }
      loadTile(chart);
    }
    S.dirty = true;
    toast('차트를 수정했습니다 — 레이아웃 저장으로 확정하세요');
  }
  closeCfg();
};

/* ── 상단 동기화 표시 + 자동 갱신 ─────────────────────────── */
const REFRESH = { sec: 600, nextAt: 0, lastAt: 0, ticker: null };

function updateSync() {
  const modes = Object.values(S.modes);
  const stale = modes.filter(m => m === 'stale').length;
  const live = modes.filter(m => m === 'live').length;
  const pulse = $('pulse');
  pulse.className = 'pulse' + (stale ? ' warn' : '');
  let note = 'gold 직결 · Trino';
  if (REFRESH.sec > 0 && REFRESH.nextAt) {
    const left = Math.max(0, Math.round((REFRESH.nextAt - Date.now()) / 1000));
    note = `갱신 ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')} 후`;
    if (REFRESH.lastAt) note = `업데이트 ${new Date(REFRESH.lastAt).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })} · ` + note;
  }
  $('sync-note').textContent = stale ? `stale 캐시 ${stale}건 — Trino 확인 필요` : note;
  $('foot-mode').textContent = stale ? 'stale cache' : (live ? 'live' : 'cache');
}

/* 특정 간격으로 gold 를 재질의해 화면 갱신 — 캐시를 우회(force)해 항상 최신을 가져온다.
 * 편집 중이거나 탭이 백그라운드면 건너뛰고, 다시 보이면 밀린 갱신을 즉시 수행한다. */
function refreshBaseline() {
  REFRESH.nextAt = REFRESH.sec > 0 ? Date.now() + REFRESH.sec * 1000 : 0;
}
function doAutoRefresh() {
  REFRESH.lastAt = Date.now();
  refreshBaseline();
  document.body.dataset.refreshCount = String(Number(document.body.dataset.refreshCount || 0) + 1);
  if (!S.page) return;
  S.page.charts.forEach(c => loadTile(c, true, true));
}
function setRefreshSec(sec, save = true) {
  REFRESH.sec = sec;
  if (save) localStorage.setItem('charts.refreshSec', String(sec));
  const sel = $('refresh-sel');
  if ([...sel.options].some(o => Number(o.value) === sec)) sel.value = String(sec);
  refreshBaseline();
  updateSync();
}
function initAutoRefresh() {
  const urlSec = Number(new URLSearchParams(location.search).get('refresh'));   // 개발/검증용 오버라이드
  const saved = localStorage.getItem('charts.refreshSec');
  const sec = urlSec > 0 ? urlSec : (saved !== null ? Number(saved) : 600);
  setRefreshSec(Number.isFinite(sec) && sec >= 0 ? sec : 600, false);
  $('refresh-sel').onchange = e => { setRefreshSec(Number(e.target.value)); toast(Number(e.target.value) ? '자동 갱신 간격을 변경했습니다' : '자동 갱신을 껐습니다'); };
  REFRESH.ticker = setInterval(() => {
    if (REFRESH.sec <= 0 || !REFRESH.nextAt) return;
    if (Date.now() >= REFRESH.nextAt) {
      if (document.hidden || S.edit) { REFRESH.nextAt = Date.now() + 5000; return; }  // 잠시 뒤 재확인
      doAutoRefresh();
    } else updateSync();
  }, 1000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && REFRESH.sec > 0 && Date.now() >= REFRESH.nextAt && !S.edit) doAutoRefresh();
  });
}

/* ── 부트 ─────────────────────────────────────────────────── */
async function boot() {
  const grid = GridStack.init({
    column: 12, cellHeight: 84, margin: 8, float: false,
    staticGrid: true, animate: true,
  }, '#grid');
  S.grid = grid;
  grid.on('change', () => { if (S.edit) S.dirty = true; });
  grid.on('resizestop', (e, el) => {
    setTimeout(() => {
      const inst = echarts.getInstanceByDom(el.querySelector('.plot'));
      if (inst) inst.resize();
    }, 60);
  });
  let rsTimer;
  addEventListener('resize', () => {
    clearTimeout(rsTimer);
    rsTimer = setTimeout(() => Object.values(S.tiles).forEach(r => r.inst && r.inst.resize()), 120);
  });
  addEventListener('beforeunload', e => { if (S.edit && S.dirty) { e.preventDefault(); e.returnValue = ''; } });

  $('btn-edit').onclick = () => {
    if (!S.page) { toast('레이아웃 페이지를 먼저 추가하세요'); return; }
    S.edit ? saveLayout() : enterEdit();
  };
  $('btn-cancel-edit').onclick = async () => {
    if (S.dirty) {
      const ok = await modal({ title: '변경 취소', desc: '저장하지 않은 배치/차트 변경을 되돌릴까요?', okLabel: '되돌리기', danger: true });
      if (!ok) return;
    }
    exitEdit();
    S.page = await API.page(S.pageId);
    await renderPage();
  };
  $('btn-add-chart').onclick = () => openCfg('add');
  $('add-page').onclick = async () => {
    const name = await modal({ title: '레이아웃 추가', desc: '새 레이아웃 페이지 이름을 입력하세요.', input: '새 레이아웃', okLabel: '추가' });
    if (!name) return;
    const page = await API.createPage(name);
    S.pages = await API.pages();
    await switchPage(page.id);
    enterEdit();
    toast('빈 레이아웃입니다 — 차트 추가로 시작하세요');
  };

  try {
    const [meta, srcs, pages] = await Promise.all([API.meta(), API.sources('all'), API.pages()]);
    S.meta = meta; S.sources = srcs.sources; S.pages = pages;
    CFG.domain = meta.default_domain || 'all';
    RENDER.setMeta(meta);
    RECO.setMeta(meta);
    initAutoRefresh();
    updateSync();
    const urlPage = new URLSearchParams(location.search).get('page');
    const saved = urlPage || localStorage.getItem('charts.pageId');
    const first = pages.find(p => p.id === saved) ? saved : (pages[0] && pages[0].id);
    await switchPage(first || null, { force: true });
  } catch (err) {
    $('sync-note').textContent = 'API 연결 실패';
    $('pulse').className = 'pulse bad';
    toast('초기화 실패: ' + err.message);
  }
}

/* ── 셀프테스트(?selftest=1) — 실브라우저에서 핵심 플로우를 자동 검증 ── */
async function selftest() {
  const R = [];
  const ok = (name, cond) => R.push(`${cond ? 'PASS' : 'FAIL'} ${name}`);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  try {
    ok('pages loaded', S.pages.length >= 3);
    await switchPage('seed-overview', { force: true });
    await sleep(4000);
    ok('tiles rendered', Object.keys(S.tiles).length === S.page.charts.length);
    ok('no tile errors', !document.querySelector('.tile-state.err'));

    // 편집 모드 + 프로그램적 이동 → 저장 → 영속 확인
    enterEdit();
    ok('edit mode grid unlocked', !S.grid.opts.staticGrid);
    const firstEl = S.grid.engine.nodes[0].el;
    const movedId = firstEl.dataset.chartId;
    const before = JSON.stringify((S.page.charts.find(c => c.id === movedId) || {}).grid);
    S.grid.update(firstEl, { x: 6, y: 8 });
    ok('move marks dirty', S.dirty);
    await saveLayout();
    const reloaded = await API.page(S.pageId);
    const moved = reloaded.charts.find(c => c.id === movedId);
    ok('moved position persisted', moved && moved.grid.x === 6);
    // 원복
    enterEdit();
    S.grid.update(firstEl, JSON.parse(before));
    await saveLayout();

    // 차트 추가 → 저장 → 삭제 → 저장
    enterEdit();
    const chart = { id: uid(), title: '셀프테스트 LQ', type: 'bar', source: 'gold_license_gu_specialization',
                    bindings: { axis: 'gu', value: 'lq' }, agg: 'max', filters: [], options: { top_n: 10 }, grid: {} };
    S.page.charts.push(chart); addTile(chart); S.dirty = true;
    await saveLayout();
    ok('chart add persisted', (await API.page(S.pageId)).charts.some(c => c.id === chart.id));
    await sleep(2500);
    ok('added chart rendered', !!S.tiles[chart.id] && !S.tiles[chart.id].el.querySelector('.tile-state.err'));
    enterEdit(); removeTile(chart.id); S.dirty = true; await saveLayout();
    ok('chart delete persisted', !(await API.page(S.pageId)).charts.some(c => c.id === chart.id));

    // 레이아웃 페이지 CRUD + 순서변경
    const p = await API.createPage('셀프테스트 페이지');
    S.pages = await API.pages();
    ok('page created', S.pages.some(x => x.id === p.id));
    await API.patchPage(p.id, { name: '이름변경 확인' });
    ok('page renamed', (await API.pages()).some(x => x.id === p.id && x.name === '이름변경 확인'));
    const ids = (await API.pages()).map(x => x.id);
    [ids[0], ids[1]] = [ids[1], ids[0]];
    const after = await API.reorderPages(ids);
    ok('pages reordered', after[0].id === ids[0]);
    [ids[0], ids[1]] = [ids[1], ids[0]];
    await API.reorderPages(ids);
    await API.deletePage(p.id);
    ok('page deleted', !(await API.pages()).some(x => x.id === p.id));
    S.pages = await API.pages(); renderSidebar();

    // 온톨로지 폴백 — 사라진 필드는 같은 role 로 재바인딩
    const src = S.srcDetails['gold_license_gu_specialization'] || await API.source('gold_license_gu_specialization');
    const fb = resolveBindings({ type: 'bar', source: src.name,
      bindings: { axis: 'gone_field', value: 'lq' } }, src);
    ok('ontology fallback rebinds', fb.b.axis === 'gu' && fb.rebound.length === 1);

    // 자동 추천 — role 형태만으로 도표·조합이 나와야 한다 (테이블 무관 일반 규칙)
    const reco = RECO.types(src);
    ok('reco highlights bar+map', reco.bar && reco.bar.score >= 2 && reco.map_seoul && reco.map_seoul.score >= 3
       && reco.bar.reason.length > 10);
    const combos = RECO.combos(src, 'bar');
    ok('reco combos bindable', combos.length >= 1 && combos.every(c => {
      try { return !!resolveBindings({ type: 'bar', source: src.name, bindings: c.bindings }, src).b.axis; }
      catch { return false; }
    }));
    const flowSrc = S.srcDetails['gold_license_flow_monthly'] || await API.source('gold_license_flow_monthly');
    const recoFlow = RECO.types(flowSrc);
    ok('reco prefers line for time source', recoFlow.line && recoFlow.line.score >= 3);
    ok('reco suggests race for time source', recoFlow.race && recoFlow.race.score >= 3
       && RECO.combos(flowSrc, 'race').length >= 1);
  } catch (err) {
    R.push('FAIL exception: ' + err.message);
  }
  const div = document.createElement('div');
  div.id = 'selftest';
  div.style.display = 'none';
  div.textContent = R.join('\n');
  document.body.appendChild(div);
  document.title = R.every(r => r.startsWith('PASS')) ? 'SELFTEST_ALL_PASS' : 'SELFTEST_FAIL';
}

AuthUI.bootstrapProtected().then(boot).then(async () => {
  const q = new URLSearchParams(location.search);
  if (q.get('selftest') === '1') selftest();
  if (q.get('uidemo')) {           // 개발/검증용: 편집모드·드로어를 열어둔 상태로 진입
    enterEdit();
    if (q.get('uidemo') === 'drawer') openCfg('add');
    if (q.get('uidemo') === 'bind') {
      openCfg('add');
      CFG.source = 'gold_license_gu_specialization';
      CFG.src = await API.source(CFG.source);
      CFG.type = 'bar'; autoBind(); CFG.step = 'bind'; renderCfg();
    }
    if (q.get('uidemo') === 'type') {
      openCfg('add');
      CFG.source = 'gold_license_flow_monthly';
      CFG.src = await API.source(CFG.source);
      CFG.step = 'type'; renderCfg();
    }
  }
});
