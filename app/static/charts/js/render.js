/* 도표 렌더러 — 온톨로지 슬롯(축/값/시리즈/지역)만 보고 그린다. 특정 컬럼명 결합 없음.
 * 팔레트: dataviz 검증 통과 고정 순서 8슬롯(9번째부터는 '기타'로 접힘) */
'use strict';

const RENDER = (() => {
  const PALETTE = ['#2a78d6', '#008300', '#e87ba4', '#eda100', '#1baf7a', '#eb6834', '#4a3aa7', '#e34948'];
  const SEQ = ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#2a78d6', '#1c5cab', '#0d366b'];
  const INK = { p: '#171c26', s: '#4c5566', m: '#8a93a5', grid: '#edf0f5', axis: '#e2e6ed' };
  const FONT = '"Pretendard Variable", Pretendard, "Segoe UI", sans-serif';
  const MAX_SERIES = 8;

  let META = { value_labels: {} };
  const setMeta = m => { META = m; };

  const vlabel = (field, v) => {
    const m = META.value_labels && META.value_labels[field];
    return (m && m[String(v)]) || String(v ?? '—');
  };
  /* 필드 granularity 기반 축 라벨 — month_of_year('03') → '3월' */
  const axisLabelFn = (ctx, field) => {
    const f = ctx.src && ctx.src.fields.find(x => x.name === field);
    if (f && f.granularity === 'month_of_year') return v => `${Number(v)}월`;
    return v => vlabel(field, v);
  };
  const fmt = v => {
    if (v == null || Number.isNaN(v)) return '—';
    const n = Number(v);
    if (Math.abs(n) >= 1e8) return (n / 1e8).toLocaleString('ko-KR', { maximumFractionDigits: 1 }) + '억';
    if (Math.abs(n) >= 1e4) return (n / 1e4).toLocaleString('ko-KR', { maximumFractionDigits: 1 }) + '만';
    if (Math.abs(n) > 0 && Math.abs(n) < 1) return n.toLocaleString('ko-KR', { maximumFractionDigits: 3 });
    return n.toLocaleString('ko-KR', { maximumFractionDigits: 1 });
  };
  const PCT_RE = /(rate|ratio|share|survival|pct|비율|율)/i;
  function pctLike(fieldName, values) {
    if (!PCT_RE.test(fieldName || '')) return false;
    const max = Math.max(...values.map(v => Math.abs(Number(v) || 0)));
    return max <= 1.5;
  }
  const fmtPct = v => (Number(v) * 100).toLocaleString('ko-KR', { maximumFractionDigits: 1 }) + '%';

  const tooltipBase = {
    backgroundColor: '#fff', borderColor: '#e2e6ed', borderWidth: 1,
    textStyle: { color: INK.p, fontSize: 11.5, fontFamily: FONT },
    extraCssText: 'box-shadow:0 6px 20px rgba(18,26,44,.12);border-radius:8px;padding:8px 11px;',
  };
  const base = () => ({
    textStyle: { fontFamily: FONT, color: INK.s },
    color: PALETTE,
    tooltip: { ...tooltipBase },
    animationDuration: 240,
  });
  const catAxis = (labels, rotate) => ({
    type: 'category', data: labels,
    axisLine: { lineStyle: { color: INK.axis } }, axisTick: { show: false },
    axisLabel: { color: INK.m, fontSize: 10.5, rotate: rotate || 0, hideOverlap: true },
  });
  const valAxis = usePct => ({
    type: 'value',
    splitLine: { lineStyle: { color: INK.grid } },
    axisLabel: { color: INK.m, fontSize: 10.5, formatter: v => usePct ? fmtPct(v) : fmt(v) },
  });
  const gridBox = { left: 8, right: 14, top: 30, bottom: 4, containLabel: true };
  const legendBase = names => ({
    show: names.length > 1, top: 0, right: 0, itemWidth: 9, itemHeight: 9,
    icon: 'roundRect', textStyle: { color: INK.s, fontSize: 10.5 }, type: 'scroll',
  });

  /* 숫자형(월/연차)이나 코드형 순서 라벨('0_lt1y')은 값 정렬보다 자연 순서가 우선 */
  function smartOrder(keys) {
    if (keys.every(k => /^\d+(\.\d+)?$/.test(k))) return [...keys].sort((a, b) => Number(a) - Number(b));
    if (keys.every(k => /^\d+_/.test(k))) return [...keys].sort();
    return null;
  }

  /* rows [[axis, (series), value]] → {labels, seriesNames, matrix} — 상위 topN.
     초과 시리즈는 합산 가능한 집계(sum/count)에서만 '기타'로 접고, avg 같은 비가산
     집계에서는 접으면 '평균의 합'이라는 무의미한 값이 되므로 초과분을 버리고 개수만 알린다. */
  function pivot(rows, hasSeries, topN, axisSort, foldable = true) {
    const axisTotal = new Map(), serTotal = new Map();
    rows.forEach(r => {
      const a = String(r[0] ?? '—'), s = hasSeries ? String(r[1] ?? '—') : '_';
      const v = Number(r[hasSeries ? 2 : 1]) || 0;
      axisTotal.set(a, (axisTotal.get(a) || 0) + v);
      serTotal.set(s, (serTotal.get(s) || 0) + v);
    });
    let labels = [...axisTotal.keys()];
    const natural = smartOrder(labels);
    if (axisSort === 'value' && !natural) labels.sort((a, b) => axisTotal.get(b) - axisTotal.get(a));
    else if (natural) labels = natural;
    else labels.sort();
    if (topN && labels.length > topN) {
      const top = new Set([...labels].sort((a, b) => axisTotal.get(b) - axisTotal.get(a)).slice(0, topN));
      labels = labels.filter(l => top.has(l));   // 상위 topN 만 남기되 현재 정렬 순서 유지
    }

    let seriesNames = smartOrder([...serTotal.keys()])
      || [...serTotal.keys()].sort((a, b) => serTotal.get(b) - serTotal.get(a));
    const over = Math.max(0, seriesNames.length - MAX_SERIES);
    let folded = false, droppedSeries = 0;
    if (over > 0) {
      if (foldable) { seriesNames = [...seriesNames.slice(0, MAX_SERIES - 1), '기타']; folded = true; }
      else { droppedSeries = over; seriesNames = seriesNames.slice(0, MAX_SERIES); }
    }

    /* 결측 셀은 0 이 아니라 null — 우측검열(코호트 미관측 연차) 구간을 0% 로 왜곡하지 않는다 */
    const li = new Map(labels.map((l, i) => [l, i]));
    const matrix = new Map(seriesNames.map(s => [s, new Array(labels.length).fill(null)]));
    rows.forEach(r => {
      const a = String(r[0] ?? '—');
      if (!li.has(a)) return;
      let s = hasSeries ? String(r[1] ?? '—') : '_';
      if (!matrix.has(s)) { if (!folded) return; s = '기타'; }
      const arr = matrix.get(s), i = li.get(a);
      arr[i] = (arr[i] ?? 0) + (Number(r[hasSeries ? 2 : 1]) || 0);
    });
    return { labels, seriesNames, matrix, folded, droppedSeries };
  }

  /* ── 타입별 렌더 — ctx = {el, chart, b(슬롯→필드), rows, cols, src, geo} ── */

  /* 의미가 고정된 코드값은 값→색을 고정한다 — 정렬/필터가 바뀌어도 색-의미 대응 유지 */
  const VALUE_COLORS = { opened: '#2a78d6', closed: '#eb6834' };
  const seriesColor = raw => raw === '기타' ? '#b3bac7' : VALUE_COLORS[raw];

  function rBarLine(ctx, kind) {
    const { chart, b } = ctx;
    const o = chart.options || {};
    const hasSeries = !!b.series;
    const isTime = kind === 'line';
    const topN = isTime ? 0 : (o.top_n || 20);
    const foldable = ['sum', 'count', 'count_distinct'].includes(chart.agg || 'sum');
    const pv = pivot(ctx.rows, hasSeries, topN, isTime ? 'alpha' : 'value', foldable);
    if (pv.droppedSeries) ctx.note = `시리즈 상위 ${MAX_SERIES}개만 표시 (+${pv.droppedSeries})`;
    const axisLabels = pv.labels.map(axisLabelFn(ctx, b.axis));
    const serNames = pv.seriesNames.map(s => s === '_' ? (ctx.valueLabel || '값') : vlabel(b.series, s));
    const usePct = pctLike(b.value, [...pv.matrix.values()].flat());
    const horizontal = kind === 'bar' && o.horizontal;

    const series = [...pv.matrix.entries()].map(([name, data], i) => kind === 'bar' ? {
      name: serNames[i], type: 'bar', data,
      stack: o.stacked && hasSeries ? 'st' : undefined,
      barMaxWidth: 26,
      itemStyle: {
        color: seriesColor(name),
        borderRadius: o.stacked && hasSeries ? 2 : (horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0]),
        borderColor: '#fff', borderWidth: o.stacked && hasSeries ? 1 : 0,
      },
    } : {
      name: serNames[i], type: 'line', data,
      smooth: !!o.smooth, symbol: 'circle', symbolSize: 7,
      showSymbol: data.length <= 40,
      lineStyle: { width: 2, color: seriesColor(name) },
      itemStyle: { color: seriesColor(name) },
      areaStyle: o.area ? { opacity: 0.12 } : undefined,
      emphasis: { focus: 'series' },
    });

    return {
      ...base(),
      tooltip: { ...tooltipBase, trigger: 'axis',
        axisPointer: kind === 'line' ? { type: 'line', lineStyle: { color: '#b3bac7', type: 'dashed' } } : { type: 'shadow' },
        valueFormatter: v => usePct ? fmtPct(v) : fmt(v) },
      legend: legendBase(serNames),
      grid: gridBox,
      xAxis: horizontal ? valAxis(usePct)
        : catAxis(axisLabels, kind === 'bar' && axisLabels.length > 12 ? 32 : 0),
      yAxis: horizontal ? catAxis(axisLabels) : valAxis(usePct),
      series,
    };
  }

  function rPie(ctx) {
    const { chart, b } = ctx;
    const o = chart.options || {};
    const topN = Math.min(o.top_n || 8, MAX_SERIES);
    const sorted = [...ctx.rows].map(r => ({ name: vlabel(b.axis, r[0]), value: Number(r[1]) || 0 }))
      .sort((a, x) => x.value - a.value);
    const head = sorted.slice(0, topN);
    const rest = sorted.slice(topN).reduce((a, r) => a + r.value, 0);
    if (rest > 0) head.push({ name: '기타', value: rest, itemStyle: { color: '#b3bac7' } });
    const total = head.reduce((a, r) => a + r.value, 0) || 1;
    return {
      ...base(),
      tooltip: { ...tooltipBase, trigger: 'item',
        formatter: p => `${escapeHtml(p.name)}<br><b>${fmt(p.value)}</b> (${(p.value / total * 100).toFixed(1)}%)` },
      legend: { ...legendBase(head.map(h => h.name)), type: 'plain', show: head.length > 1, orient: 'vertical', right: 0, top: 'middle' },
      series: [{
        type: 'pie',
        radius: o.donut === false ? [0, '78%'] : ['46%', '78%'],
        center: ['40%', '50%'],
        itemStyle: { borderColor: '#fff', borderWidth: 2, borderRadius: 3 },
        label: { show: true, fontSize: 10.5, color: INK.s,
                 formatter: p => p.percent >= 5 ? `${p.name} ${p.percent.toFixed(0)}%` : '' },
        labelLine: { show: true, length: 8, length2: 6 },
        data: head,
      }],
    };
  }

  function rScatter(ctx) {
    const { b } = ctx;
    const xs = ctx.rows.map(r => Number(r[1]) || 0), ys = ctx.rows.map(r => Number(r[2]) || 0);
    const xPct = pctLike(b.x, xs), yPct = pctLike(b.y, ys);
    return {
      ...base(),
      tooltip: { ...tooltipBase, trigger: 'item',
        formatter: p => `${escapeHtml(vlabel(b.axis, p.data[2]))}<br>${escapeHtml(ctx.xLabel)}: <b>${xPct ? fmtPct(p.data[0]) : fmt(p.data[0])}</b><br>${escapeHtml(ctx.yLabel)}: <b>${yPct ? fmtPct(p.data[1]) : fmt(p.data[1])}</b>` },
      grid: gridBox,
      xAxis: { ...valAxis(xPct), name: ctx.xLabel, nameTextStyle: { color: INK.m, fontSize: 10 } },
      yAxis: { ...valAxis(yPct), name: ctx.yLabel, nameTextStyle: { color: INK.m, fontSize: 10 } },
      series: [{
        type: 'scatter', symbolSize: 9,
        itemStyle: { color: PALETTE[0], opacity: 0.7, borderColor: '#fff', borderWidth: 1 },
        data: ctx.rows.map(r => [Number(r[1]) || 0, Number(r[2]) || 0, r[0]]),
      }],
    };
  }

  function rHeatmap(ctx) {
    const { chart, b } = ctx;
    const topN = (chart.options || {}).top_n || 30;
    const xt = new Map(), yt = new Map();
    ctx.rows.forEach(r => {
      const v = Number(r[2]) || 0;
      xt.set(String(r[0]), (xt.get(String(r[0])) || 0) + v);
      yt.set(String(r[1]), (yt.get(String(r[1])) || 0) + v);
    });
    const xs = smartOrder([...xt.keys()]) || [...xt.keys()].sort();
    const ys = [...yt.entries()].sort((a, x) => x[1] - a[1]).slice(0, topN).map(e => e[0]).reverse();
    const xi = new Map(xs.map((v, i) => [v, i])), yi = new Map(ys.map((v, i) => [v, i]));
    const data = [];
    let vmax = 0;
    ctx.rows.forEach(r => {
      const x = xi.get(String(r[0])), y = yi.get(String(r[1]));
      if (x == null || y == null) return;
      const v = Number(r[2]) || 0;
      vmax = Math.max(vmax, v);
      data.push([x, y, v]);
    });
    return {
      ...base(),
      tooltip: { ...tooltipBase,
        formatter: p => `${escapeHtml(axisLabelFn(ctx, b.y)(ys[p.data[1]]))} × ${escapeHtml(axisLabelFn(ctx, b.x)(xs[p.data[0]]))}<br><b>${fmt(p.data[2])}</b>` },
      grid: { ...gridBox, top: 26, right: 8 },
      xAxis: catAxis(xs.map(axisLabelFn(ctx, b.x)), xs.length > 14 ? 40 : 0),
      yAxis: { ...catAxis(ys.map(axisLabelFn(ctx, b.y))), type: 'category' },
      visualMap: {
        min: 0, max: vmax || 1, calculable: false,
        orient: 'horizontal', right: 0, top: 0, itemWidth: 9, itemHeight: 60,
        inRange: { color: SEQ }, textStyle: { color: INK.m, fontSize: 9.5 },
        formatter: fmt,
      },
      series: [{ type: 'heatmap', data, itemStyle: { borderColor: '#fff', borderWidth: 1 },
                 emphasis: { itemStyle: { borderColor: INK.p, borderWidth: 1 } } }],
    };
  }

  function rMap(ctx) {
    const geoId = ctx.geo;
    const byCode = /_code$/.test(ctx.regionRole || '');
    const m = GEO.matchRows(geoId, ctx.rows, byCode);
    const values = m.data.map(d => d.value);
    const vmax = Math.max(...values, 1);
    ctx.coverage = `${m.matched}/${m.total} 매칭` + (m.ambiguous ? ` · 동명 모호 ${m.ambiguous} 제외` : '');
    return {
      ...base(),
      tooltip: { ...tooltipBase, trigger: 'item',
        formatter: p => `${escapeHtml(p.name)}<br><b>${p.value != null && !Number.isNaN(p.value) ? fmt(p.value) : '데이터 없음'}</b>` },
      visualMap: {
        min: 0, max: vmax, calculable: true,
        orient: 'horizontal', left: 0, bottom: 0, itemWidth: 9, itemHeight: 70,
        inRange: { color: SEQ }, textStyle: { color: INK.m, fontSize: 9.5 }, formatter: fmt,
      },
      series: [{
        type: 'map', map: geoId, nameProperty: '__display', roam: true,
        layoutCenter: ['50%', '50%'], layoutSize: '96%',
        selectedMode: false, data: m.data,
        itemStyle: { areaColor: '#f4f6f9', borderColor: '#fff', borderWidth: 0.8 },
        label: { show: false },
        emphasis: { label: { show: true, fontSize: 10, color: INK.p }, itemStyle: { areaColor: '#fdf3d5' } },
      }],
    };
  }

  function rMapPoints(ctx) {
    const rows = ctx.rows.filter(r => r[0] != null && r[1] != null);
    const vmax = Math.max(...rows.map(r => Number(r[2]) || 0), 1);
    return {
      ...base(),
      geo: {
        map: ctx.geo, nameProperty: '__display', roam: true,
        layoutCenter: ['50%', '50%'], layoutSize: '96%',
        itemStyle: { areaColor: '#f4f6f9', borderColor: '#dfe4ec', borderWidth: 0.8 },
        emphasis: { disabled: true }, silent: true,
      },
      tooltip: { ...tooltipBase, trigger: 'item', formatter: p => `<b>${fmt(p.data[2])}</b>` },
      visualMap: {
        min: 0, max: vmax, calculable: true,
        orient: 'horizontal', left: 0, bottom: 0, itemWidth: 9, itemHeight: 70,
        inRange: { color: ['#9ec5f4', '#2a78d6', '#0d366b'] }, textStyle: { color: INK.m, fontSize: 9.5 }, formatter: fmt,
      },
      series: [{
        type: 'scatter', coordinateSystem: 'geo',
        symbolSize: v => Math.max(3, Math.min(14, 3 + 11 * Math.sqrt((Number(v[2]) || 0) / vmax))),
        itemStyle: { opacity: 0.65 },
        data: rows.map(r => [Number(r[1]), Number(r[0]), Number(r[2]) || 0]),
      }],
    };
  }

  /* ── 타임랩스 경주 — 시간 프레임을 재생하며 순위 변화를 애니메이션으로 ── */
  function buildRaceModel(ctx) {
    const { chart, b } = ctx;
    const o = chart.options || {};
    const cumulative = o.cumulative !== false;
    const topN = o.top_n || 12;
    const interval = Math.max(200, Math.min(5000, o.interval_ms || 800));
    const tLabel = axisLabelFn(ctx, b.time);
    const byTime = new Map();
    const axes = new Set();
    ctx.rows.forEach(r => {
      const t = String(r[0] ?? ''), a = String(r[1] ?? '—');
      if (!byTime.has(t)) byTime.set(t, new Map());
      const cur = byTime.get(t);
      cur.set(a, (cur.get(a) || 0) + (Number(r[2]) || 0));
      axes.add(a);
    });
    const times = smartOrder([...byTime.keys()]) || [...byTime.keys()].sort();
    const running = new Map([...axes].map(a => [a, 0]));
    const frames = times.map(t => {
      const cur = byTime.get(t);
      if (cumulative) cur.forEach((v, a) => running.set(a, running.get(a) + v));
      const src = cumulative ? running : new Map([...axes].map(a => [a, cur.get(a) || 0]));
      return { label: tLabel(t), rows: [...src.entries()].map(([a, v]) => [vlabel(b.axis, a), v]) };
    });
    return { frames, topN, interval, cumulative };
  }

  const raceGraphic = (text, playing) => ({
    elements: [
      { type: 'text', right: 18, bottom: 14, silent: true,
        style: { text, font: '750 26px "Pretendard Variable", Pretendard, sans-serif', fill: '#dfe4ec' } },
      { type: 'text', right: 18, bottom: 48, silent: true,
        style: { text: playing ? '' : '⏸ 일시정지 — 클릭으로 재생', font: '600 11px Pretendard, sans-serif', fill: '#8a93a5' } },
    ],
  });

  function rRace(ctx) {
    const model = buildRaceModel(ctx);
    ctx.__race = model;
    ctx.note = `${model.frames.length}프레임 · ${model.cumulative ? '누적' : '구간'} · 클릭=일시정지`;
    return {
      ...base(),
      tooltip: { ...tooltipBase, trigger: 'item',
        formatter: p => `${escapeHtml(p.value[0])}<br><b>${fmt(p.value[1])}</b>` },
      grid: { left: 8, right: 56, top: 8, bottom: 4, containLabel: true },
      xAxis: { type: 'value', max: 'dataMax',
               splitLine: { lineStyle: { color: INK.grid } },
               axisLabel: { color: INK.m, fontSize: 10.5, formatter: fmt } },
      yAxis: { type: 'category', inverse: true, max: model.topN - 1,
               axisLine: { show: false }, axisTick: { show: false },
               axisLabel: { color: INK.s, fontSize: 10.5 },
               animationDuration: 200, animationDurationUpdate: 200 },
      dataset: { source: model.frames.length ? model.frames[0].rows : [] },
      series: [{
        type: 'bar', encode: { x: 1, y: 0 }, realtimeSort: true,
        barMaxWidth: 22, itemStyle: { color: PALETTE[0], borderRadius: [0, 4, 4, 0] },
        label: { show: true, position: 'right', valueAnimation: true,
                 color: INK.s, fontSize: 10, formatter: p => fmt(p.value[1]) },
      }],
      graphic: raceGraphic(model.frames.length ? model.frames[0].label : '', true),
      animationDuration: 0,
      animationDurationUpdate: model.interval,
      animationEasing: 'linear', animationEasingUpdate: 'linear',
    };
  }

  /* 프레임 재생 루프 — dispose 자가 감지로 정리, 클릭으로 일시정지/재생, 끝나면 잠시 쉬고 반복 */
  function startRace(inst, model) {
    stopRace(inst);
    let i = 1, playing = true, holdUntil = 0;
    inst.getZr().on('click', () => {
      playing = !playing;
      const cur = model.frames[Math.max(0, i - 1)];
      inst.setOption({ graphic: raceGraphic(cur ? cur.label : '', playing) });
    });
    inst.__raceTimer = setInterval(() => {
      if (!inst.getZr || (inst.isDisposed && inst.isDisposed())) { clearInterval(inst.__raceTimer); return; }
      if (!playing || Date.now() < holdUntil) return;
      const f = model.frames[i];
      if (!f) { i = 0; holdUntil = Date.now() + 1500; return; }   // 한 바퀴 끝 — 쉬었다 처음부터
      inst.setOption({ dataset: { source: f.rows }, graphic: raceGraphic(f.label, playing) });
      i++;
    }, model.interval);
  }
  function stopRace(inst) {
    if (inst.__raceTimer) { clearInterval(inst.__raceTimer); inst.__raceTimer = null; }
    if (inst.getZr && !((inst.isDisposed && inst.isDisposed()))) inst.getZr().off('click');
  }

  function rStat(el, ctx) {
    const v = ctx.rows.length ? Number(ctx.rows[0][0]) : null;
    const usePct = pctLike(ctx.b.value, [v || 0]);
    el.innerHTML = `<div class="stat-wrap">
      <div class="v num">${v == null ? '—' : (usePct ? fmtPct(v) : Number(v).toLocaleString('ko-KR', { maximumFractionDigits: 1 }))}</div>
      <div class="s">${escapeHtml(ctx.valueLabel || '')} · ${escapeHtml(ctx.src.label)}</div>
    </div>`;
  }

  function rTable(el, ctx) {
    const cols = ctx.cols;
    const rows = ctx.rows;
    const numeric = cols.map((_, i) => rows.every(r => r[i] == null || !Number.isNaN(Number(r[i]))));
    el.innerHTML = `<div class="tbl-wrap"><table>
      <tr>${cols.map(c => `<th>${escapeHtml(ctx.colLabels[c] || c)}</th>`).join('')}</tr>
      ${rows.map(r => `<tr>${r.map((v, i) => numeric[i] && typeof v === 'number'
        ? `<td class="num-cell num">${fmt(v)}</td>`
        : `<td>${escapeHtml(vlabel(cols[i], v))}</td>`).join('')}</tr>`).join('')}
    </table></div>`;
  }

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  /* 진입점 — chart.type 에 따라 DOM 렌더 또는 ECharts 옵션 생성.
     타입 전환(스탯↔ECharts) 잔상이 남지 않게 반대 계열 잔여물을 정리한다. */
  async function render(plotEl, ctx) {
    const t = ctx.chart.type;
    if (t === 'stat' || t === 'table') {
      const old = echarts.getInstanceByDom(plotEl);
      if (old) old.dispose();
      if (t === 'stat') rStat(plotEl, ctx); else rTable(plotEl, ctx);
      return null;
    }
    if (!echarts.getInstanceByDom(plotEl)) plotEl.innerHTML = '';
    if (ctx.geo) await GEO.ensure(ctx.geo);

    let option;
    if (t === 'bar') option = rBarLine(ctx, 'bar');
    else if (t === 'line' || t === 'area') option = rBarLine(ctx, 'line');
    else if (t === 'pie') option = rPie(ctx);
    else if (t === 'scatter') option = rScatter(ctx);
    else if (t === 'heatmap') option = rHeatmap(ctx);
    else if (t === 'race') option = rRace(ctx);
    else if (t === 'map_points') option = rMapPoints(ctx);
    else if (t.startsWith('map_')) option = rMap(ctx);
    else throw new Error(`렌더러 없음: ${t}`);

    let inst = echarts.getInstanceByDom(plotEl);
    if (!inst) inst = echarts.init(plotEl, null, { renderer: 'canvas' });
    stopRace(inst);   // 같은 인스턴스가 경주→다른 타입으로 바뀌어도 재생 루프가 남지 않게
    inst.setOption(option, true);
    if (t === 'race' && ctx.__race) startRace(inst, ctx.__race);
    return inst;
  }

  return { render, setMeta, fmt, PALETTE };
})();
