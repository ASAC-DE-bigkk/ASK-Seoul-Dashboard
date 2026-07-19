/* 자동 추천 엔진 — 온톨로지 role 과 필드 이름 패턴만으로 도표·조합을 제안한다.
 *
 * 원칙: 특정 테이블/컬럼명을 하드코딩하지 않는다. 입력은 (소스의 필드+role, 슬롯 계약,
 * 값 라벨 사전)뿐이므로 새 gold 테이블이 실리거나 컬럼 형태가 바뀌어도 그 시점의
 * 데이터 모양에 맞는 추천이 즉석에서 다시 계산된다.
 *
 *   types(src)        → { chartType: {score(1~3), reason} }   score≥2 가 "추천"
 *   combos(src, type) → [ {label, bindings, agg, options} ]   연결 단계의 원클릭 조합
 */
'use strict';

const RECO = (() => {
  let META = { value_labels: {}, chart_types: {} };
  const setMeta = m => { META = m; };

  /* 필드 이름 패턴 — 건수형(합계가 자연스러움) vs 비율형(평균이 자연스러움) */
  const COUNT_RE = /(cnt|count|_n$|^n_|total|active|open|business|stock|survivor)/i;
  const RATIO_RE = /(rate|ratio|share|survival|lq|pct|비율|율)/i;

  const byRole = (src, ...roles) => src.fields
    .filter(f => f.chartable !== false && roles.includes(f.role))
    .sort((a, b) => (a.recommendation_priority ?? 40) - (b.recommendation_priority ?? 40));
  /* 값 라벨 사전에 있는 필드 = 가짓수가 적다고 확정된 분류축 (개업/폐업, 대분류 등) */
  const knownSmall = f => !!(META.value_labels && META.value_labels[f.name]);
  const q = f => `‘${f.label}’`;

  function pickValues(src) {
    const ms = byRole(src, 'measure');
    return {
      counts: ms.filter(f => COUNT_RE.test(f.name)),
      ratios: ms.filter(f => RATIO_RE.test(f.name)),
      all: ms,
    };
  }
  const ROW_COUNT = { name: null, label: '행 수', preferred_agg: 'count', additive: true, virtual: true };
  const bestValue = src => pickValues(src).all[0] || ROW_COUNT;
  const aggFor = f => (f && f.preferred_agg) || ((f && RATIO_RE.test(f.name)) ? 'avg' : 'sum');
  const withValue = (bindings, f) => f && f.name ? { ...bindings, value: f.name } : bindings;

  /* 추세축: 순환축(month_of_year)은 제외하고 월>일>연 순으로 선호 */
  function trendTime(src) {
    const pref = { month: 0, date: 1, datetime: 2, year: 3 };
    return byRole(src, 'time')
      .filter(f => f.granularity !== 'month_of_year')
      .sort((a, b) =>
        ((a.recommendation_priority ?? 40) - (b.recommendation_priority ?? 40))
        || ((pref[a.granularity] ?? 9) - (pref[b.granularity] ?? 9)))[0];
  }
  const cyclicMonth = src => byRole(src, 'time').find(f => f.granularity === 'month_of_year');

  /* ── 1단계: 도표 타입 추천 (컬럼 형태 → 보편적 도표) ────────── */
  function types(src) {
    const out = {};
    const put = (t, score, reason) => { if (src.supports.includes(t)) out[t] = { score, reason }; };
    const vals = pickValues(src);
    const v = bestValue(src);
    const cats = byRole(src, 'category');
    const t = trendTime(src);
    const seq = byRole(src, 'sequence')[0];
    const mo = cyclicMonth(src);
    const gu = byRole(src, 'geo_gu', 'geo_gu_code')[0];
    const dong = byRole(src, 'geo_dong', 'geo_dong_code')[0];
    const legal = byRole(src, 'geo_legal_code', 'geo_legal_dong')[0];
    const sido = byRole(src, 'geo_sido')[0];
    const country = byRole(src, 'geo_country')[0];
    const lat = byRole(src, 'geo_lat')[0], lng = byRole(src, 'geo_lng')[0];
    const axisAny = cats.length || t || seq || gu || dong;

    if (!axisAny)
      put('stat', 3, `축이 될 만한 분류가 없는 소스예요 — ${q(v)} 하나를 큼직하게 보여주는 게 제일 깔끔해요.`);
    else
      put('stat', 1, `${q(v)}만 뚝 떼어 헤드라인 숫자로 써도 좋아요.`);

    if (cats.length)
      put('bar', 3, `${q(cats[0])}처럼 이름 붙은 축은 막대로 세워 비교하는 게 제일 정직해요.`);
    else if (gu || dong)
      put('bar', 2, `지역(${q(gu || dong)})별 크기 비교라면 막대도 잘 어울려요.`);

    if (t)
      put('line', 3, `${q(t)}가 흐르는 소스네요 — 추세는 선으로 이어야 눈이 따라가요.`);
    else if (seq)
      put('line', 3, `${q(seq)} 순서대로 점을 이으면 곡선이 이야기를 들려줘요 — 생존곡선처럼요.`);

    const raceAxis = gu || dong || cats.find(f => !knownSmall(f)) || cats[0];
    if ((t || seq) && raceAxis)
      put('race', 3, `${q(t || seq)}가 흐르고 ${q(raceAxis)}가 겨루는 소스 — 순위가 엎치락뒤치락하는 걸 영상처럼 볼 수 있어요.`);

    const smallCat = cats.find(knownSmall);
    if (smallCat && (v.additive || v.virtual))
      put('pie', 2, `${q(smallCat)}는 가짓수가 몇 안 되죠 — 구성비는 도넛 한 바퀴면 충분해요.`);

    if (vals.all.length >= 2)
      put('scatter', 2, `숫자 필드가 ${vals.all.length}개나 있어요 — ${q(vals.all[0])}와 ${q(vals.all[1])}를 맞대 보면 숨은 관계가 보일지도요.`);

    if (mo && cats.length)
      put('heatmap', 3, `${q(mo)}×${q(cats[0])} — 계절이 만드는 무늬는 히트맵이 제일 잘 그려요.`);
    else if (cats.length >= 2)
      put('heatmap', 2, `${q(cats[0])}×${q(cats[1])} 두 축이 있으니 밀도를 색으로 깔아볼 만해요.`);

    if (gu)
      put('map_seoul', 3, `${q(gu)}가 있네요 — 서울 지도에 칠하면 어디가 뜨거운지 한눈에 보여요.`);
    if (dong)
      put('map_seoul_dong', 3, `동 이름(${q(dong)})까지 있으니 동네 단위로 칠해보죠 — 골목 상권이 드러나요.`);
    if (legal)
      put('map_seoul_legal', 3, `법정동(${q(legal)})이 있어요 — 경계 그대로 칠하는 정밀 지도가 됩니다.`);
    if (sido)
      put('map_korea', 3, `${q(sido)}가 있으니 전국 지도에 올려보죠 — 지역 격차가 바로 보여요.`);
    if (country)
      put('map_world', 3, `${q(country)}가 있네요 — 세계 지도에 펼치면 스케일이 달라져요.`);
    if (lat && lng)
      put('map_points', 3, `위경도 좌표가 있으니 점을 뿌려보세요 — 밀집한 곳이 곧 핫스팟이에요.`);

    put('table', 1, '숫자보다 목록으로 훑고 싶은 날엔 — 정직한 표만 한 게 없죠.');
    return out;
  }

  /* ── 2단계: 소스×도표 → 원클릭 바인딩 조합 ─────────────────── */
  function combos(src, type) {
    const vals = pickValues(src);
    const v = bestValue(src);
    const r = vals.ratios[0];
    const cats = byRole(src, 'category');
    const cat0 = cats[0];
    const smallCat = cats.find(knownSmall);
    const t = trendTime(src);
    const seq = byRole(src, 'sequence')[0];
    const mo = cyclicMonth(src);
    const gu = byRole(src, 'geo_gu')[0] || byRole(src, 'geo_gu_code')[0];
    const dong = byRole(src, 'geo_dong', 'geo_dong_code')[0];
    const list = [];
    const mk = (label, bindings, agg, options) => {
      if (Object.values(bindings).some(x => !x)) return;   // 필요한 필드가 없으면 조합 자체를 제안하지 않는다
      list.push({ label, bindings, agg, options: options || {} });
    };
    const AGG_KO = { sum: '합계', avg: '평균', min: '최소', max: '최대', count: '건수' };
    const sl = f => f.label.length > 14 ? f.label.slice(0, 13) + '…' : f.label;   // 칩 라벨용 축약
    const vLabel = (f, agg) => `${sl(f)} ${AGG_KO[agg] || agg}`;

    if (type === 'bar') {
      if (cat0) mk(`${sl(cat0)}별 ${vLabel(v, aggFor(v))}`, withValue({ axis: cat0.name }, v), aggFor(v), { top_n: 15 });
      if (gu) mk(`자치구별 ${vLabel(v, aggFor(v))} TOP 15`, withValue({ axis: gu.name }, v), aggFor(v), { top_n: 15 });
      if (cat0 && r) mk(`${sl(cat0)}별 ${vLabel(r, 'avg')}`, { axis: cat0.name, value: r.name }, 'avg', { top_n: 15 });
      if (cat0 && smallCat && smallCat !== cat0 && v.additive)
        mk(`${sl(cat0)}×${sl(smallCat)} 누적`, { axis: cat0.name, value: v.name, series: smallCat.name }, 'sum', { stacked: true });
    } else if (type === 'line') {
      const axis = t || seq;
      if (axis) mk(`${sl(axis)} 추이 — ${vLabel(v, aggFor(v))}`, withValue({ axis: axis.name }, v), aggFor(v));
      if (axis && smallCat) mk(`${sl(smallCat)}별로 갈라 본 추이`, withValue({ axis: axis.name, series: smallCat.name }, v), aggFor(v));
      if (axis && r) mk(`${sl(axis)} 흐름 — ${vLabel(r, 'avg')}`, { axis: axis.name, value: r.name }, 'avg');
    } else if (type === 'race') {
      const axis = gu || dong || cats.find(c => !knownSmall(c)) || cat0;
      const t0 = t || seq;
      if (t0 && axis) {
        mk(`${sl(axis)} 구간별 경주 (비누적)`,
           withValue({ time: t0.name, axis: axis.name }, v), aggFor(v), { cumulative: false, top_n: 12 });
        if (v.cumulative_safe) mk(`${sl(axis)} 누적 경주 — ${vLabel(v, aggFor(v))}`,
           withValue({ time: t0.name, axis: axis.name }, v), aggFor(v), { cumulative: true, top_n: 12 });
      }
    } else if (type === 'pie') {
      const c = smallCat || cat0;
      if (c && (v.additive || v.virtual)) mk(`${sl(c)} 구성비`, withValue({ axis: c.name }, v), aggFor(v), { donut: true });
      if (gu && (v.additive || v.virtual)) mk(`자치구 구성비 TOP 8`, withValue({ axis: gu.name }, v), aggFor(v), { donut: true, top_n: 8 });
    } else if (type === 'scatter') {
      const a = vals.counts[0] || vals.all[0];
      const b = vals.ratios.find(x => x !== a) || vals.all.find(x => x !== a);
      const unit = gu || dong || cat0;
      if (a && b && unit) mk(`${sl(a)} vs ${sl(b)} — ${sl(unit)} 단위`,
        { x: a.name, y: b.name, axis: unit.name },
        a.additive === false || b.additive === false ? 'avg' : 'sum');
    } else if (type === 'heatmap') {
      const x = mo || (cats[1] && cat0) || t;
      const y = (x === cat0 ? cats[1] : cat0) || dong || gu;
      if (x && y) mk(`${sl(x)} × ${sl(y)} 밀도`, withValue({ x: x.name, y: y.name }, v), aggFor(v), { top_n: 20 });
    } else if (type === 'table') {
      const axis = dong || gu || cat0;
      if (axis) mk(`${sl(axis)} 상위 50`, withValue({ axis: axis.name }, v), aggFor(v), { top_n: 50 });
    } else if (type === 'stat') {
      mk(`${vLabel(v, aggFor(v))}`, withValue({}, v), aggFor(v));
      if (r) mk(`${vLabel(r, 'avg')}`, { value: r.name }, 'avg');
    } else if (type.startsWith('map_')) {
      const def = META.chart_types && META.chart_types[type];
      const regionSlot = def && def.slots.find(s => s.name === 'region');
      if (type === 'map_points') {
        const lat = byRole(src, 'geo_lat')[0], lng = byRole(src, 'geo_lng')[0];
        if (lat && lng) mk(`${vLabel(v, aggFor(v))} 밀도`, withValue({ lat: lat.name, lng: lng.name }, v), aggFor(v));
      } else if (regionSlot) {
        const region = src.fields.find(f => regionSlot.accepts.includes(f.role));
        if (region) mk(`${sl(region)}별 ${vLabel(v, aggFor(v))}`, withValue({ region: region.name }, v), aggFor(v));
        if (region && r) mk(`${sl(region)}별 ${vLabel(r, 'avg')}`, { region: region.name, value: r.name }, 'avg');
      }
    }
    return list.slice(0, 3);
  }

  return { setMeta, types, combos };
})();
