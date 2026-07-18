/* 지도 자산 로딩 + 지역 매칭 — 이름/코드 어느 쪽으로도 폴리곤에 붙는다.
 *
 * 코드 체계 주의: gold 데이터의 지역 코드는 전부 MOIS(행안부) 체계다
 * (gu_code 5자리, legal_code 10자리). 반면 seoul_gu/seoul_dong 자산의 code 는
 * KOSTAT(통계청) 체계라 그대로 붙이면 '다른 구'에 칠해진다 — 그래서 코드 매칭은
 * MOIS 사전(MOIS_GU, EMD_CD)이 있는 지도에서만 허용하고, KOSTAT 코드는 쓰지 않는다.
 * 동 단위 이름은 구를 넘어 중복될 수 있어(신사동 등) 등록 시 유일한 표시 이름
 * ("신사동·강남구")으로 재작성하고, 모호한 이름 행은 매칭 제외로 계수한다(이중 칠 방지). */
'use strict';

const GEO = (() => {
  /* MOIS(행안부) 서울 자치구 코드 — gold 데이터(gu_code·legal_code 접두)와 같은 체계 */
  const MOIS_GU = {
    '11110': '종로구', '11140': '중구', '11170': '용산구', '11200': '성동구', '11215': '광진구',
    '11230': '동대문구', '11260': '중랑구', '11290': '성북구', '11305': '강북구', '11320': '도봉구',
    '11350': '노원구', '11380': '은평구', '11410': '서대문구', '11440': '마포구', '11470': '양천구',
    '11500': '강서구', '11530': '구로구', '11545': '금천구', '11560': '영등포구', '11590': '동작구',
    '11620': '관악구', '11650': '서초구', '11680': '강남구', '11710': '송파구', '11740': '강동구',
  };
  const GU_TO_MOIS = Object.fromEntries(Object.entries(MOIS_GU).map(([c, n]) => [n, c]));

  const CONF = {
    /* moisCode: 코드 매칭 방식 — 'byName'(이름→MOIS 역매핑) | 'emd'(속성이 곧 MOIS) | null(코드 매칭 불가) */
    seoul_gu:         { file: 'seoul_gu.json',         nameProp: 'name',       codeLen: 5, moisCode: 'byName' },
    seoul_dong:       { file: 'seoul_dong.json',       nameProp: 'name',       codeLen: 0, moisCode: null, guSuffix: true },
    seoul_legal_dong: { file: 'seoul_legal_dong.json', nameProp: 'EMD_KOR_NM', codeLen: 8, moisCode: 'emd', codeProp: 'EMD_CD', guSuffix: true },
    korea_sido:       { file: 'korea_sido.json',       nameProp: 'name',       codeLen: 0, moisCode: null },
    world:            { file: 'world.json',            nameProp: 'name',       codeLen: 0, moisCode: null },
  };
  const loaded = {};   // id → {codeToName, baseToNames, conf}

  async function fetchJson(file) {
    const res = await fetch('/static/charts/geo/' + file);
    if (!res.ok) throw new Error(`지도 자산 로딩 실패: ${file} (${res.status})`);
    return res.json();
  }

  async function ensure(id) {
    if (loaded[id]) return loaded[id];
    const conf = CONF[id];
    if (!conf) throw new Error(`알 수 없는 지도: ${id}`);
    const gj = await fetchJson(conf.file);

    const baseCount = {};
    gj.features.forEach(f => {
      const base = String(f.properties[conf.nameProp] || '');
      baseCount[base] = (baseCount[base] || 0) + 1;
    });
    const codeToName = {}, baseToNames = {};
    gj.features.forEach(f => {
      const p = f.properties;
      const base = String(p[conf.nameProp] || '');
      let display = base;
      if (conf.guSuffix && baseCount[base] > 1) {
        // 법정동: EMD_CD 앞 5자리가 MOIS 구코드 → 구명 접미사로 유일화
        const gu = conf.moisCode === 'emd' ? MOIS_GU[String(p[conf.codeProp] || '').slice(0, 5)] : '';
        display = gu ? `${base}·${gu}` : base;
      }
      p.__display = display;
      if (conf.moisCode === 'emd' && p[conf.codeProp] != null) {
        codeToName[String(p[conf.codeProp])] = display;
      } else if (conf.moisCode === 'byName' && GU_TO_MOIS[base]) {
        codeToName[GU_TO_MOIS[base]] = display;
      }
      (baseToNames[base] = baseToNames[base] || []).push(display);
    });

    echarts.registerMap(id, gj);
    loaded[id] = { codeToName, baseToNames, conf };
    return loaded[id];
  }

  /* rows: [[region, value], …] → {data, matched, ambiguous, total}
     byCode: 코드 role 바인딩. 이름 매칭에서 동명이 여럿이면 이중 칠 대신 '모호'로 제외한다. */
  function matchRows(id, rows, byCode) {
    const g = loaded[id];
    const agg = {};
    let matched = 0, ambiguous = 0;
    rows.forEach(([region, value]) => {
      const key = String(region ?? '');
      if (!key || key === 'UNK') return;
      let name = null;
      if (byCode) {
        if (g.conf.codeLen) name = g.codeToName[key.slice(0, g.conf.codeLen)];
      } else {
        const cands = g.baseToNames[key];
        if (cands && cands.length === 1) name = cands[0];
        else if (cands && cands.length > 1) { ambiguous++; return; }
      }
      if (!name) return;
      matched++;
      agg[name] = (agg[name] || 0) + (Number(value) || 0);
    });
    return {
      data: Object.entries(agg).map(([name, value]) => ({ name, value })),
      matched, ambiguous, total: rows.length,
    };
  }

  return { CONF, ensure, matchRows };
})();
