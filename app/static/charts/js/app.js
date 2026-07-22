/* Charts Studio 오케스트레이터 — 레이아웃 페이지·그리드·편집모드·구성 드로어.
 * 온톨로지 원칙: 차트 저장물은 {type, source, bindings(슬롯→필드), agg, options} 뿐이고,
 * 렌더 시점마다 소스의 현재 필드/role 로 재해석한다. 필드가 사라지면 같은 role 로 폴백. */
'use strict';

const S = {
  meta: null, sources: [], srcDetails: {},
  pages: [], pageId: null, page: null,
  edit: false, dirty: false,
  canEdit: false,
  booting: true, switching: false, saving: false, pageAction: false,
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
  if (!$('modal').hidden) return Promise.resolve(null);
  return new Promise(resolve => {
    const returnFocus = document.activeElement;
    $('modal-title').textContent = title;
    $('modal-desc').textContent = desc;
    const wrap = $('modal-input-wrap'), inp = $('modal-input');
    wrap.hidden = input == null;
    if (input != null) { inp.value = input; setTimeout(() => { inp.focus(); inp.select(); }, 30); }
    const ok = $('modal-ok'), cancel = $('modal-cancel');
    ok.textContent = okLabel;
    ok.className = 'btn primary' + (danger ? ' danger' : '');
    $('modal').hidden = false;
    if (input == null) setTimeout(() => cancel.focus(), 0);
    const done = v => {
      $('modal').hidden = true;
      ok.onclick = cancel.onclick = inp.onkeydown = null;
      if (returnFocus && returnFocus.isConnected) returnFocus.focus();
      resolve(v);
    };
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
  if (e.key === 'Escape') { hideCtx(); if (!$('modal').hidden) $('modal-cancel').click(); else if (CFG.open) requestCloseCfg(); }
  if (e.key === 'Tab' && !$('modal').hidden) {
    const focusable = [...$('modal').querySelectorAll('button:not(:disabled), input:not(:disabled)')]
      .filter(el => el.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    return;
  }
  if (e.key === 'Tab' && CFG.open && $('modal').hidden) {
    const focusable = [...$('cfg-drawer').querySelectorAll(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])'
    )].filter(el => el.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
});

/* ── 온톨로지 바인딩 해석 ─────────────────────────────────── */
function typeDef(t) {
  const contracts = S.meta.chart_contracts || S.meta.chart_types;
  return contracts[t];
}
function fieldsByRole(src, accepts) {
  return src.fields.filter(f => f.chartable !== false && accepts.includes(f.role));
}
/* 슬롯 배정 규칙 — 서버 ontology.field_matches_slot 과 동일 계약.
 * role 일치 외에 ① 저카디널리티 groupable(실측 distinct≤50 measure)은 category 축 허용,
 * ② 구간 폭(bins[슬롯])이 지정된 measure 는 binnable 축 허용(히스토그램형). */
function fieldMatchesSlot(field, slot, bins) {
  if (slot.accepts.includes(field.role)) return true;
  if (field.groupable && slot.accepts.includes('category')) return true;
  return field.role === 'measure' && !!slot.binnable && Number((bins || {})[slot.name]) > 0;
}
/* 구간 기본 폭 — 실측 min/max 범위를 12구간 안팎의 1·2·5·10 단위로 반올림 */
function niceBinWidth(field) {
  const span = Number(field.max) - Number(field.min);
  if (!Number.isFinite(span) || span <= 0) return 1;
  const raw = span / 12;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const unit = raw / pow;
  return (unit >= 5 ? 10 : unit >= 2 ? 5 : unit >= 1 ? 2 : 1) * pow;
}
function slotRequired(slot, chart) {
  return !!slot.required && !((chart.agg || 'sum') === 'count' && slot.count_optional);
}

function resolveBindings(chart, src) {
  const def = typeDef(chart.type);
  if (!def) throw new Error(`알 수 없는 도표 타입: ${chart.type}`);
  const requested = chart.bindings || {};
  const bins = chart.bins || {};
  const candidates = slot => {
    const matches = src.fields.filter(f => f.chartable !== false && fieldMatchesSlot(f, slot, bins));
    const want = requested[slot.name];
    return want
      ? [...matches.filter(field => field.name === want), ...matches.filter(field => field.name !== want)]
      : matches;
  };
  // heatmap처럼 슬롯의 허용 role이 겹칠 때 앞 슬롯의 탐욕 선택이 뒤 슬롯을 막지 않도록
  // 후보가 적은 필수 슬롯부터 백트래킹한다. 서버 compatible_bindings와 같은 불변식이다.
  const required = def.slots
    .filter(slot => slotRequired(slot, chart))
    .sort((a, b) => candidates(a).length - candidates(b).length);
  const assign = (index, used, result) => {
    if (index === required.length) return result;
    const slot = required[index];
    for (const field of candidates(slot)) {
      if (used.has(field.name)) continue;
      const found = assign(index + 1, new Set([...used, field.name]),
        { ...result, [slot.name]: field.name });
      if (found) return found;
    }
    return null;
  };
  const b = assign(0, new Set(), {});
  if (!b) {
    const slot = required.find(item => !candidates(item).length) || required[0];
    throw new Error(`'${src.label}' 에 ${slot.label}(${slot.accepts.join('/')}) 역할 필드 조합이 없습니다`);
  }
  const used = new Set(Object.values(b));
  // 선택 슬롯은 사용자가 명시했을 때만 살린다. count(*)의 값 슬롯은 물리 필드를
  // 요구하지 않으므로 오래된 저장물에 값이 남아 있어도 정규화해 제거한다.
  def.slots.filter(slot => !slotRequired(slot, chart)).forEach(slot => {
    const want = requested[slot.name];
    if (!want || ((chart.agg || 'sum') === 'count' && slot.count_optional)) return;
    const field = candidates(slot).find(candidate => !used.has(candidate.name));
    if (field) { b[slot.name] = field.name; used.add(field.name); }
  });
  const rebound = [];
  def.slots.forEach(slot => {
    const want = requested[slot.name];
    if (want && b[slot.name] !== want) rebound.push(`${want}→${b[slot.name] || '없음'}`);
  });
  return { b, rebound, def };
}

function fieldLabel(src, name) {
  const f = src.fields.find(f => f.name === name);
  return f ? f.label : name;
}

/* 식별 승격 — 표시 필드(이름/한글) 바인딩을 동반 코드 필드(id_field)로 바꿔 집계한다.
 * 저장물(chart.bindings)은 그대로 두고 쿼리·렌더에만 적용: GROUP BY 가 항상 코드가 되어
 * 동명 지역(신사동: 강남·관악)이 합산되지 않고, 화면 표기는 value_labels(코드→한글)가 맡는다.
 * 반환 promoted = {승격된필드: 원래필드} — 컬럼 라벨을 사용자가 고른 필드명으로 유지할 때 쓴다. */
function effectiveBindings(src, b) {
  const be = {}, promoted = {};
  const used = new Set(Object.values(b));
  Object.entries(b).forEach(([slot, name]) => {
    const f = src.fields.find(x => x.name === name);
    const target = f && f.id_field
      && src.fields.some(x => x.name === f.id_field)
      && !used.has(f.id_field)                       // 코드 필드가 이미 다른 슬롯에 있으면 유지
      ? f.id_field : name;
    if (target !== name) { promoted[target] = name; used.add(target); }
    be[slot] = target;
  });
  return { be, promoted };
}
const AGG_LABEL = { sum: '합계', avg: '평균', count: '건수', count_distinct: '고유수', min: '최소', max: '최대' };
const FILTER_OP_LABEL = {
  eq: '같음', neq: '같지 않음',
  gt: '초과 (>)', gte: '이상 (≥)', lt: '미만 (<)', lte: '이하 (≤)',
  between: '범위 안(이상~이하)', not_between: '범위 밖',
  in: '목록 중 하나', not_in: '목록 제외',
  contains: '포함(부분일치)', starts_with: '~(으)로 시작', ends_with: '~(으)로 끝남',
  like: '문자 패턴(%·_ 직접)', not_like: '패턴 불일치(고급)',
  is_null: '값 없음', not_null: '값 있음',
  last_n: '최근 N(일/개월/년)',
};
const NO_VALUE_OPS = ['is_null', 'not_null'];
const ARRAY_VALUE_OPS = ['in', 'not_in', 'between', 'not_between'];
const HAVING_OP_LABEL = { gte: '이상 (≥)', gt: '초과 (>)', lte: '이하 (≤)', lt: '미만 (<)', eq: '같음', neq: '같지 않음' };
/* 그룹 판별 — 서버 querybuilder._is_group/models._filter_kind 와 동일 한 문장 */
const isFilterGroup = node => !!node && Object.hasOwn(node, 'logic');
/* data-fi 경로("i" 또는 "i.j") → {leaf, list, index, group} */
function filterAt(path) {
  const [i, j] = String(path).split('.').map(Number);
  const top = CFG.filters[i];
  return Number.isInteger(j)
    ? { leaf: top.filters[j], list: top.filters, index: j, group: top }
    : { leaf: top, list: CFG.filters, index: i, group: null };
}
/* 트리 워커 — [사람용 라벨, leaf] 순회 (검증·표시 공용) */
function* filterLeaves() {
  let groupNo = 0;
  for (const [i, node] of CFG.filters.entries()) {
    if (isFilterGroup(node)) {
      groupNo++;
      for (const [j, leaf] of node.filters.entries()) yield [`그룹 ${groupNo}·조건 ${j + 1}`, leaf];
    } else {
      yield [`필터 ${i + 1}`, node];
    }
  }
}

function allowedAggs(chart, src, bindings = chart.bindings || {}) {
  const def = typeDef(chart.type);
  if (!def || !src) return [];
  let allowed = Object.keys(AGG_LABEL)
    .filter(agg => !def.aggs || def.aggs.includes(agg));
  const options = { ...(def.options || {}), ...(chart.options || {}) };
  (def.agg_constraints || []).forEach(rule => {
    const active = Object.entries(rule.when || {}).every(([key, value]) => options[key] === value);
    if (active) allowed = allowed.filter(agg => (rule.allowed || []).includes(agg));
  });
  // 집계 제약은 '집계되는 슬롯'(value/x/y 처럼 measure 를 받는 슬롯)의 필드에만 적용 —
  // groupable/구간 축으로 바인딩된 measure 는 그룹 키라 집계되지 않는다(서버와 동일 규칙).
  const slotByName = Object.fromEntries((def.slots || []).map(slot => [slot.name, slot]));
  const measureFields = Object.entries(bindings)
    .filter(([slotName]) => (slotByName[slotName]?.accepts || []).includes('measure'))
    .map(([, name]) => src.fields.find(field => field.name === name))
    .filter(field => field && field.role === 'measure');
  measureFields.forEach(field => {
    if (field.allowed_aggs) allowed = allowed.filter(agg => field.allowed_aggs.includes(agg));
  });
  if (chart.type === 'race' && options.cumulative) {
    const valueSlot = def.slots.find(slot => slot.name === 'value');
    const valueField = valueSlot && src.fields.find(field => field.name === bindings.value);
    if ((chart.agg || 'sum') === 'count' || !valueField?.cumulative_safe) allowed = [];
  }
  const missingCountValue = def.slots.some(slot =>
    slot.count_optional && slot.required
    && !bindings[slot.name]
    && fieldsByRole(src, slot.accepts).length === 0);
  if (missingCountValue) allowed = allowed.filter(agg => agg === 'count');
  return allowed;
}

function supportsAfterAvailability(src) {
  return (src.supports || []).filter(type => {
    try {
      resolveBindings({ type, agg: 'sum', bindings: {} }, src);
      return true;
    } catch {
      try {
        resolveBindings({ type, agg: 'count', bindings: {} }, src);
        return true;
      } catch { return false; }
    }
  });
}

const AVAILABILITY_RECHECK_MS = 10 * 60 * 1000;
function availabilityFresh(src) {
  return !!src.__availabilityChecked
    && Date.now() - Number(src.__availabilityCheckedAt || 0) < AVAILABILITY_RECHECK_MS;
}

async function sourceForConfig(name) {
  const src = S.srcDetails[name] || await API.source(name);
  S.srcDetails[name] = src;
  if (!src.__catalogSupports) src.__catalogSupports = [...(src.supports || [])];
  src.fields.forEach(field => {
    if (field.__catalogChartable == null) field.__catalogChartable = field.chartable !== false;
  });
  if (availabilityFresh(src)) return src;
  try {
    const availability = await API.availability(name);
    src.__availabilityMode = availability.mode;
    if (availability.mode !== 'stale') {
      src.supports = [...src.__catalogSupports];
      src.fields.forEach(field => {
        field.chartable = field.__catalogChartable;
        delete field.unavailable;
        field.non_null_count = Number(availability.fields[field.name] || 0);
        if (field.non_null_count === 0) {
          field.chartable = false;
          field.unavailable = true;
        }
      });
      src.supports = supportsAfterAvailability(src);
      delete src.__availabilityWarning;
      const summary = S.sources.find(source => source.name === name);
      if (summary) summary.supports = [...src.supports];
    } else {
      src.__availabilityWarning = '필드 가용성이 stale 캐시라 선택 후보를 제거하지 않았습니다';
    }
  } catch (err) {
    // Trino 장애 중에도 기존 stale 타일은 열어야 한다. 새 설정에는 검증 불가를 명시하고
    // 적용 전 미리보기의 전부-null 방어를 마지막 안전망으로 둔다.
    src.__availabilityWarning = `실데이터 가용성 확인 실패: ${err.message}`;
    // 일시 장애를 영구 캐시하지 않는다. 다음 설정 진입에서는 다시 확인한다.
    src.__availabilityChecked = false;
    return src;
  }
  src.__availabilityChecked = true;
  src.__availabilityCheckedAt = Date.now();
  return src;
}

function buildSpec(chart, src, b) {
  const agg = chart.agg || 'sum';
  if (!allowedAggs(chart, src, b).includes(agg)) {
    throw new Error(`선택한 필드·옵션에는 ${AGG_LABEL[agg] || agg} 집계를 사용할 수 없습니다`);
  }
  const o = optionsForType(chart.type, chart.options || {});
  const alias = agg === 'count' ? 'count' : `${agg}_${b.value || b.x || 'v'}`;
  const m = f => ({ field: agg === 'count' ? null : f, agg, alias: agg === 'count' ? 'count' : `${agg}_${f}` });
  const spec = { source: chart.source, dims: [], measures: [], filters: chart.filters || [],
                 filters_logic: chart.filters_logic || 'and', order_by: [], limit: 1000 };
  const t = chart.type;
  // 집계 조건(having)은 축 있는 도표에만 — stat 은 무차원이라 서버가 거부한다
  if (t !== 'stat' && Array.isArray(chart.having) && chart.having.length) {
    spec.having = chart.having;
  }
  // 구간 축 — bins[슬롯]이 지정되면 dim 을 {field, bin_width} 로 (별칭=필드명, 소비자 동일)
  const bins = chart.bins || {};
  const dimFor = slot => (Number(bins[slot]) > 0
    ? { field: b[slot], bin_width: Number(bins[slot]) } : b[slot]);

  if (t === 'stat') { spec.measures = [m(b.value)]; }
  else if (t === 'bar' || t === 'line' || t === 'area') {
    spec.dims = b.series ? [dimFor('axis'), b.series] : [dimFor('axis')];
    spec.measures = [m(b.value)];
    // limit 은 서버 상한(5000)까지 — 절단이 피벗/합계를 왜곡하는 것을 최소화 (도달 시 타일에 경고)
    if (t === 'bar') { spec.order_by = [{ field: m(b.value).alias, dir: 'desc' }]; spec.limit = 5000; }
    else { spec.order_by = [{ field: b.axis, dir: 'asc' }]; spec.limit = 5000; }
  }
  else if (t === 'pie') {
    spec.dims = [dimFor('axis')]; spec.measures = [m(b.value)];
    spec.order_by = [{ field: m(b.value).alias, dir: 'desc' }]; spec.limit = 500;
  }
  else if (t === 'scatter') {
    const sm = f => ({ field: f, agg, alias: `${agg}_${f}` });
    spec.dims = [b.axis]; spec.measures = [sm(b.x), sm(b.y)];
    spec.order_by = [{ field: sm(b.x).alias, dir: 'desc' }];
    spec.limit = o.top_n || 300;
  }
  else if (t === 'heatmap') { spec.dims = [dimFor('x'), dimFor('y')]; spec.measures = [m(b.value)]; spec.limit = 5000; }
  else if (t === 'race') {
    spec.dims = [b.time, b.axis]; spec.measures = [m(b.value)];
    spec.order_by = [{ field: b.time, dir: 'asc' }]; spec.limit = 5000;
  }
  else if (t === 'map_points') {
    spec.dims = [b.lat, b.lng]; spec.measures = [m(b.value)]; spec.limit = o.top_n || 4000;
  }
  else if (t.startsWith('map_')) { spec.dims = [b.region]; spec.measures = [m(b.value)]; spec.limit = 800; }
  else if (t === 'table') {
    spec.dims = [dimFor('axis')]; spec.measures = [m(b.value)];
    spec.order_by = [{ field: m(b.value).alias, dir: 'desc' }]; spec.limit = o.top_n || 50;
  }
  return { spec, alias };
}

function hasRenderableMeasures(chart, spec, response) {
  if (!response.rows.length) return false;
  const indexes = spec.measures
    .map(measure => measure.alias
      || (measure.field ? `${measure.agg}_${measure.field}` : 'count'))
    .map(alias => response.columns.indexOf(alias))
    .filter(index => index >= 0);
  if (!indexes.length) return false;
  const numeric = value => value != null && value !== '' && Number.isFinite(Number(value));
  if ((chart.agg || 'sum') === 'count') {
    if (!response.rows.some(row => indexes.some(index => numeric(row[index]) && Number(row[index]) > 0))) {
      return false;
    }
  } else if (chart.type === 'scatter') {
    if (!response.rows.some(row => indexes.every(index => numeric(row[index])))) return false;
  } else if (chart.type === 'pie') {
    const values = response.rows.flatMap(row => indexes.map(index => row[index]))
      .filter(numeric).map(Number);
    if (!(values.length > 0 && values.some(value => value > 0) && values.every(value => value >= 0))) {
      return false;
    }
  } else if (!response.rows.some(row => indexes.some(index => numeric(row[index])))) {
    return false;
  }
  if (chart.type === 'line' || chart.type === 'race') {
    const d0 = spec.dims[0];
    const progressionIndex = response.columns.indexOf(typeof d0 === 'object' ? d0.field : d0);
    if (progressionIndex < 0) return false;
    const points = new Set(response.rows
      .filter(row => indexes.some(index => numeric(row[index])))
      .map(row => row[progressionIndex])
      .filter(value => value != null && value !== '')
      .map(String));
    if (points.size < 2) return false;
  }
  return true;
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
  const gen = (rec.loadGen || 0) + 1;
  rec.loadGen = gen;
  const current = () => S.tiles[chart.id] === rec && rec.loadGen === gen;
  const el = rec.el;
  const plot = el.querySelector('.plot');
  const refreshButton = el.querySelector('.rf');
  refreshButton.disabled = true;
  // quiet(자동 갱신) 이고 이미 그려져 있으면 스피너로 화면을 가리지 않는다 — 무깜빡임 갱신
  if (!quiet || !rec.inst) tileState(el, '<div class="spin"></div>');
  try {
    const src = S.srcDetails[chart.source] || (S.srcDetails[chart.source] = await API.source(chart.source));
    if (!current()) return;
    chart.options = optionsForType(chart.type, chart.options || {});
    const { b, rebound, def } = resolveBindings(chart, src);
    // 사라진 필드의 role 폴백 결과를 in-memory 저장물에도 반영한다. 그렇지 않으면
    // 화면은 정상인데 다음 레이아웃 저장에서 오래된 필드명 때문에 전체 저장이 실패한다.
    if (JSON.stringify(chart.bindings || {}) !== JSON.stringify(b)) chart.bindings = { ...b };
    // role 폴백으로 축 필드가 바뀌었으면 measure 가 아닌 슬롯의 구간(bins)은 무효 — 정리
    Object.keys(chart.bins || {}).forEach(slot => {
      const bound = src.fields.find(x => x.name === b[slot]);
      if (!bound || bound.role !== 'measure') delete chart.bins[slot];
    });
    const { be, promoted } = effectiveBindings(src, b);
    const { spec, alias } = buildSpec(chart, src, be);
    if (force) spec.force = true;               // 신선 캐시 무시 ('다시 조회')
    const res = await API.query(spec);
    if (!current()) return;                    // 재조회/페이지 전환의 늦은 응답 폐기
    tileState(el, null);

    const ctx = {
      el, chart, b: be, src,
      rows: res.rows, cols: res.columns,
      geo: def.geo || null,
      regionRole: be.region ? (src.fields.find(f => f.name === be.region) || {}).role : null,
      valueLabel: chart.agg === 'count' ? '건수' : `${fieldLabel(src, b.value || b.x)} ${AGG_LABEL[chart.agg || 'sum'] || ''}`.trim(),
      xLabel: b.x ? fieldLabel(src, b.x) : '', yLabel: b.y ? fieldLabel(src, b.y) : '',
      colLabels: {},
      raceState: RENDER.raceSnapshot(rec.inst),
      isCurrent: current,
    };
    // 승격된 컬럼의 헤더/툴팁 라벨은 사용자가 고른 표시 필드명으로 유지한다
    ctx.colLabels = Object.fromEntries(res.columns.map(c =>
      [c, c === alias ? ctx.valueLabel : fieldLabel(src, promoted[c] || c)]));
    if (!hasRenderableMeasures(chart, spec, res)) {
      const old = echarts.getInstanceByDom(plot);
      if (old) RENDER.dispose(old);             // 직전 렌더 잔상이 '데이터 없음' 뒤로 비치지 않게
      plot.innerHTML = '';
      rec.inst = null;
      tileState(el, `<span>${res.rows.length
        ? '측정값이 모두 비어 있거나 이 도표에 사용할 수 없습니다'
        : '데이터가 없습니다 — 필터를 확인하세요'}</span>`);
    } else {
      const inst = await RENDER.render(plot, ctx);
      if (!current()) {
        if (inst) RENDER.dispose(inst);
        return;
      }
      rec.inst = inst;
      if (rec.inst) {
        plotRO.observe(plot);
        requestAnimationFrame(() => rec.inst && rec.inst.resize());
      } else {
        plotRO.unobserve(plot);
      }
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
    if (!current()) return;
    const st = tileState(el, `<div class="tile-state err"><b>렌더 실패</b>
      <span class="detail" title="${esc(err.message)}">${esc(err.message)}</span>
      <button class="btn ghost retry">다시 시도</button></div>`);
    st.querySelector('.retry').onclick = () => loadTile(chart, true);
    st.className = 'tile-state err';
  } finally {
    if (current()) refreshButton.disabled = false;
  }
}

function addTile(chart, load = true) {
  const el = tileDom(chart);
  S.grid.el.appendChild(el);
  S.grid.makeWidget(el);
  S.tiles[chart.id] = { el, inst: null, chart, loadGen: 0 };
  if (load) loadTile(chart);
  $('empty-board').hidden = S.page.charts.length > 0;
}

function removeTile(chartId) {
  const rec = S.tiles[chartId]; if (!rec) return;
  rec.loadGen++;
  plotRO.unobserve(rec.el.querySelector('.plot'));
  if (rec.inst) RENDER.dispose(rec.inst);
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
  Object.values(S.tiles).forEach(r => {
    r.loadGen++;
    plotRO.unobserve(r.el.querySelector('.plot'));
    if (r.inst) RENDER.dispose(r.inst);
  });
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

function setSwitchBusy(busy) {
  S.switching = busy;
  const locked = busy || S.booting || S.saving || S.pageAction;
  $('canvas').setAttribute('aria-busy', String(busy));
  ['btn-edit', 'btn-add-chart', 'btn-cancel-edit', 'add-page', 'mobile-page-menu']
    .forEach(id => { $(id).disabled = locked || !S.canEdit; });
  document.querySelectorAll('.page-item').forEach(button => { button.disabled = locked; });
  $('mobile-page').disabled = locked;
  $('mobile-add-page').disabled = locked || !S.canEdit;
}

async function switchPage(id, { force } = {}) {
  if (S.saving) {
    toast('레이아웃 저장이 끝난 뒤 이동하세요');
    renderSidebar();
    return false;
  }
  if (!force && S.edit && S.dirty) {
    const ok = await modal({ title: '저장하지 않은 변경', desc: '레이아웃 변경 내용을 저장하지 않고 이동할까요?', okLabel: '이동', danger: true });
    if (!ok) return false;
  }
  if (S.edit) exitEdit();
  const gen = ++switchGen;
  setSwitchBusy(true);
  try {
    const page = id ? await API.page(id) : null;
    if (gen !== switchGen) return false;   // 그 사이 다른 전환이 시작됨 — 이 응답은 폐기
    // pageId/page를 한 번에 확정한다. 조회 중에는 이전 페이지가 새 ID로 저장되지 않는다.
    S.pageId = id;
    S.page = page;
    localStorage.setItem('charts.pageId', id || '');
    renderSidebar();
    await renderPage();
    return true;
  } catch (err) {
    if (gen === switchGen) toast('페이지 전환 실패: ' + err.message);
    return false;
  } finally {
    if (gen === switchGen) setSwitchBusy(false);
  }
}

/* ── 사이드탭 ─────────────────────────────────────────────── */
const PAGE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M10 10v10"/></svg>';

function renderSidebar() {
  const box = $('pages');
  box.innerHTML = '';
  S.pages.forEach((p, i) => {
    const b = document.createElement('button');
    b.className = 'page-item' + (p.id === S.pageId ? ' on' : '');
    b.disabled = S.switching || S.saving || S.pageAction;
    b.innerHTML = `${PAGE_ICON}<span class="nm">${esc(p.name)}</span><span class="cnt">${p.chart_count}</span>`;
    b.onclick = () => { if (p.id !== S.pageId) switchPage(p.id); };
    b.oncontextmenu = S.canEdit ? e => { e.preventDefault(); pageCtx(e, p, i); } : null;
    box.appendChild(b);
  });
  const mobile = $('mobile-page');
  mobile.innerHTML = S.pages.map(page =>
    `<option value="${esc(page.id)}" ${page.id === S.pageId ? 'selected' : ''}>${esc(page.name)} (${page.chart_count})</option>`
  ).join('');
  mobile.hidden = S.pages.length === 0;
  $('mobile-page-menu').hidden = !S.canEdit || S.pages.length === 0;
  mobile.onchange = () => { if (mobile.value !== S.pageId) switchPage(mobile.value); };
}

function pageCtx(e, p, i) {
  if (!S.canEdit) return;
  if (S.switching || S.saving || S.pageAction) {
    toast(S.saving ? '레이아웃을 저장하는 중입니다'
      : S.pageAction ? '레이아웃 작업을 처리하는 중입니다' : '페이지를 불러오는 중입니다');
    return;
  }
  showCtx(e.clientX, e.clientY, [
    { label: '위로 이동', disabled: i === 0, run: () => movePage(i, -1) },
    { label: '아래로 이동', disabled: i === S.pages.length - 1, run: () => movePage(i, 1) },
    'hr',
    { label: '이름 변경', run: async () => {
        const name = await modal({ title: '레이아웃 이름 변경', input: p.name, okLabel: '변경' });
        if (!name) return;
        await runPageAction('이름 변경', async () => {
          await API.patchPage(p.id, { name });
          p.name = name;
          if (p.id === S.pageId) { S.page.name = name; $('crumb-page').textContent = name; }
          renderSidebar(); toast('이름을 변경했습니다');
        });
      } },
    { label: '복제', run: async () => {
        await runPageAction('레이아웃 복제', async () => {
          const copy = await API.duplicatePage(p.id);
          S.pages = await API.pages(); renderSidebar();
          toast(`'${copy.name}' 레이아웃을 만들었습니다`);
        });
      } },
    'hr',
    { label: '삭제', danger: true, run: async () => {
        const ok = await modal({ title: '레이아웃 삭제', desc: `'${p.name}' 레이아웃과 차트 ${p.chart_count}개 구성이 삭제됩니다.`, okLabel: '삭제', danger: true });
        if (!ok) return;
        await runPageAction('레이아웃 삭제', async () => {
          await API.deletePage(p.id);
          S.pages = await API.pages();
          if (p.id === S.pageId
              && !await switchPage(S.pages.length ? S.pages[0].id : null, { force: true })) {
            throw new Error('삭제 후 다음 레이아웃을 불러오지 못했습니다');
          }
          else renderSidebar();
          toast('레이아웃을 삭제했습니다');
        });
      } },
  ]);
}

async function runPageAction(label, action) {
  if (S.pageAction) { toast('이미 레이아웃 작업을 처리하는 중입니다'); return false; }
  S.pageAction = true;
  setSwitchBusy(false);
  try {
    await action();
    return true;
  } catch (err) {
    toast(`${label} 실패: ${err.message}`);
    return false;
  } finally {
    S.pageAction = false;
    setSwitchBusy(false);
  }
}

async function movePage(i, delta) {
  if (S.switching || S.saving || S.pageAction) return;
  await runPageAction('레이아웃 순서 변경', async () => {
    const ids = S.pages.map(p => p.id);
    const j = i + delta;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    S.pages = await API.reorderPages(ids);
    renderSidebar();
  });
}

async function addLayoutPageFlow() {
  if (!S.canEdit) { toast('일반회원 이상만 레이아웃을 추가할 수 있습니다'); return; }
  if (S.booting || S.switching || S.saving || S.pageAction) {
    toast(S.booting ? 'Charts Studio를 불러오는 중입니다' : '다른 레이아웃 작업이 끝난 뒤 추가하세요');
    return;
  }
  const name = await modal({
    title: '레이아웃 추가',
    desc: '새 레이아웃 페이지 이름을 입력하세요.',
    input: '새 레이아웃',
    okLabel: '추가',
  });
  if (!name) return;
  await runPageAction('레이아웃 추가', async () => {
    const page = await API.createPage(name);
    S.pages = await API.pages();
    if (!await switchPage(page.id)) throw new Error('새 레이아웃을 불러오지 못했습니다');
    enterEdit();
    toast('빈 레이아웃입니다 — 차트 추가로 시작하세요');
  });
}

/* ── 편집 모드 ────────────────────────────────────────────── */
function setActButtons() {
  document.querySelectorAll('.tile .acts .ed, .tile .acts .del')
    .forEach(b => { b.hidden = !S.edit; });
}
function enterEdit() {
  if (!S.canEdit) { toast('일반회원 이상만 레이아웃을 변경할 수 있습니다'); return; }
  if (S.booting || S.switching) { toast('페이지를 불러온 뒤 다시 시도하세요'); return; }
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
  if (S.switching || S.saving) {
    toast(S.saving ? '이미 레이아웃을 저장하는 중입니다' : '페이지 전환이 끝난 뒤 저장하세요');
    return false;
  }
  collectGrid();
  const targetPageId = S.pageId;
  const charts = JSON.parse(JSON.stringify(S.page.charts));
  const btn = $('btn-edit');
  S.saving = true;
  setSwitchBusy(false);
  S.grid.setStatic(true);
  btn.classList.add('saving');
  try {
    await API.patchPage(targetPageId, { charts });
    S.pages = await API.pages(); renderSidebar();
    if (S.pageId === targetPageId) exitEdit();
    toast('레이아웃을 저장했습니다');
    return true;
  } catch (err) {
    toast('저장 실패: ' + err.message);
    return false;
  } finally {
    S.saving = false;
    setSwitchBusy(false);
    if (S.edit) S.grid.setStatic(false);
    btn.classList.remove('saving');
  }
}

/* ── 구성 드로어 (소스 → 도표 → 연결) ─────────────────────── */
const CFG = { open: false, mode: 'add', chartId: null, step: 'source',
              source: null, src: null, type: null, bindings: {}, bins: {}, agg: 'sum',
              title: '', titleTouched: false, filters: [], filtersLogic: 'and', having: [],
              options: {}, domain: 'all', search: '',
              dirty: false, sourceGen: 0, previewGen: 0, previewKey: null,
              previewBusy: false, loadError: '', returnFocus: null };

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
  hideRecoTip();
  CFG.open = true; CFG.mode = mode;
  CFG.returnFocus = document.activeElement;
  CFG.dirty = false; CFG.previewKey = null; CFG.previewBusy = false; CFG.loadError = '';
  if (mode === 'edit' && chart) {
    Object.assign(CFG, {
      chartId: chart.id, source: chart.source, type: chart.type,
      src: null,
      bindings: { ...(chart.bindings || {}) }, bins: { ...(chart.bins || {}) },
      agg: chart.agg || 'sum',
      title: chart.title, titleTouched: true,
      filters: JSON.parse(JSON.stringify(chart.filters || [])),
      filtersLogic: chart.filters_logic || 'and',
      having: JSON.parse(JSON.stringify(chart.having || [])),
      options: optionsForType(chart.type, chart.options || {}), step: 'bind',
      comboIdx: null,
    });
    $('cfg-mode-label').textContent = '차트 편집';
    $('cfg-apply').textContent = '적용';
  } else {
    Object.assign(CFG, { chartId: null, source: null, src: null, type: null, bindings: {}, bins: {},
                         agg: 'sum', title: '', titleTouched: false,
                         filters: [], filtersLogic: 'and', having: [], options: {},
                         step: 'source', search: '', comboIdx: null });
    $('cfg-mode-label').textContent = '차트 추가';
    $('cfg-apply').textContent = '추가';
  }
  $('cfg-overlay').classList.add('on');
  $('cfg-drawer').classList.add('on');
  $('cfg-drawer').removeAttribute('inert');
  $('cfg-drawer').setAttribute('aria-hidden', 'false');
  if (mode === 'edit') {
    // 캐시가 있으면 동기 설정 — 최초 편집 클릭에서 CFG.src=null 렌더 크래시 방지
    const cached = S.srcDetails[CFG.source] || null;
    CFG.src = cached && availabilityFresh(cached) ? cached : null;
    if (!CFG.src) {
      const sourceName = CFG.source;
      const gen = ++CFG.sourceGen;
      sourceForConfig(sourceName).then(src => {
        S.srcDetails[src.name] = src;
        if (CFG.open && gen === CFG.sourceGen && CFG.source === src.name) {
          CFG.src = src; renderCfg();
        }
      }).catch(err => {
        if (CFG.open && gen === CFG.sourceGen && CFG.source === sourceName) {
          CFG.loadError = err.message; renderCfg();
        }
      });
    }
  }
  renderCfg();
  $('cfg-preview').disabled = false;
  $('cfg-preview').textContent = '미리보기';
  setTimeout(() => { if (CFG.open) $('cfg-close').focus(); }, 0);
}
function disposePreview() {
  const plot = document.querySelector('.preview-box .plot');
  if (!plot) return;
  const pv = echarts.getInstanceByDom(plot);
  if (pv) RENDER.dispose(pv);
}
function resetPreview(message) {
  const box = document.querySelector('.preview-box');
  if (!box) return;
  disposePreview();
  box.querySelectorAll('.race-controls').forEach(control => control.remove());
  const plot = box.querySelector('.plot');
  if (plot) plot.innerHTML = '';
  const hint = box.querySelector('.hint');
  if (hint) {
    hint.style.display = '';
    hint.textContent = message;
  }
}
function closeCfg() {
  hideRecoTip();
  disposePreview();
  CFG.open = false;
  invalidatePreview();
  $('cfg-overlay').classList.remove('on');
  $('cfg-drawer').classList.remove('on');
  $('cfg-drawer').setAttribute('inert', '');
  $('cfg-drawer').setAttribute('aria-hidden', 'true');
  const target = CFG.returnFocus;
  CFG.returnFocus = null;
  if (target && target.isConnected) target.focus();
}
async function requestCloseCfg() {
  if (!CFG.open) return;
  if (CFG.dirty) {
    const ok = await modal({
      title: '차트 구성을 닫을까요?',
      desc: '아직 적용하지 않은 설정이 사라집니다.',
      okLabel: '닫기',
      danger: true,
    });
    if (!ok) { $('cfg-close').focus(); return; }
  }
  closeCfg();
}
$('cfg-close').onclick = requestCloseCfg;
$('cfg-overlay').onclick = requestCloseCfg;

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
    b.onclick = () => {
      hideRecoTip();
      if (CFG.step !== b.dataset.step) invalidatePreview();
      CFG.step = b.dataset.step;
      renderCfg();
    };
  });
  $('cfg-title').textContent = CFG.title || (CFG.src ? `${CFG.src.label}` : '새 차트');
  $('cfg-apply').disabled = !!draftProblem() || CFG.previewBusy || $('cfg-apply').dataset.busy === '1';
}
function requiredBound() {
  return !draftProblem();
}
function currentDraft() {
  return { id: CFG.chartId || 'draft', title: CFG.title, type: CFG.type, source: CFG.source,
           bindings: { ...CFG.bindings }, bins: { ...CFG.bins }, agg: CFG.agg,
           filters: JSON.parse(JSON.stringify(CFG.filters)),
           filters_logic: CFG.filtersLogic || 'and',
           having: JSON.parse(JSON.stringify(CFG.having || [])),
           options: optionsForType(CFG.type, CFG.options) };
}
function previewFingerprint() {
  const draft = currentDraft();
  delete draft.id; delete draft.title;
  return JSON.stringify(draft);
}
function invalidatePreview() {
  CFG.previewKey = null;
  CFG.previewGen++;
  CFG.previewBusy = false;
  if (CFG.open) resetPreview('설정이 변경되었습니다 — 다시 미리보기');
  const button = $('cfg-preview');
  if (button) { button.disabled = false; button.textContent = '미리보기'; }
  if (CFG.open) $('cfg-note').textContent = '';
}
function markCfgDirty() {
  CFG.dirty = true;
  invalidatePreview();
}
function optionsForType(type, options = {}) {
  const contract = (typeDef(type) || {}).options || {};
  return Object.fromEntries(
    Object.keys(contract)
      .filter(key => Object.hasOwn(options, key))
      .map(key => {
        const fallback = contract[key];
        if (typeof fallback === 'boolean') {
          return [key, typeof options[key] === 'boolean' ? options[key] : fallback];
        }
        const bounds = key === 'interval_ms' ? [200, 5000] : [1, 5000];
        const numeric = Number(options[key]);
        const normalized = Number.isFinite(numeric) ? Math.round(numeric) : fallback;
        return [key, Math.max(bounds[0], Math.min(bounds[1], normalized))];
      })
  );
}

function filterOpsFor(field) {
  if (!field) return [];
  if (Array.isArray(field.allowed_filter_ops) && field.allowed_filter_ops.length) {
    return field.allowed_filter_ops;
  }
  // 서버 querybuilder.allowed_filter_ops 미러(구 스냅샷 폴백) — 서버 변경 시 함께 갱신
  const codeStrict = ['geo_gu_code', 'geo_dong_code', 'geo_legal_code', 'id'].includes(field.role);
  if (codeStrict) return ['eq', 'neq', 'in', 'not_in', 'is_null', 'not_null'];
  if (['measure', 'sequence', 'ordinal'].includes(field.role)) {
    return ['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'between', 'not_between',
            'in', 'not_in', 'is_null', 'not_null'];
  }
  if (field.role === 'time') {
    const ops = ['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'between', 'not_between',
                 'in', 'not_in', 'is_null', 'not_null'];
    if (['year', 'month', 'date', 'datetime'].includes(field.granularity)) ops.push('last_n');
    return ops;
  }
  if (['category', 'geo_gu', 'geo_dong', 'geo_sido', 'geo_legal_dong', 'geo_country'].includes(field.role)) {
    return ['eq', 'neq', 'in', 'not_in', 'contains', 'starts_with', 'ends_with',
            'like', 'not_like', 'is_null', 'not_null'];
  }
  return ['eq', 'neq', 'in', 'not_in', 'is_null', 'not_null'];
}

function leafProblem(label, item) {
  const field = CFG.src.fields.find(candidate => candidate.name === item.field);
  if (!field) return `${label}의 필드를 선택하세요`;
  if (field.unavailable) return `${label}의 필드는 실제 값이 없어 사용할 수 없습니다`;
  if (!filterOpsFor(field).includes(item.op)) return `${label}의 조건이 필드와 맞지 않습니다`;
  if (NO_VALUE_OPS.includes(item.op)) return null;   // 값 없음/있음 — 값 검사 전체 면제
  if (item.op === 'last_n') {
    const n = Number(item.value);
    return Number.isInteger(n) && n >= 1 ? null : `${label}의 최근 N 값은 1 이상의 정수여야 합니다`;
  }
  const value = item.value;
  const values = ARRAY_VALUE_OPS.includes(item.op) ? value : [value];
  if (!Array.isArray(values) || !values.length
      || (['between', 'not_between'].includes(item.op) && values.length !== 2)) {
    return `${label}의 값을 확인하세요`;
  }
  if (values.some(v => v == null || (typeof v === 'string' && !v.trim()))) {
    return `${label}의 값은 비워둘 수 없습니다`;
  }
  const textOps = ['contains', 'starts_with', 'ends_with', 'like', 'not_like'];
  if (!textOps.includes(item.op)
      && ['measure', 'sequence', 'ordinal'].includes(field.role)
      && values.some(v => !Number.isFinite(Number(v)))) {
    return `${label}에는 숫자를 입력하세요`;
  }
  return null;
}

function filterProblem() {
  if (!CFG.src) return null;
  let groupNo = 0;
  for (const node of CFG.filters) {
    if (isFilterGroup(node) && !node.filters.length) {
      return `그룹 ${++groupNo}에 조건을 추가하거나 그룹을 삭제하세요`;
    }
    if (isFilterGroup(node)) groupNo++;
  }
  for (const [label, leaf] of filterLeaves()) {
    const problem = leafProblem(label, leaf);
    if (problem) return problem;
  }
  if ((CFG.having || []).length && CFG.type === 'stat') {
    return '집계 결과 조건은 축이 있는 도표에서만 쓸 수 있습니다 — 도표를 바꾸거나 조건을 삭제하세요';
  }
  for (let i = 0; i < (CFG.having || []).length; i++) {
    const h = CFG.having[i];
    if (h.agg !== 'count' && !CFG.src.fields.some(f => f.name === h.field && f.role === 'measure')) {
      return `집계 조건 ${i + 1}의 대상 측정값을 선택하세요`;
    }
    // Number('')===0 함정 — 빈 값/공백을 유한수로 오인하지 않게 명시 검사
    if (h.value == null || (typeof h.value === 'string' && !h.value.trim())
        || !Number.isFinite(Number(h.value))) {
      return `집계 조건 ${i + 1}의 값은 숫자여야 합니다`;
    }
  }
  return null;
}

function ensureValidAgg() {
  if (!CFG.src || !CFG.type) return;
  const allowed = allowedAggs(currentDraft(), CFG.src, CFG.bindings);
  if (allowed.includes(CFG.agg)) return;
  const def = typeDef(CFG.type);
  const preferred = def.slots
    .map(slot => CFG.bindings[slot.name])
    .map(name => CFG.src.fields.find(field => field.name === name && field.role === 'measure'))
    .filter(Boolean)
    .map(field => field.preferred_agg)
    .find(agg => allowed.includes(agg));
  CFG.agg = preferred || allowed[0] || 'sum';
}

function draftProblem() {
  if (!CFG.source) return '데이터 소스를 선택하세요';
  if (!CFG.src) return CFG.loadError || '소스 정보를 불러오는 중입니다';
  if (!CFG.type) return '도표를 선택하세요';
  if (!CFG.src.supports.includes(CFG.type)) {
    return '현재 실데이터와 정책에서는 이 도표를 사용할 수 없습니다';
  }
  try {
    const { b } = resolveBindings(currentDraft(), CFG.src);
    if (!allowedAggs(currentDraft(), CFG.src, b).includes(CFG.agg)) {
      return '선택한 필드·옵션에 맞는 집계를 선택하세요';
    }
  } catch (err) {
    return err.message;
  }
  return filterProblem();
}

function renderCfg() {
  hideRecoTip();
  disposePreview();      // body 재작성 전에 미리보기 인스턴스 정리 (detached DOM 누수 방지)
  renderCfgTabs();
  const body = $('cfg-body');
  if (CFG.step === 'source') return renderCfgSource(body);
  if ((CFG.step === 'type' || CFG.step === 'bind') && !CFG.src) {
    body.innerHTML = CFG.loadError
      ? `<section><h3>소스 정보를 불러오지 못했습니다</h3>
          <div class="cfg-error">${esc(CFG.loadError)}
            <button class="btn ghost" id="cfg-source-back">소스 다시 선택</button></div></section>`
      : '<section><h3>소스 정보 로딩 중…</h3><div class="tile-state" style="position:static;padding:30px"><div class="spin"></div></div></section>';
    const back = $('cfg-source-back');
    if (back) back.onclick = () => { CFG.step = 'source'; CFG.source = null; CFG.loadError = ''; renderCfg(); };
    return;
  }
  if (CFG.step === 'type') return renderCfgType(body);
  try {
    return renderCfgBind(body);
  } catch (err) {
    body.innerHTML = `<section><h3>현재 실데이터로 이 구성을 사용할 수 없습니다</h3>
      <div class="cfg-error">${esc(err.message)}
        <button class="btn ghost" id="cfg-source-back">소스 다시 선택</button></div></section>`;
    $('cfg-source-back').onclick = () => { CFG.step = 'source'; renderCfg(); };
    $('cfg-note').textContent = 'null이 아닌 실제 값이 있는 필드만 선택할 수 있습니다';
    return;
  }
}

function renderCfgSource(body) {
  const selectable = new Set(Object.keys(S.meta.chart_types));
  const viable = S.sources.filter(source => source.supports.some(type => selectable.has(type)));
  const domainCounts = viable.reduce((counts, source) => {
    counts[source.domain] = (counts[source.domain] || 0) + 1;
    return counts;
  }, {});
  const domains = ['all', ...Object.keys(S.meta.domains).filter(domain => domainCounts[domain])];
  const labels = S.meta.domain_labels || {};
  const list = viable.filter(source => CFG.domain === 'all' || source.domain === CFG.domain);
  body.innerHTML = `
    <section><h3>데이터 소스 — gold 카탈로그</h3>
      <div class="src-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
        <input id="src-q" type="search" placeholder="소스 검색" value="${esc(CFG.search)}"></div>
      <div class="src-doms">${domains.map(d =>
        `<button class="chip ${CFG.domain === d ? 'on' : ''}" data-d="${esc(d)}">${esc(labels[d] || d)}
          <span>${d === 'all' ? viable.length : domainCounts[d]}</span></button>`).join('')}</div>
      <div class="src-list">${list.map(s => `
        <button class="src-item ${CFG.source === s.name ? 'on' : ''}" data-s="${esc(s.name)}"
                data-search="${esc((s.name + ' ' + s.label + ' ' + s.description).toLowerCase())}">
          <div class="info"><b>${esc(s.label)}</b><span>${esc(s.name)}</span></div>
          <span class="rows num">${Number(s.row_count).toLocaleString('ko-KR')}행 · ${s.supports.filter(t => selectable.has(t)).length}종</span>
        </button>`).join('')}
        <div class="src-empty" hidden>조건에 맞는 시각화 가능 소스가 없습니다</div>
      </div>
      ${S.sources.length === viable.length ? '' :
        `<p class="bind-hint">${S.sources.length - viable.length}개 소스는 현재 정책 또는 슬롯 계약상 구성 가능한 도표가 없어 선택에서 제외했습니다.</p>`}
    </section>`;
  const applySearch = () => {
    const query = CFG.search.trim().toLowerCase();
    let shown = 0;
    body.querySelectorAll('.src-item').forEach(item => {
      item.hidden = !!query && !item.dataset.search.includes(query);
      if (!item.hidden) shown++;
    });
    body.querySelector('.src-empty').hidden = shown > 0;
  };
  $('src-q').oninput = e => { CFG.search = e.target.value; applySearch(); };
  applySearch();
  body.querySelectorAll('.chip').forEach(c => c.onclick = () => { CFG.domain = c.dataset.d; renderCfgSource(body); });
  body.querySelectorAll('.src-item').forEach(el => el.onclick = async () => {
    const name = el.dataset.s;
    const gen = ++CFG.sourceGen;
    Object.assign(CFG, {
      source: name, src: null, type: null, bindings: {}, options: {}, filters: [],
      agg: 'sum', title: '', titleTouched: false, comboIdx: null, loadError: '', step: 'type',
    });
    markCfgDirty();
    renderCfg();
    try {
      const src = await sourceForConfig(name);
      if (!CFG.open || gen !== CFG.sourceGen || CFG.source !== name) return;
      if (!src.supports.some(type => Object.hasOwn(S.meta.chart_types, type))) {
        Object.assign(CFG, { source: null, src: null, type: null, step: 'source' });
        toast('실제 값이 있는 필드로 구성 가능한 도표가 없어 소스에서 제외했습니다');
        renderCfg();
        return;
      }
      CFG.src = src;
      renderCfg();
    } catch (err) {
      if (!CFG.open || gen !== CFG.sourceGen || CFG.source !== name) return;
      CFG.loadError = err.message;
      renderCfg();
    }
  });
}

/* 추천 이유 툴팁 — 도표 카드/조합 칩 공용 */
function recoTip() {
  let tip = $('reco-tip');
  if (!tip) { tip = document.createElement('div'); tip.id = 'reco-tip'; document.body.appendChild(tip); }
  return tip;
}
function hideRecoTip() {
  const tip = $('reco-tip');
  if (!tip) return;
  tip.classList.remove('on');
  tip.textContent = '';
}
function bindRecoTip(el, reason) {
  const show = () => {
    const tip = recoTip();
    tip.innerHTML = `<b>이 소스엔 이 도표</b>${esc(reason)}`;
    tip.classList.add('on');
    const r = el.getBoundingClientRect();
    tip.style.left = Math.min(r.left, innerWidth - 270) + 'px';
    tip.style.top = (r.bottom + 6) + 'px';
  };
  el.addEventListener('pointerenter', show);
  el.addEventListener('focus', show);
  el.addEventListener('pointerleave', hideRecoTip);
  el.addEventListener('blur', hideRecoTip);
  el.addEventListener('click', hideRecoTip);
}
addEventListener('resize', hideRecoTip);
document.addEventListener('scroll', hideRecoTip, true);

function renderCfgType(body) {
  const src = CFG.src;
  const reco = RECO.types(src);   // role 기반 — 어떤 소스가 와도 그 자리에서 계산
  const choices = Object.entries(S.meta.chart_types)
    .filter(([type]) => src.supports.includes(type))
    .sort(([a], [b]) => (reco[b]?.score || 0) - (reco[a]?.score || 0));
  body.innerHTML = `
    <section><h3>도표 타입 — '${esc(src.label)}' 가 지원하는 형태</h3>
      <div class="type-grid">${choices.map(([t, def]) => {
        const isReco = reco[t] && reco[t].score >= 2;
        return `
        <button class="type-card ${CFG.type === t ? 'on' : ''} ${isReco ? 'reco' : ''}" data-t="${t}">
          ${TYPE_ICONS[def.icon] || TYPE_ICONS.bar}<b>${esc(def.label)}</b>
        </button>`;
      }).join('')}</div>
      <p class="bind-hint" style="margin-top:10px">실제로 서로 다른 필드로 구성 가능한 도표만 추천 순으로 표시합니다.
        <span style="color:var(--accent-deep)">추천</span> 배지에 마우스를 올리거나 키보드 포커스를 두면 이유가 보입니다.</p>
    </section>`;
  body.querySelectorAll('.type-card').forEach(el => {
    const t = el.dataset.t;
    el.onclick = () => {
      hideRecoTip();
      CFG.type = t;
      CFG.options = {};
      CFG.agg = 'sum';
      autoBind();
      markCfgDirty();
      CFG.step = 'bind';
      renderCfg();
    };
    if (reco[t] && reco[t].score >= 2) bindRecoTip(el, reco[t].reason);
  });
}

function autoBind() {
  const def = typeDef(CFG.type), src = CFG.src;
  const b = {}, used = new Set();
  const dc = src.default_chart;
  const curatedHit = dc && dc.type === CFG.type;
  // 우선순위: 큐레이션 힌트 → 추천 엔진 1순위 조합 → role 첫 후보
  const combo = !curatedHit ? (RECO.combos(src, CFG.type)[0] || null) : null;
  if (curatedHit && dc.agg) CFG.agg = dc.agg;
  else if (combo) {
    CFG.agg = combo.agg;
    CFG.options = { ...CFG.options, ...combo.options };
  } else {
    const value = src.fields.find(field =>
      field.chartable !== false && field.role === 'measure'
      && (field.recommendation_priority ?? 40) < 90);
    CFG.agg = value?.preferred_agg || (def.slots.some(slot => slot.count_optional)
      && !src.fields.some(field => field.role === 'measure') ? 'count' : 'sum');
  }
  for (const slot of def.slots) {
    const want = (curatedHit && dc.bindings && dc.bindings[slot.name])
              || (combo && combo.bindings[slot.name]);
    const wf = want && src.fields.find(f =>
      f.name === want && f.chartable !== false
      && slot.accepts.includes(f.role) && !used.has(f.name));
    if (wf) { b[slot.name] = wf.name; used.add(wf.name); continue; }
    if (!slotRequired(slot, { agg: CFG.agg })) continue;
    const cands = fieldsByRole(src, slot.accepts)
      .filter(field => !used.has(field.name))
      .sort((a, b) => (a.recommendation_priority ?? 40) - (b.recommendation_priority ?? 40));
    if (cands.length) { b[slot.name] = cands[0].name; used.add(cands[0].name); }
  }
  CFG.bindings = b;
  CFG.comboIdx = combo ? 0 : null;
  ensureValidAgg();
  if (!CFG.titleTouched) CFG.title = combo ? combo.label : `${src.label} · ${def.label}`;
}

function renderCfgBind(body) {
  hideRecoTip();
  disposePreview();
  const def = typeDef(CFG.type), src = CFG.src;
  if (CFG.agg === 'count') {
    def.slots.filter(slot => slot.count_optional).forEach(slot => delete CFG.bindings[slot.name]);
  }
  // stat(무차원)으로 전환하면 잔존 having 을 정리한다 — 미리보기(스펙 제외)와 저장물이
  // 비대칭이면 레이아웃 저장 전체가 400 으로 막힌다(적대적 검증 결함 #1)
  if (CFG.type === 'stat' && (CFG.having || []).length) CFG.having = [];
  const normalized = resolveBindings(currentDraft(), src);
  CFG.bindings = { ...normalized.b };
  ensureValidAgg();
  const aggChoices = allowedAggs(currentDraft(), src, CFG.bindings);
  const combos = RECO.combos(src, CFG.type).filter(combo => {
    const candidate = {
      ...currentDraft(),
      bindings: combo.bindings,
      agg: combo.agg,
      options: { ...CFG.options, ...combo.options },
    };
    try {
      const { b } = resolveBindings(candidate, src);
      return allowedAggs(candidate, src, b).includes(candidate.agg);
    } catch { return false; }
  });
  const slotRow = slot => {
    const cur = CFG.bindings[slot.name] || '';
    const usedElsewhere = new Set(Object.entries(CFG.bindings)
      .filter(([name]) => name !== slot.name)
      .map(([, value]) => value));
    // 후보 = 규칙 일치(role/groupable/구간 지정 measure) ∪ binnable 슬롯의 모든 measure
    // (선택하는 순간 기본 구간 폭이 자동 설정되어 규칙을 충족하게 된다)
    const cands = src.fields
      .filter(f => f.chartable !== false
        && (fieldMatchesSlot(f, slot, CFG.bins) || (slot.binnable && f.role === 'measure')))
      .filter(field => !usedElsewhere.has(field.name));
    const countValue = CFG.agg === 'count' && slot.count_optional;
    const roleTag = f => f.role !== 'measure' ? f.role
      : (f.groupable ? 'measure·그룹' : (slot.binnable ? 'measure·구간' : 'measure'));
    const curField = src.fields.find(f => f.name === cur);
    const binRow = slot.binnable && curField && curField.role === 'measure'
      ? `<div class="bind-row"><label>구간 폭${curField.groupable ? '' : ' <span class="req">*</span>'}</label>
          <input type="number" data-binslot="${esc(slot.name)}" step="any" min="0"
                 value="${esc(CFG.bins[slot.name] ?? '')}"
                 placeholder="${esc(String(niceBinWidth(curField)))}${curField.groupable ? ' (비우면 값 그대로)' : ''}"></div>`
      : '';
    return `<div class="bind-row">
      <label>${esc(slot.label)} ${slot.required ? '<span class="req">*</span>' : ''}</label>
      <select data-slot="${esc(slot.name)}" ${countValue && !cands.length ? 'disabled' : ''}>
        ${countValue ? '<option value="">행 수 (count *)</option>'
          : (slot.required ? '' : '<option value="">(없음)</option>')}
        ${cands.map(f => `<option value="${esc(f.name)}" ${f.name === cur ? 'selected' : ''}>${esc(f.label)} — ${esc(f.name)} (${esc(roleTag(f))})</option>`).join('')}
      </select></div>${binRow}`;
  };
  const filterRow = (f, path) => {
    const field = src.fields.find(item => item.name === f.field);
    const enumLabels = (S.meta.value_labels || {})[f.field];
    // enum(코드→라벨) 필드에서는 부분일치/패턴을 UI 에서 숨긴다 — DB 엔 코드가 저장되어
    // 한글 라벨 부분일치가 0행이 되는 함정(서버는 저장물 호환 위해 계속 허용)
    // 현재 저장된 op 는 숨기지 않는다 — 목록에서 빠지면 select 가 첫 항목('같음')을
    // 오표시하면서 상태는 like 로 남는 표시·상태 불일치가 생긴다(저장물 하위호환)
    const ops = filterOpsFor(field).filter(op =>
      op === f.op
      || !(enumLabels && ['contains', 'starts_with', 'ends_with', 'like', 'not_like'].includes(op)));
    if (enumLabels && ['eq', 'neq'].includes(f.op)
        && !Object.hasOwn(enumLabels, String(f.value ?? ''))) f.value = '';
    const noValue = NO_VALUE_OPS.includes(f.op);
    const rangeOp = ['between', 'not_between'].includes(f.op);
    const displayValue = Array.isArray(f.value) ? f.value.join(',') : (f.value ?? '');
    // 값 위젯은 op 우선 디스패치 — 값 없는 op 에 enum select 가 뜨지 않게
    const valueControl = noValue
      ? '<span class="fv-none bind-hint" style="align-self:center">값 입력 없음</span>'
      : f.op === 'last_n'
        ? `<input type="number" class="fv" min="1" step="1" value="${esc(f.value ?? '')}"
            placeholder="N (${{ year: '년', month: '개월', date: '일', datetime: '일' }[field?.granularity] || '단위'})" ${field ? '' : 'disabled'}>`
      : rangeOp
        ? `<span style="display:flex;gap:4px;align-items:center">
            <input type="text" class="fv" data-vi="0" placeholder="최소" value="${esc(f.value?.[0] ?? '')}" ${field ? '' : 'disabled'}>~
            <input type="text" class="fv" data-vi="1" placeholder="최대" value="${esc(f.value?.[1] ?? '')}" ${field ? '' : 'disabled'}></span>`
      : enumLabels && ['eq', 'neq', 'in', 'not_in'].includes(f.op)
        ? `<select class="fv" ${['in', 'not_in'].includes(f.op) ? 'multiple size="4"' : ''} ${field ? '' : 'disabled'}>
            ${['eq', 'neq'].includes(f.op) ? '<option value="">값 선택</option>' : ''}
            ${Object.entries(enumLabels).map(([value, label]) => {
              const selected = Array.isArray(f.value)
                ? f.value.map(String).includes(value) : String(f.value ?? '') === value;
              return `<option value="${esc(value)}" ${selected ? 'selected' : ''}>${esc(label)} (${esc(value)})</option>`;
            }).join('')}</select>`
      : `<input type="text" class="fv" value="${esc(displayValue)}"
          placeholder="${['in', 'not_in'].includes(f.op) ? '쉼표로 구분'
            : ['contains', 'starts_with', 'ends_with'].includes(f.op) ? '문자 그대로 입력 (%·_ 불필요)' : '값 입력'}"
          ${field ? '' : 'disabled'}>`;
    return `
    <div class="bind-grid" data-fi="${esc(String(path))}" style="grid-template-columns: 1.2fr .7fr 1fr auto; align-items:end">
      <div class="bind-row"><label>필드</label><select class="ff">
        <option value="">필드 선택</option>${src.fields.filter(x => !x.unavailable).map(x =>
        `<option value="${esc(x.name)}" ${x.name === f.field ? 'selected' : ''}>${esc(x.label)} — ${esc(x.name)}</option>`).join('')}</select></div>
      <div class="bind-row"><label>조건</label><select class="fo" ${field ? '' : 'disabled'}>${ops.map(o =>
        `<option value="${o}" ${o === f.op ? 'selected' : ''}>${FILTER_OP_LABEL[o] || o}</option>`).join('')}</select></div>
      <div class="bind-row"><label>값</label>${valueControl}</div>
      <button class="btn ghost fdel" title="조건 삭제" style="height:33px">✕</button>
    </div>`;
  };
  // 최상위 노드: leaf 는 그대로, 그룹은 박스(logic 토글 + 내부 leaf + 조건 추가/그룹 삭제)
  const filterNode = (node, i) => {
    if (!isFilterGroup(node)) return filterRow(node, i);
    return `
    <div class="f-group" data-gi="${i}" style="border:1px solid #dfe4ec;border-radius:8px;padding:8px 10px;margin:6px 0">
      <div style="display:flex;gap:6px;align-items:center;margin-bottom:4px">
        <button class="btn ghost glogic" data-lg="${node.logic === 'or' ? 'and' : 'or'}"
                title="그룹 결합 방식 전환" style="padding:2px 8px;font-size:10.5px">
          ${node.logic === 'or' ? '하나라도 만족 (또는)' : '모두 만족 (그리고)'}</button>
        <button class="btn ghost gadd" style="padding:2px 8px;font-size:10.5px">+ 조건</button>
        <button class="btn ghost gdel" title="그룹 삭제" style="margin-left:auto;padding:2px 8px;font-size:10.5px">그룹 ✕</button>
      </div>
      ${node.filters.map((leaf, j) => filterRow(leaf, `${i}.${j}`)).join('')}
    </div>`;
  };
  const havingRow = (h, i) => {
    const measures = src.fields.filter(x => x.role === 'measure' && !x.unavailable);
    return `
    <div class="bind-grid" data-hi="${i}" style="grid-template-columns: .8fr 1.2fr .8fr .8fr auto; align-items:end">
      <div class="bind-row"><label>집계</label><select class="hagg">
        ${['count', 'sum', 'avg', 'min', 'max'].map(a =>
          `<option value="${a}" ${a === h.agg ? 'selected' : ''}>${AGG_LABEL[a] || a}</option>`).join('')}</select></div>
      <div class="bind-row"><label>대상</label><select class="hfield" ${h.agg === 'count' ? 'disabled' : ''}>
        <option value="">${h.agg === 'count' ? '행 수 (count *)' : '측정값 선택'}</option>
        ${measures.map(x => `<option value="${esc(x.name)}" ${x.name === h.field ? 'selected' : ''}>${esc(x.label)} — ${esc(x.name)}</option>`).join('')}</select></div>
      <div class="bind-row"><label>조건</label><select class="hop">
        ${Object.entries(HAVING_OP_LABEL).map(([o, l]) => `<option value="${o}" ${o === h.op ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
      <div class="bind-row"><label>값</label><input type="text" class="hval" value="${esc(h.value ?? '')}" placeholder="숫자"></div>
      <button class="btn ghost hdel" title="집계 조건 삭제" style="height:33px">✕</button>
    </div>`;
  };

  body.innerHTML = `
    <section><h3>온톨로지 연결 — 슬롯(역할) ↔ 필드</h3>
      <div class="bind-form">
        <div class="bind-row"><label>제목</label><input type="text" id="cfg-name" value="${esc(CFG.title)}" maxlength="60"></div>
        <div class="bind-grid">${def.slots.map(slotRow).join('')}
          <div class="bind-row"><label>집계</label><select id="cfg-agg">
            ${Object.entries(AGG_LABEL).filter(([a]) => aggChoices.includes(a))
              .map(([a, l]) => `<option value="${a}" ${a === CFG.agg ? 'selected' : ''}>${l} (${a})</option>`).join('')}
          </select></div>
          ${def.options ? Object.keys(def.options).filter(k => typeof def.options[k] === 'number').map(k => {
            const lim = k === 'interval_ms' ? [200, 5000] : [1, 5000];
            const nm = { top_n: '상위 N', interval_ms: '프레임 간격(ms)' }[k] || k;
            return `<div class="bind-row"><label>${nm}</label>
            <input type="number" data-numopt="${esc(k)}" min="${lim[0]}" max="${lim[1]}" step="1"
                   value="${esc(CFG.options[k] ?? def.options[k])}"></div>`;
          }).join('') : ''}
        </div>
        ${renderOptToggles(def)}
      </div>
    </section>
    <section><h3>필터
        <button class="btn ghost" id="f-add" style="margin-left:8px;padding:2px 8px;font-size:10.5px">+ 조건</button>
        <button class="btn ghost" id="g-add" style="padding:2px 8px;font-size:10.5px">+ OR 그룹</button></h3>
      <div id="f-list">${CFG.filters.map(filterNode).join('') || '<p class="bind-hint">필터 없음 — 소스 전체를 집계합니다.</p>'}</div>
      ${CFG.filters.length ? '<p class="bind-hint">조건들은 ‘그리고’로 결합됩니다. 그룹 안 조건은 그룹의 결합 방식(또는/그리고)을 따릅니다. 범위는 최소·최대 두 칸, 포함은 %·_ 없이 입력한 그대로 부분일치, 값 없음/있음 조건은 값을 입력하지 않습니다.</p>' : ''}
    </section>
    ${CFG.type === 'stat' ? '' : `<section><h3>집계 결과 조건
        <button class="btn ghost" id="h-add" style="margin-left:8px;padding:2px 8px;font-size:10.5px">+ 추가</button></h3>
      <div id="h-list">${(CFG.having || []).map(havingRow).join('') || '<p class="bind-hint">없음 — 예: ‘건수 10건 이상인 그룹만’(소표본 숨기기).</p>'}</div>
    </section>`}
    ${combos.length ? `<section><h3>추천 조합 — 누르면 위 설정에 바로 적용됩니다</h3>
        <div class="combo-row">${combos.map((c, i) => `
          <button class="combo-chip ${CFG.comboIdx === i ? 'on' : ''}" data-ci="${i}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M13 2L4 14h6l-1 8 9-12h-6l1-8z"/></svg>
            ${esc(c.label)}</button>`).join('')}
        </div></section>` : ''}
    <section><h3>미리보기</h3>
      <div class="preview-box"><div class="plot"></div><div class="hint">아래 '미리보기'를 누르면 여기에 그려집니다</div></div>
    </section>`;

  $('cfg-name').oninput = e => {
    CFG.title = e.target.value; CFG.titleTouched = true; CFG.dirty = true; renderCfgTabs();
  };
  body.querySelectorAll('select[data-slot]').forEach(s => s.onchange = () => {
    if (s.value) CFG.bindings[s.dataset.slot] = s.value; else delete CFG.bindings[s.dataset.slot];
    // 구간 상태 동기화 — 비-groupable measure 를 binnable 축에 놓으면 기본 폭 자동 설정,
    // measure 가 아니게 되면 구간 해제(서버 계약: 구간은 숫자 측정값에만)
    const slotDef = def.slots.find(x => x.name === s.dataset.slot);
    const picked = src.fields.find(x => x.name === s.value);
    if (slotDef?.binnable && picked?.role === 'measure') {
      if (!picked.groupable && !(Number(CFG.bins[slotDef.name]) > 0)) {
        CFG.bins[slotDef.name] = niceBinWidth(picked);
      }
    } else if (slotDef) {
      delete CFG.bins[slotDef.name];
    }
    CFG.comboIdx = null;   // 수동으로 만졌으면 추천 조합 선택 표시 해제
    ensureValidAgg();
    markCfgDirty();
    renderCfgBind(body);
  });
  body.querySelectorAll('input[data-binslot]').forEach(inp => inp.onchange = () => {
    const width = Number(inp.value);
    if (Number.isFinite(width) && width > 0) CFG.bins[inp.dataset.binslot] = width;
    else delete CFG.bins[inp.dataset.binslot];
    markCfgDirty();
    renderCfgBind(body);
  });
  body.querySelectorAll('.combo-chip').forEach(ch => ch.onclick = () => {
    const combo = combos[Number(ch.dataset.ci)];
    if (!combo) return;
    CFG.bindings = { ...combo.bindings };
    CFG.bins = {};        // 추천 조합은 role 기반 — 이전 구간 상태를 끌고 가지 않는다
    CFG.agg = combo.agg;
    CFG.options = { ...CFG.options, ...combo.options };
    CFG.comboIdx = Number(ch.dataset.ci);
    if (!CFG.titleTouched) CFG.title = combo.label;
    markCfgDirty();
    renderCfgBind(body);          // 위 폼(슬롯·집계·상위 N·토글)에 반영
    runPreview();                 // 적용 즉시 미리보기
  });
  $('cfg-agg').onchange = e => {
    CFG.agg = e.target.value;
    markCfgDirty();
    renderCfgBind(body);
  };
  body.querySelectorAll('input[data-numopt]').forEach(inp => inp.onchange = e => {
    const k = inp.dataset.numopt;
    const lo = Number(inp.min), hi = Number(inp.max);
    CFG.options[k] = Math.max(lo, Math.min(hi, Math.round(Number(e.target.value) || Number(inp.min))));
    ensureValidAgg();
    markCfgDirty();
    renderCfgBind(body);
  });
  body.querySelectorAll('.opt-toggle input').forEach(t => t.onchange = () => {
    CFG.options[t.dataset.opt] = t.checked;
    ensureValidAgg();
    markCfgDirty();
    renderCfgBind(body);
  });
  $('f-add').onclick = () => {
    CFG.filters.push({ field: '', op: 'eq', value: '' });
    markCfgDirty();
    renderCfgBind(body);
  };
  // 새 그룹 기본 logic 은 'or' — AND 그룹은 최상위 leaf 와 동치라 그룹을 만드는 동기가 보통 OR
  $('g-add').onclick = () => {
    CFG.filters.push({ logic: 'or', filters: [{ field: '', op: 'eq', value: '' }] });
    markCfgDirty();
    renderCfgBind(body);
  };
  body.querySelectorAll('#f-list .f-group').forEach(box => {
    const g = CFG.filters[Number(box.dataset.gi)];
    if (!isFilterGroup(g)) return;
    box.querySelector('.glogic').onclick = e => {
      g.logic = e.currentTarget.dataset.lg === 'or' ? 'or' : 'and';
      markCfgDirty();
      renderCfgBind(body);
    };
    box.querySelector('.gadd').onclick = () => {
      g.filters.push({ field: '', op: 'eq', value: '' });
      markCfgDirty();
      renderCfgBind(body);
    };
    box.querySelector('.gdel').onclick = () => {
      CFG.filters.splice(Number(box.dataset.gi), 1);
      markCfgDirty();
      renderCfgBind(body);
    };
  });
  body.querySelectorAll('#f-list [data-fi]').forEach(row => {
    const at = filterAt(row.dataset.fi);
    const f = at.leaf;
    if (!f) return;
    row.querySelector('.ff').onchange = e => {
      f.field = e.target.value;
      const field = src.fields.find(item => item.name === f.field);
      f.op = filterOpsFor(field)[0] || 'eq';
      f.value = '';
      markCfgDirty();
      renderCfgBind(body);
    };
    row.querySelector('.fo').onchange = e => {
      f.op = e.target.value;
      // op 모양 인지형 초기화 — between 2칸 / 값없음 null / 목록 [] / 그 외 ''
      f.value = ['between', 'not_between'].includes(f.op) ? ['', '']
        : NO_VALUE_OPS.includes(f.op) ? null
        : ['in', 'not_in'].includes(f.op) ? [] : '';
      markCfgDirty();
      renderCfgBind(body);
    };
    const rangeInputs = row.querySelectorAll('.fv[data-vi]');
    if (rangeInputs.length === 2) {
      // between 두 칸은 재렌더 없이 칸별 갱신 — 타이핑 중 상태 소실 방지
      rangeInputs.forEach(inp => inp.oninput = e => {
        if (!Array.isArray(f.value)) f.value = ['', ''];
        f.value[Number(inp.dataset.vi)] = parseFilterValue(
          { ...f, op: 'eq' }, e.target.value);
        markCfgDirty();
        renderCfgTabs();
        $('cfg-note').textContent = filterProblem() || '';
      });
    } else {
      const valueInput = row.querySelector('.fv');
      if (valueInput && valueInput.tagName !== 'SPAN') {
        const updateFilterValue = e => {
          const raw = e.target.multiple
            ? [...e.target.selectedOptions].map(option => option.value)
            : e.target.value;
          f.value = parseFilterValue(f, raw);
          markCfgDirty();
          renderCfgTabs();
          $('cfg-note').textContent = filterProblem() || '';
        };
        if (valueInput.tagName === 'SELECT') valueInput.onchange = updateFilterValue;
        else valueInput.oninput = updateFilterValue;
      }
    }
    row.querySelector('.fdel').onclick = () => {
      at.list.splice(at.index, 1);
      // 그룹이 비면 그룹 자동 제거 — 빈 그룹은 SQL 불가·검증 걸림보다 제거가 낫다
      if (at.group && !at.group.filters.length) {
        CFG.filters.splice(CFG.filters.indexOf(at.group), 1);
      }
      markCfgDirty();
      renderCfgBind(body);
    };
  });
  const hAdd = $('h-add');   // stat(무차원)에서는 섹션 자체가 없다
  if (hAdd) hAdd.onclick = () => {
    (CFG.having = CFG.having || []).push({ agg: 'count', field: null, op: 'gte', value: '' });
    markCfgDirty();
    renderCfgBind(body);
  };
  body.querySelectorAll('#h-list [data-hi]').forEach(row => {
    const h = CFG.having[Number(row.dataset.hi)];
    if (!h) return;
    row.querySelector('.hagg').onchange = e => {
      h.agg = e.target.value;
      if (h.agg === 'count') h.field = null;
      markCfgDirty();
      renderCfgBind(body);
    };
    row.querySelector('.hfield').onchange = e => {
      h.field = e.target.value || null;
      markCfgDirty();
      renderCfgBind(body);
    };
    row.querySelector('.hop').onchange = e => { h.op = e.target.value; markCfgDirty(); renderCfgTabs(); };
    row.querySelector('.hval').oninput = e => {
      const n = Number(e.target.value);
      h.value = e.target.value !== '' && Number.isFinite(n) ? n : e.target.value;
      markCfgDirty();
      renderCfgTabs();
      $('cfg-note').textContent = filterProblem() || '';
    };
    row.querySelector('.hdel').onclick = () => {
      CFG.having.splice(Number(row.dataset.hi), 1);
      markCfgDirty();
      renderCfgBind(body);
    };
  });
  renderCfgTabs();
  $('cfg-note').textContent = draftProblem() || src.__availabilityWarning || '';
}

function renderOptToggles(def) {
  if (!def.options) return '';
  const toggles = Object.keys(def.options).filter(k => typeof def.options[k] === 'boolean');
  if (!toggles.length) return '';
  const NAMES = { horizontal: '가로 막대', stacked: '누적', area: '면적 채움', smooth: '곡선', donut: '도넛',
                  cumulative: '누적 값(경주)' };
  return `<div style="display:flex;gap:14px;flex-wrap:wrap">` + toggles.map(k => {
    const checked = CFG.options[k] ?? def.options[k];
    const candidate = { ...currentDraft(), options: { ...CFG.options, [k]: true } };
    const canEnable = allowedAggs(candidate, CFG.src, CFG.bindings).includes(CFG.agg);
    return `
    <label class="opt-toggle" style="display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--ink-2)">
      <input type="checkbox" data-opt="${k}" ${checked ? 'checked' : ''}
             ${!checked && !canEnable ? 'disabled title="현재 집계에서는 사용할 수 없습니다"' : ''}>${NAMES[k] || k}</label>`;
  }).join('') + '</div>';
}

function parseFilterValue(f, raw) {
  const src = CFG.src, fl = src.fields.find(x => x.name === f.field);
  if (NO_VALUE_OPS.includes(f.op)) return null;
  if (f.op === 'last_n') {
    const n = Math.floor(Number(raw));
    return Number.isFinite(n) && n >= 1 ? n : '';
  }
  // 문자열 매칭 op 는 숫자 강제변환 금지 — '123' 부분일치는 문자열 그대로가 의미
  const textOps = ['contains', 'starts_with', 'ends_with', 'like', 'not_like'];
  const numeric = !textOps.includes(f.op)
    && fl && (fl.role === 'measure' || fl.role === 'sequence' || fl.role === 'ordinal');
  const parseOne = value => numeric && value !== '' && !Number.isNaN(Number(value))
    ? Number(value) : value;
  if (ARRAY_VALUE_OPS.includes(f.op)) {
    if (Array.isArray(raw)) return raw.map(parseOne);
    return raw.split(',').map(s => s.trim()).filter(Boolean).map(parseOne);
  }
  return parseOne(raw);
}

async function runPreview() {
  const problem = draftProblem();
  if (problem) { toast(problem); return false; }
  const box = document.querySelector('.preview-box');
  if (!box) return false;
  resetPreview('gold 데이터를 확인하는 중…');
  const hint = box.querySelector('.hint');
  const button = $('cfg-preview');
  const key = previewFingerprint();
  const gen = ++CFG.previewGen;
  CFG.previewBusy = true;
  button.disabled = true;
  button.textContent = '조회 중…';
  renderCfgTabs();
  try {
    const draft = currentDraft();
    const { b, def } = resolveBindings(draft, CFG.src);
    const { be, promoted } = effectiveBindings(CFG.src, b);
    const { spec, alias } = buildSpec(draft, CFG.src, be);
    const res = await API.query(spec);
    if (!CFG.open || gen !== CFG.previewGen || key !== previewFingerprint()) return false;
    if (!hasRenderableMeasures(draft, spec, res)) {
      CFG.previewKey = null;
      hint.textContent = res.rows.length
        ? '사용 가능한 측정값이 없습니다 — 이 설정은 추가하지 않습니다'
        : '데이터 0행 — 이 설정은 추가하지 않습니다';
      $('cfg-note').textContent = '필터 값·집계 대상·측정값 가용성을 확인하세요';
      return false;
    }
    hint.style.display = 'none';
    const colLabels = Object.fromEntries(res.columns.map(column =>
      [column, column === alias
        ? (draft.agg === 'count' ? '건수' : `${fieldLabel(CFG.src, b.value || b.x)} ${AGG_LABEL[draft.agg] || ''}`.trim())
        : fieldLabel(CFG.src, promoted[column] || column)]));
    const inst = await RENDER.render(box.querySelector('.plot'), {
      el: box, chart: draft, b: be, src: CFG.src, rows: res.rows, cols: res.columns,
      geo: def.geo || null,
      regionRole: be.region ? (CFG.src.fields.find(f => f.name === be.region) || {}).role : null,
      valueLabel: draft.agg === 'count' ? '건수' : `${fieldLabel(CFG.src, b.value || b.x)} ${AGG_LABEL[draft.agg] || ''}`.trim(),
      xLabel: b.x ? fieldLabel(CFG.src, b.x) : '', yLabel: b.y ? fieldLabel(CFG.src, b.y) : '',
      colLabels,
      isCurrent: () => CFG.open && gen === CFG.previewGen && key === previewFingerprint(),
    });
    if (!CFG.open || gen !== CFG.previewGen || key !== previewFingerprint()) {
      if (inst) RENDER.dispose(inst);
      return false;
    }
    CFG.previewKey = key;
    $('cfg-note').textContent = `${res.row_count}행 · ${res.mode}${res.mode === 'live' ? ` · ${res.elapsed_ms}ms` : ''}`;
    return true;
  } catch (err) {
    if (gen !== CFG.previewGen) return false;
    CFG.previewKey = null;
    hint.style.display = ''; hint.textContent = '실패: ' + err.message;
    $('cfg-note').textContent = '오류가 해결되기 전에는 추가되지 않습니다';
    return false;
  } finally {
    if (gen === CFG.previewGen) {
      CFG.previewBusy = false;
      button.disabled = false;
      button.textContent = '미리보기';
      renderCfgTabs();
    }
  }
}
$('cfg-preview').onclick = () => runPreview();

$('cfg-apply').onclick = async () => {
  const button = $('cfg-apply');
  if (button.dataset.busy === '1') return;
  const problem = draftProblem();
  if (problem) { toast(problem); return; }
  if (!S.page) { toast('레이아웃 페이지를 먼저 추가하세요'); return; }
  button.dataset.busy = '1';
  const originalLabel = CFG.mode === 'add' ? '추가' : '적용';
  button.textContent = '확인 중…';
  button.disabled = true;
  if (CFG.previewKey !== previewFingerprint()) {
    const valid = await runPreview();
    if (!valid) {
      toast('미리보기 오류를 확인하세요 — 차트는 추가하지 않았습니다');
      button.dataset.busy = '';
      button.textContent = originalLabel;
      renderCfgTabs();
      return;
    }
  }
  const cfg = currentDraft();
  try {
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
                             bindings: cfg.bindings, bins: cfg.bins, agg: cfg.agg,
                             filters: cfg.filters, filters_logic: cfg.filters_logic,
                             having: cfg.having, options: cfg.options });
      const rec = S.tiles[chart.id];
      if (rec) {
        rec.el.querySelector('.tt b').textContent = chart.title || '차트';
        rec.el.querySelector('.tt span').textContent = chart.source;
        plotRO.unobserve(rec.el.querySelector('.plot'));
        if (rec.inst) { RENDER.dispose(rec.inst); rec.inst = null; }
        loadTile(chart);
      }
      S.dirty = true;
      toast('차트를 수정했습니다 — 레이아웃 저장으로 확정하세요');
    }
    CFG.dirty = false;
    closeCfg();
  } finally {
    button.dataset.busy = '';
    button.textContent = originalLabel;
  }
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
async function boot(authState) {
  S.canEdit = authState?.user?.can_edit_charts === true;
  $('read-only-badge').hidden = S.canEdit;
  ['btn-edit', 'add-page', 'mobile-add-page'].forEach(id => { $(id).hidden = !S.canEdit; });
  $('empty-board-note').textContent = S.canEdit
    ? '오른쪽 위 레이아웃 변경을 누른 뒤 차트 추가로 gold 데이터셋을 연결하세요.'
    : '게스트는 허용된 현재 레이아웃을 조회할 수 있으며 변경 내용은 저장할 수 없습니다.';
  const grid = GridStack.init({
    column: 12, cellHeight: 84, margin: 8, float: false,
    staticGrid: true, animate: true,
  }, '#grid');
  S.grid = grid;
  setSwitchBusy(true);
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
    await switchPage(S.pageId, { force: true });
  };
  $('btn-add-chart').onclick = () => openCfg('add');
  $('add-page').onclick = addLayoutPageFlow;
  $('mobile-add-page').onclick = addLayoutPageFlow;
  $('mobile-page-menu').onclick = event => {
    const index = S.pages.findIndex(page => page.id === S.pageId);
    if (index < 0) return;
    const rect = event.currentTarget.getBoundingClientRect();
    pageCtx(
      { preventDefault() {}, clientX: rect.right, clientY: rect.bottom },
      S.pages[index],
      index,
    );
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
    S.booting = false;
    setSwitchBusy(false);
  } catch (err) {
    $('sync-note').textContent = 'API 연결 실패';
    $('pulse').className = 'pulse bad';
    const empty = $('empty-board');
    empty.hidden = false;
    empty.innerHTML = `<div class="art">!</div><b>Charts Studio를 불러오지 못했습니다</b>
      <p>${esc(err.message)}</p><button type="button" class="btn primary" id="boot-retry">다시 연결</button>`;
    $('boot-retry').onclick = () => location.reload();
    toast('초기화 실패: ' + err.message);
  }
}

/* ── 셀프테스트(?selftest=1) — 실브라우저에서 핵심 플로우를 자동 검증 ── */
async function selftest() {
  const R = [];
  const ok = (name, cond) => R.push(`${cond ? 'PASS' : 'FAIL'} ${name}`);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const waitFor = async (predicate, label, timeoutMs = 8000) => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (predicate()) return;
      await sleep(50);
    }
    throw new Error(`${label} 대기 시간 초과`);
  };
  const cleanup = {
    originalPageId: S.pageId,
    originalOrder: S.pages.map(page => page.id),
    prefix: `__charts_selftest_${uid()}`,
    tempIds: [],
  };
  try {
    ok('pages loaded', S.pages.length >= 3);
    const baseSummary = S.pages.find(page => page.id === 'seed-overview')
      || S.pages.find(page => page.chart_count > 0);
    if (!baseSummary) throw new Error('복제할 기준 레이아웃이 없습니다');
    const basePage = await API.page(baseSummary.id);
    const testPage = await API.createPage(cleanup.prefix);
    cleanup.tempIds.push(testPage.id);
    await API.patchPage(testPage.id, {
      charts: JSON.parse(JSON.stringify(basePage.charts)),
    });
    S.pages = await API.pages();
    ok('page created', S.pages.some(page => page.id === testPage.id));
    await switchPage(testPage.id, { force: true });
    await sleep(4000);
    ok('tiles rendered', Object.keys(S.tiles).length === S.page.charts.length);
    ok('no tile errors', !document.querySelector('.tile-state.err'));

    // 편집 모드 + 프로그램적 이동 → 저장 → 영속 확인
    enterEdit();
    ok('edit mode grid unlocked', !S.grid.opts.staticGrid);
    const firstEl = S.grid.engine.nodes[0].el;
    const movedId = firstEl.dataset.chartId;
    S.grid.update(firstEl, { x: 6, y: 8 });
    ok('move marks dirty', S.dirty);
    await saveLayout();
    const reloaded = await API.page(S.pageId);
    const moved = reloaded.charts.find(c => c.id === movedId);
    ok('moved position persisted', moved && moved.grid.x === 6);

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

    // 임시 레이아웃 안에서 이름·순서 변경. 삭제와 원래 순서 복원은 finally가 책임진다.
    await API.patchPage(testPage.id, { name: `${cleanup.prefix}_renamed` });
    ok('page renamed', (await API.pages()).some(
      page => page.id === testPage.id && page.name === `${cleanup.prefix}_renamed`
    ));
    const ids = (await API.pages()).map(x => x.id);
    const reorderedIds = [testPage.id, ...ids.filter(id => id !== testPage.id)];
    const after = await API.reorderPages(reorderedIds);
    ok('pages reordered', after[0].id === testPage.id);
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

    // 식별 승격 — 이름 바인딩은 쿼리에서 동반 코드로 바뀌고(동명 합산 방지),
    // 코드값은 value_labels 로 한글 표기, 행정동 지도는 MOIS 코드로 폴리곤 매칭
    const eb = effectiveBindings(flowSrc, { axis: 'admin_dong', value: 'cnt' });
    ok('identity promoted to code', eb.be.axis === 'admin_dong_code'
       && eb.promoted.admin_dong_code === 'admin_dong' && eb.be.value === 'cnt');
    ok('code value labels served',
       (S.meta.value_labels.admin_dong_code || {})['1168051000'] === '신사동·강남구'
       && typeDef('map_seoul_dong').slots[0].accepts.includes('geo_dong_code'));
    await GEO.ensure('seoul_dong');
    const dongMatch = GEO.matchRows('seoul_dong', [['1168051000', 3], ['1162068500', 5]], true);
    ok('seoul_dong map matches by MOIS code', dongMatch.matched === 2
       && dongMatch.data.some(d => d.name === '신사동·강남구' && d.value === 3)
       && dongMatch.data.some(d => d.name === '신사동·관악구' && d.value === 5));

    // 자율성 개방 — ① 구간 축(bins): 숫자 측정값이 floor 그룹핑으로 축이 된다
    // ② 저카디널리티(groupable): 실측 distinct 기반으로 measure 가 category 축 후보에 선다
    const lifespanSrc = S.srcDetails['gold_license_lifespan'] || await API.source('gold_license_lifespan');
    const binRes = await API.query({ source: lifespanSrc.name,
      dims: [{ field: 'avg_days', bin_width: 365 }],
      measures: [{ field: null, agg: 'count', alias: 'count' }], limit: 50 });
    ok('binned measure axis groups by interval', binRes.rows.length >= 1
       && /floor\(try_cast/.test(binRes.sql || ''));
    const summarySrc = S.srcDetails['gold_license_dong_summary'] || await API.source('gold_license_dong_summary');
    ok('low-cardinality measure opened as axis',
       summarySrc.fields.some(f => f.groupable && f.role === 'measure'));

    // 필터 확장 — OR 그룹 괄호 봉인 · is_null 원본 컬럼 · contains 이스케이프 · HAVING
    const orRes = await API.query({ source: summarySrc.name, dims: ['gu'],
      measures: [{ field: null, agg: 'count', alias: 'count' }],
      filters: [{ logic: 'or', filters: [
        { field: 'gu', op: 'eq', value: '강남구' }, { field: 'gu', op: 'eq', value: '서초구' }] }],
      limit: 10 });
    ok('or-group renders parenthesized', orRes.rows.length === 2
       && / or /.test(orRes.sql || '') && /\(cast\("gu"/.test(orRes.sql || ''));
    const nullRes = await API.query({ source: summarySrc.name, dims: ['gu'],
      measures: [{ field: null, agg: 'count', alias: 'count' }],
      filters: [{ field: 'admin_dong_code', op: 'not_null' }], limit: 5 });
    ok('is_null family targets raw column', /"admin_dong_code" is not null/.test(nullRes.sql || ''));
    const havRes = await API.query({ source: summarySrc.name, dims: ['gu'],
      measures: [{ field: null, agg: 'count', alias: 'count' }],
      having: [{ agg: 'count', op: 'gte', value: 10 }], limit: 30 });
    ok('having gates aggregated groups', /having cast\(count\(\*\)/.test(havRes.sql || '')
       && havRes.rows.every(r => Number(r[1]) >= 10));

    // 성격 급한 이용자 — 검색 입력이 첫 글자 뒤 DOM 교체로 포커스를 잃지 않아야 한다.
    openCfg('add');
    CFG.domain = 'all'; CFG.step = 'source'; renderCfg();
    const search = $('src-q');
    search.focus(); search.value = 'weather';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    ok('persona fast: source search keeps focus',
       document.activeElement === search && [...document.querySelectorAll('.src-item')].some(el => !el.hidden));
    CFG.dirty = false; closeCfg();

    // 화가 많은 이용자 — 추천 카드를 hover한 채 클릭해도 body 툴팁이 남지 않아야 한다.
    openCfg('add');
    CFG.source = flowSrc.name; CFG.src = flowSrc; CFG.step = 'type'; renderCfg();
    const recoCard = document.querySelector('.type-card.reco');
    recoCard.dispatchEvent(new Event('pointerenter'));
    const tipWasVisible = $('reco-tip') && $('reco-tip').classList.contains('on');
    recoCard.click();
    ok('persona angry: recommendation tooltip closes on transition',
       tipWasVisible && !$('reco-tip').classList.contains('on') && CFG.step === 'bind');
    CFG.dirty = false; closeCfg();

    // 귀찮은 이용자 — 도메인→소스 클릭→실데이터 availability→도표 카드→
    // 자동 바인딩→미리보기 렌더까지 실제 드로어 경로를 한 번에 통과해야 한다.
    const domainCases = [
      ['culture', 'gold_culture_activity_by_dong', 'line',
       { axis: 'event_date', value: 'activities_count' }, 'sum'],
      ['traffic', 'gold_traffic_incident_current_by_admin_dong_hourly', 'bar',
       { axis: 'admin_dong', value: 'incident_count' }, 'sum'],
      ['weather', 'gold_weather_daily_by_admin_dong', 'line',
       { axis: 'forecast_date', value: 'temp_avg_c' }, 'avg'],
      ['citydata', 'gold_citydata_ppltn_by_time', 'line',
       { axis: 'event_at', value: 'avg_ppltn' }, 'avg'],
      ['transit', 'gold_transit_dong_hourly', 'table',
       { axis: 'admin_dong_code', value: 'subway_arrival_cnt' }, 'sum'],
    ];
    for (const [domain, sourceName, type, bindings, agg] of domainCases) {
      openCfg('add');
      CFG.domain = 'all'; CFG.step = 'source'; renderCfg();
      const domainChip = [...document.querySelectorAll('.src-doms .chip')]
        .find(element => element.dataset.d === domain);
      if (!domainChip) throw new Error(`${domain} 도메인 선택지가 없습니다`);
      domainChip.click();
      const sourceButton = [...document.querySelectorAll('.src-item')]
        .find(element => element.dataset.s === sourceName && !element.hidden);
      if (!sourceButton) throw new Error(`${sourceName} 소스 선택지가 없습니다`);
      sourceButton.click();
      await waitFor(
        () => CFG.source === sourceName && !!CFG.src && CFG.step === 'type',
        `${domain} availability`,
      );
      const typeCard = [...document.querySelectorAll('.type-card')]
        .find(element => element.dataset.t === type);
      if (!typeCard) throw new Error(`${domain} ${type} 도표 선택지가 없습니다`);
      typeCard.click();
      const autoBound = Object.entries(bindings).every(
        ([slot, field]) => CFG.bindings[slot] === field
      ) && CFG.agg === agg;
      const previewed = await runPreview();
      const plot = document.querySelector('.preview-box .plot');
      const rendered = type === 'table'
        ? !!plot.querySelector('table')
        : !!echarts.getInstanceByDom(plot);
      ok(`persona lazy/domain ${domain}: one-flow preview`,
         autoBound && previewed && rendered && availabilityFresh(CFG.src));
      CFG.dirty = false;
      closeCfg();
    }

    const transitSrc = S.srcDetails.gold_transit_dong_hourly;
    const distinct = resolveBindings({
      type: 'heatmap', source: transitSrc.name,
      bindings: { x: 'hour_at', y: 'hour_at', value: 'bus_obs_cnt' }, agg: 'sum',
    }, transitSrc);
    ok('ontology prevents duplicate heatmap axes',
       distinct.b.x !== distinct.b.y && distinct.b.y === 'admin_dong_code');
    const overlapSrc = {
      label: '겹치는 역할 테스트',
      fields: [
        { name: 'category', role: 'category', chartable: true },
        { name: 'sequence', role: 'sequence', chartable: true },
        { name: 'value', role: 'measure', chartable: true, allowed_aggs: ['sum'] },
      ],
    };
    const overlap = resolveBindings({ type: 'heatmap', bindings: {}, agg: 'sum' }, overlapSrc);
    ok('ontology backtracks overlapping slot roles',
       overlap.b.x === 'sequence' && overlap.b.y === 'category' && overlap.b.value === 'value');

    const sportsSrc = await API.source('gold_culture_sports_schedule');
    const countChart = { type: 'race', source: sportsSrc.name,
      bindings: { time: 'game_date', axis: 'stadium' }, agg: 'count', options: {} };
    ok('count-only gold is chartable without fake value field',
       sportsSrc.supports.includes('race') && !resolveBindings(countChart, sportsSrc).b.value);

    // 레이스 컨트롤과 결측 보존은 외부 데이터와 무관한 결정론적 DOM 테스트.
    const raceHost = document.createElement('div');
    raceHost.className = 'preview-box';
    raceHost.style.cssText = 'position:fixed;left:-2000px;top:0;width:520px;height:280px';
    raceHost.innerHTML = '<div class="plot"></div>';
    document.body.appendChild(raceHost);
    const raceChart = { type: 'race', source: flowSrc.name,
      bindings: { time: 'ym', axis: 'event_type', value: 'cnt' }, agg: 'sum',
      options: { cumulative: false, top_n: 5, interval_ms: 200 } };
    const raceInst = await RENDER.render(raceHost.querySelector('.plot'), {
      el: raceHost, chart: raceChart, b: raceChart.bindings, src: flowSrc,
      rows: [['2025-01', 'opened', 2], ['2025-01', 'closed', 1],
             ['2025-02', 'opened', 3], ['2025-02', 'closed', 4],
             ['2025-03', 'opened', 5]], cols: [], colLabels: {},
    });
    const raceControls = raceHost.querySelector('.race-controls');
    if (RENDER.raceSnapshot(raceInst).playing) raceControls.querySelector('.race-toggle').click();
    const beforeFrame = RENDER.raceSnapshot(raceInst).index;
    raceControls.querySelector('.race-next').click();
    const speedControl = raceControls.querySelector('.race-speed');
    speedControl.value = '2'; speedControl.dispatchEvent(new Event('change'));
    const raceState = RENDER.raceSnapshot(raceInst);
    ok('race has play, frame and speed controls',
       !!raceControls && !raceState.playing && raceState.speed === 2
       && raceState.index === (beforeFrame + 1) % raceState.frameCount);
    const cumulativeModel = RENDER.buildRaceModel({
      chart: { ...raceChart, options: { ...raceChart.options, cumulative: true } },
      b: raceChart.bindings, src: flowSrc,
      rows: [['2025-01', 'opened', 2], ['2025-02', 'opened', 3]],
    });
    ok('race cumulative frames start from zero and accumulate',
       cumulativeModel.frames[0].rows[0][1] === 2
       && cumulativeModel.frames[1].rows[0][1] === 5);
    const defaultRaceModel = RENDER.buildRaceModel({
      chart: { ...raceChart, options: {} },
      b: raceChart.bindings, src: flowSrc,
      rows: [['2025-01', 'opened', 2], ['2025-02', 'opened', 3]],
    });
    ok('race without an option stays non-cumulative',
       defaultRaceModel.cumulative === false
       && defaultRaceModel.frames[1].rows[0][1] === 3);
    RENDER.dispose(raceInst);
    ok('race dispose clears controls and timer', !raceHost.querySelector('.race-controls'));
    raceHost.remove();

    const nullHost = document.createElement('div');
    nullHost.style.cssText = 'position:fixed;left:-2000px;top:0;width:400px;height:220px';
    document.body.appendChild(nullHost);
    const nullChart = { type: 'line', source: flowSrc.name,
      bindings: { axis: 'ym', value: 'cnt' }, agg: 'sum', options: {} };
    const nullInst = await RENDER.render(nullHost, {
      el: nullHost, chart: nullChart, b: nullChart.bindings, src: flowSrc,
      rows: [['2025-01', null], ['2025-02', 4]], cols: [], colLabels: {},
    });
    ok('missing values stay null instead of zero', nullInst.getOption().series[0].data[0] == null);
    RENDER.dispose(nullInst); nullHost.remove();
  } catch (err) {
    R.push('FAIL exception: ' + err.message);
  } finally {
    try {
      CFG.dirty = false;
      if (CFG.open) closeCfg();
      if (S.edit) exitEdit();
      const beforeCleanup = await API.pages();
      const original = beforeCleanup.find(page => page.id === cleanup.originalPageId);
      if (original) await switchPage(original.id, { force: true });
      const disposable = beforeCleanup.filter(page =>
        cleanup.tempIds.includes(page.id) || page.name.startsWith(cleanup.prefix));
      for (const page of disposable) {
        try { await API.deletePage(page.id); } catch { /* 다음 목록 검사에서 실패를 드러낸다 */ }
      }
      let remaining = await API.pages();
      const remainingIds = new Set(remaining.map(page => page.id));
      const restored = cleanup.originalOrder.filter(id => remainingIds.has(id));
      const extras = remaining.map(page => page.id).filter(id => !restored.includes(id));
      if (restored.length + extras.length === remaining.length) {
        remaining = await API.reorderPages([...restored, ...extras]);
      }
      S.pages = remaining;
      renderSidebar();
      ok('page deleted', !remaining.some(page =>
        cleanup.tempIds.includes(page.id) || page.name.startsWith(cleanup.prefix)));
    } catch (cleanupError) {
      R.push('FAIL cleanup: ' + cleanupError.message);
    }
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
  if (q.get('selftest') === '1') {
    if (S.canEdit) selftest();
    else document.title = 'SELFTEST_READ_ONLY';
  }
  if (q.get('uidemo') && S.canEdit) { // 개발/검증용: 편집모드·드로어를 열어둔 상태로 진입
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
