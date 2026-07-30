# 03 — 온톨로지 사용법 총람 (가치 등급 0~3)

Charts Studio 온톨로지를 **어떻게 쓸 수 있는가**의 전수 목록이다. 30명의 IT 전문가 페르소나
(데이터/분석/ML/LLM·MCP/RAG/온톨로지스트/GIS/보안/레드팀/프라이버시/DB성능/SRE/API/프론트/
접근성/i18n/QA/PM/UX/6개 도메인 전문가/공공정책/FinOps/거버넌스)가 각자 렌즈로 검토한 결과를
합쳐 **하나도 버리지 않고** 등급을 매겼다.

## 등급 기준
| 등급 | 의미 |
|---|---|
| **3** | 전략적·필수. 이 온톨로지가 아니면 못 하거나, 이걸 안 하면 손해가 큰 것 |
| **2** | 분명한 실무 가치. 방법까지 적어둘 만한 것 |
| **1** | 상황에 따라 유용. 조건부·부분적 가치 |
| **0** | 거의 가치 없음(오해·안티패턴 포함). **버리지 않고** 왜 낮은지 기록 |

## 읽는 법
- 3등급은 곧바로 로드맵 후보다. 다수 페르소나가 독립적으로 지목한 것부터 배치했다.
- 0등급은 "하지 말 것" 목록으로 쓴다 — 특히 **소스 간 조인·원시 행 추출·OWL 추론**은 이 시스템의
  설계(단일 relation 시맨틱 레이어)와 정면 충돌하므로 시도 자체가 낭비다.
- 각 패턴의 전제 조건·결함은 [02-gaps-and-remediation.md](02-gaps-and-remediation.md) 와
  [07-opus-handoff-guideline.md](07-opus-handoff-guideline.md) 의 미해결 항목을 함께 본다.

## 대표 축 5개 (총람 이전에 이것만 봐도 된다)
1. **MCP 도구 표면** — `tool_schemas()` = MCP inputSchema, `call_tool()` = 실행. 어떤 호스트든 붙는다.
2. **그라운딩 프리앰블** — `ontology_manifest()` 한 번으로 역할 어휘·geo part-of·도표 계약을 접지.
3. **RAG 게이트웨이** — `value_labels`(19,663 코드↔한글) = gazetteer, manifest = 검색 코퍼스.
4. **백엔드 중립 지표** — 같은 스펙이 Trino·D1(SQLite)·RDB 에서 동일 결과(05 실증).
5. **집계 안전 계약** — 비가산 sum 차단·코드 식별·읽기전용이 LLM 환각을 구조적으로 막는다.

---

## 전수 목록 (111건)

### 3등급 — 전략적·필수  (51건)

| # | 패턴 | 메모 |
|---|---|---|
| 1 | **MCP server / Anthropic tool-use surface (run_query + 3-verb dispatch, injection-hard read-only)** | The core use; tool_schemas() is already MCP-inputSchema-shaped. |
| 2 | **ontology_manifest as a cached system-prefix / compact T-Box grounding doc (~1.6-4.9K tokens)** | Grounds the planner over roles/concepts without paging 112 sources; must be dispatchable and cached. |
| 3 | **Split read surface into MCP resources vs a single run_query action tool** | MCP-native shape: hosts cache grounding, spend tool-call budget on actions. |
| 4 | **Guardrailed NL→metric/chart-spec self-serve connector (no raw-SQL escape hatch)** | Additivity + whitelist + code-strict identity structurally block summed-rate and homonym-merge bugs. |
| 5 | **Portable write-once-run-anywhere metric definition (backend-neutral spec, D1==Trino parity)** | Define a metric once, run on dev SQLite/D1 and prod Trino identically; the system's one uniquely-proven property. |
| 6 | **NAMED_METRICS as a certified/governed metric registry & business glossary (single source of truth)** | Under-populated (4 commerce entries, 2 statistically wrong) but the right abstraction; needs weighting + validation + domain coverage. |
| 7 | **Auto-generated metric catalog / data dictionary / RoPA / privacy data-map from describe_source** | Machine-readable inventory of role/additivity/aggs/geo/stats across all 112 sources; value jumps once desc/lineage/quality are surfaced. |
| 8 | **Additivity + allowed_aggs as a machine-checked aggregation-safety contract (SHACL-like shapes)** | Enforced in _measure_expr, not just advisory; needs the 3-valued/dimension-aware upgrade to be trustworthy. |
| 9 | **Retrieval-first source/field routing over an offline index of manifest+describe_source** | No retrieval exists in the loop today; an index turns enumerate-only into top-k recall and cuts per-turn tokens. |
| 10 | **value_labels as a gazetteer for Korean label→code entity linking on filters** | 19,663 code→한글 pairs; the highest-leverage grounding asset — needs a reverse resolver and per-source keying. |
| 11 | **Governed NL cohort/group-comparison & hypothesis triage** | Fan-out grouped aggregates across domains — valid only if the agent also requests the denominator/base_n. |
| 12 | **Homonym-safe code-keyed grouping with Korean labels (labeled_rows + MOIS disambiguation)** | 신사동·강남구 vs 신사동·관악구 stay distinct; the single most common Seoul geo correctness trap solved — but only on the frontend today. |
| 13 | **Conformed-dimension federation across 6 domains via N independent rolled-up queries (NOT cross-source SQL joins)** | The Kimball-correct substitute for the joins the verdict rejected; unlocked by executable geo/time rollup. |
| 14 | **Homonym-safe choropleths at gu / 행정동 / 법정동 (map_seoul family)** | Group by code, render Korean; the strongest geospatial capability. |
| 15 | **Same-grain cross-domain spatial comparison on pre-conformed gold (incidents vs weather vs 유동인구)** | gold already spatially conformed the domains at dong/gu — no join engine needed. |
| 16 | **Manifest-as-corpus semantic source router (embed offline, route before the loop)** | Cuts tool-selection error across 6 domains and 112 sources. |
| 17 | **Offline spatiotemporal feature-store materialization (gu×month / dong×date panels)** | run_query as a governed read-only feature API keyed on code composite keys; the top ML use, not yet built. |
| 18 | **Freshness + data-contract gate before any number is served** | Snapshot carries refresh.mode/domain_generated_at/contract_enforced that agent_tools drops entirely — zero freshness signal today. |
| 19 | **Gold↔serving reconciliation / cross-backend regression harness (one spec, two backends)** | Freeze golden specs as data-diff CI; any divergence is real gold-rebuild or export drift. |
| 20 | **Ontology as authz policy-as-data → auto-generated negative/deny-test suite** | describe_source contracts drive assertions that every disallowed agg/op fails — AppSec owns the deny side. |
| 21 | **Tool-arg fuzz / differential fuzzing / property-based invariant harness** | Finite typed input space + 6-dialect render diffing proves no arg escapes to raw/unescaped SQL and the boundary stays closed. |
| 22 | **Disclosure/re-identification & k-anonymity small-cell audit via run_query** | Sweep fine-geo sources to enumerate cells <K; the querybuilder already produces the re-id hotspot report today. |
| 23 | **Pre-execution query-cost / cardinality admission controller from materialized stats** | distinct_count/min/max/row_count are the cost inputs — currently wired into nothing. |
| 24 | **Required-time-filter / partition-pruning advisor from date_range + granularity** | The cheapest scan-reduction lever on partitioned Trino gold, handed to you for free. |
| 25 | **Token-cost control plane over the large stable payloads (caching, per-tool accounting)** | tool_schemas/manifest/describe_source are byte-stable cache candidates; the dominant Opus spend today is uncached re-sends. |
| 26 | **Contract-drift gate: golden-snapshot tool_schemas+manifest with semver + hash** | The whole surface is auto-derived from a mutable snapshot; a diff without a version bump is a breaking-change alarm. |
| 27 | **Compile describe_source into per-source client-side JSON Schemas (validation shift-left)** | Turns trial-and-error spec_error round-trips into local validation before any call. |
| 28 | **Cross-backend differential + metamorphic / conservation-law oracle** | Upgrades 'builds valid SQL' into 'numbers are correct and dialect-consistent' with zero golden values. |
| 29 | **Golden SQL snapshot suite guarding the byte-identical cache-key invariant** | A 1-byte drift cold-starts the entire cache; the only current guard is substring asserts. |
| 30 | **Contract-driven encoding picker + LLM chart validator (chart_contracts + field role/accepts)** | One slot contract drives both the human field-picker and the LLM so invalid encodings are structurally impossible. |
| 31 | **Null/additivity-safe aggregation guard for authored charts** | Kills summed ratios and averaged indices on a stacked bar, enforced server-side even under injection. |
| 32 | **Deterministic alt-text / long-description generator from the slot contract** | Template a WCAG 1.1.1 description from role/label/additivity — no pixels, no hallucination, all 968 combos free. |
| 33 | **Table-first / screen-reader mode reusing labeled_rows as the accessible equivalent** | Same spec through the rTable path gives blind users identical, homonym-safe data. |
| 34 | **labeled_rows / __label as the code→한글 localization delivery layer** | Identify by code, present in Korean, in one round-trip — the target i18n contract. |
| 35 | **SKOS + value_labels as an authorized Korean term base / translation memory** | Disambiguated code→prefLabel(ko) as one governed vocabulary across map/chart/chatbot/MCP. |
| 36 | **Friction telemetry: tool-call transcript as a live usability lab** | Stable error codes + elapsed let you compute turns-to-first-success and a spec_error taxonomy with zero recruiting. |
| 37 | **Intent→spec selection-accuracy benchmark (right source/measure/filter chosen)** | Coverage A/B proved SQL valid, not that the RIGHT source is picked — the biggest un-measured risk. |
| 38 | **In-product governed NL analytics assistant ('ask ASK-Seoul') for non-technical staff** | Governed, cited-SQL, Korean-labeled self-serve replacing BI tickets. |
| 39 | **Answer→saved dashboard tile (governed write path)** | The actual product value object; all safety/binding primitives already exist, only persistence is missing. |
| 40 | **Commerce: 개폐업 flow monitoring (flow tables + last_n + event_type labels)** | cnt is cumulative_safe on the 4 flow tables; the domain's primary dynamic view, correctly supported. |
| 41 | **Commerce: 자치구 특화업종 LQ ranking with non-additive guard + active_cnt floor** | sum(lq) blocked; filter small samples via WHERE active_cnt>=100 to prevent LQ blowup. |
| 42 | **Commerce: cohort survival curve via raw survivors/cohort_n (not the avg path)** | Statistically valid only through the raw weighted path, not NAMED_METRICS avg(rate). |
| 43 | **Culture: citizen/organizer concierge (요즘 핫한 공연 / 이번 주 일정) over MCP** | boxoffice/schedule filtered + code labels give grounded Korean answers. |
| 44 | **Culture: event-scheduling via calendar_density (concentration/total_events)** | Answers both '볼 게 많은 날' and '피해야 할 경쟁 집중일' in one run_query. |
| 45 | **Culture: spatial demand + baseline-crowd maps (activity_by_dong + event_crowd)** | choropleth of events with a 평시 혼잡 baseline overlay. |
| 46 | **Traffic: live incident point map with recency filter (map_points)** | occurred_at last_n drops the stale record; the operational 'what's happening now' board. |
| 47 | **Traffic: weekday × hour-of-day congestion clock heatmap** | The one genuinely hourly, DOW-resolved traffic surface; the canonical ops artifact. |
| 48 | **Weather×living-population response surface (pre-joined x-tables, rain/heat sensitivity)** | The join is already materialized in gold — the flagship weather+citydata question buildable today. |
| 49 | **Transit: 행정동 traffic-intensity choropleth + park-and-ride cross-mode correlation** | The single source fusing subway+bus+parking at one dong×hour grain — unique for transfer/P&R pressure. |
| 50 | **Transit: rush-hour load cube (hour-of-day × gu) with weighted wait** | Turns 44 opaque timestamps × 431 dong codes into a 24×25 named-gu cube; needs geo/time rollup + weighted_avg. |
| 51 | **Gov: official Indicator Dictionary (units, provenance, disclosure floor, methodology)** | The KOSIS/지표누리-style artifact that makes a figure a publishable statistic, not just a chart. |

### 2등급 — 분명한 실무 가치  (33건)

| # | 패턴 | 메모 |
|---|---|---|
| 1 | **list_metrics/run_metric as the stable public API tier / blessed pre-costed fast path** | Decouples named KPI from physical columns; only valuable once CI-validated against the live registry. |
| 2 | **Categorical entity embeddings + code<->name fuzzy resolver** | Maps messy Korean/external names to canonical MOIS codes for deterministic joins. |
| 3 | **Backend-neutral author-once, serve-at-edge (Trino authored, D1/SQLite published)** | Same governed spec serves live and published numbers with no second hand-written SQL. |
| 4 | **MOIS↔KOSTAT crosswalk / 자치구·행정동 gazetteer as a reusable SKOS asset** | Locked in geo.js/scripts today; surfacing it makes Charts Studio an authoritative Seoul admin gazetteer. |
| 5 | **Point-density lat/lng grids (poor-man's geohash) via bin_width → map_points** | Works today but cannot bbox-clip before pulling (coord range ops missing). |
| 6 | **Text-to-spec structured-output training/eval corpus (NL→validated spec→7 dialect SQL)** | Constrained JSON grammar + spec_error signals make an ideal NL2SQL-spec fine-tune/eval set. |
| 7 | **Lineage-aware pipeline impact analysis over MCP** | snapshot lineage exists for only 33/112 (commerce+culture); silently no-ops for traffic/weather/citydata/transit. |
| 8 | **Physical-relation + datasource egress allowlist for SIEM/detection** | Compile the ontology's relation set into an exfil tripwire on rendered SQL. |
| 9 | **Masking-integrity verification via value_labels + companion pairs (UNK→미상)** | Proves grouping is by code and masked dong resolves correctly. |
| 10 | **Reproducible query provenance for audit evidence (rendered SQL + spec)** | Immutable evidence of exactly what an aggregate touched — needs principal/purpose/timestamp logging added. |
| 11 | **Heavy-query observability / cache-efficiency fingerprinting via run_query mode+elapsed_ms** | Free workload telemetry already emitted; nobody consumes it for cost. |
| 12 | **Rollup / pre-aggregation recommender via GEO_PARENT × row_count** | Flag dong-grain facts that should be materialized to gu-grain and route '구별 합계' there. |
| 13 | **Auto bin-width from min/max/distinct to bound histogram cardinality** | Derive width to hit ~30-50 buckets instead of an LLM-supplied 0.001 that yields millions. |
| 14 | **Referential-integrity guard for NAMED_METRICS / CURATED bindings** | Hardcoded literal names drift independently from the auto-derived registry. |
| 15 | **Semantic-contract regression CI gate (additivity/allowed_aggs drift on rename)** | A column rename can silently flip a measure's additivity; snapshot describe_source and diff per PR. |
| 16 | **Deterministic replayable analytics with provenance (mode/sql + D1==Trino)** | Key a host result cache on SQL hash, surface staleness, replay offline for CI. |
| 17 | **ontology_export (SKOS/JSON-LD) as a versioned interchange contract → DCAT / data.go.kr / DataHub glossary** | Honest interop hook; today thin/inert (placeholder namespace, no dct:license, JSON-LD emits ~zero real triples). |
| 18 | **Map drill-down / roll-up interactions from rollup_columns + geo_part_of** | Click a gu polygon to re-run at dong; the interaction graph is handed to the renderer. |
| 19 | **KPI stat-card registry with units from NAMED_METRICS** | unit drives % vs count vs index formatting and the correct empty-state (0 vs —). |
| 20 | **LLM-judge / automated a11y lint of AI answers grounded on run_query truth** | Verify stated numbers/extremes, color-only references, and trend verbs on flat/truncated data. |
| 21 | **describe_source id_field/label_field/concept as a localization routing map** | Tells a localization agent which columns are already Korean vs need __label enrichment. |
| 22 | **Mental-model validation / card sort of role vocabulary + chart-slot IA** | Test whether role names and slot labels match analysts' mental models. |
| 23 | **Comprehension/trust testing of result rendering (homonym labels, ratio-vs-count, truncation)** | Measure misread rates on the three hazards this system creates. |
| 24 | **Auto-generated starter dashboards from the chart-slot ontology** | supports + default_chart + compatible_bindings seed a working per-source page — onboarding/activation with near-zero logic. |
| 25 | **Catalog / feature-gap roadmap analytics on the manifest itself** | Audit which domains have which charts/KPIs/descriptions to prioritize roadmap. |
| 26 | **SKOS/JSON-LD crosswalk → DataHub/OpenMetadata/Collibra glossary + hierarchy sync** | Pipe role concepts into glossary terms and geo part-of into parent/child links. |
| 27 | **Commerce: 업종 수명 / early-close distribution (lifespan p50/early_close_ratio)** | '어떤 업종이 빨리 죽나' at a glance; avg is unweighted across cohort sizes. |
| 28 | **Commerce: address/phone succession (자리 대물림) 교체압 analysis** | A rare, high-signal 'what replaces what on a closed site' asset. |
| 29 | **Commerce: seasonality heatmap + stock_age structure (상권 노후도)** | month×category open-count heatmap and age-band composition. |
| 30 | **Culture: booking-curve funnel + movie 서울 쏠림 trend** | days_to_peak by genre; seoul vs nation top-movie divergence; rank family blocked until a rank role is added. |
| 31 | **Traffic: dong/gu incident choropleth + clearance-lead SLA watchlist + incident×flow impact** | code-identity keeps dongs split; clearance field is mis-additive with a ~4yr outlier; incident_x_flow is the one populated cross table. |
| 32 | **Weather: forecast-reliability / QA agent (completeness + issue-cycle coverage)** | 'is this forecast trustworthy for this dong/cycle' — snapshot currently degenerate (coverage distinct=1). |
| 33 | **Gov: reproducible open-data republishing extract (D1 offline + rendered SQL)** | Ship a frozen dataset + the exact SQL so a citizen can re-run any published figure. |

### 1등급 — 상황부 유용  (15건)

| # | 패턴 | 메모 |
|---|---|---|
| 1 | **Reuse tool_schemas() directly as MCP inputSchema (zero glue)** | Capped because schemas under-describe having and filter-groups; strict hosts reject valid capability. |
| 2 | **RAG grounding over field label/role/concept/description** | Capped until full desc is emitted (only a 28-char label ships) — little for embeddings to grip on opaque names. |
| 3 | **Guided distribution/histogram tiles via the {field,bin_width} dim form** | floor(x/w)*w buckets without hand-written CASE; needs origin/offset and cardinality bounds. |
| 4 | **Descriptive KPI stat tiles (count/sum on genuinely additive measures)** | Shallow but reliable headline counts; not analysis. |
| 5 | **Rollup/drill affordance discovery for an LLM navigator** | Advisory and over-advertises unreachable hops until wired executable. |
| 6 | **Cheap column-profile / drift feature vectors from manifest stats** | distinct_count/min/max/date_range as a profile vector; capped by absent null_count and no mean/stddev/quantile. |
| 7 | **Auto-inferred additivity/role as a gold-model design linter in CI** | Regex-noisy but cheap naming-convention guardrail catching drift at model-write time. |
| 8 | **Read-only / secret-hygiene audit checklist keyed by describe_source.backend** | Metadata drives the review of per-backend read-only enforcement and dsn_env indirection. |
| 9 | **NAMED_METRICS as a showback / cost-per-refresh KPI catalog** | Attach measured tokens + Trino elapsed for chargeback; value is indirect. |
| 10 | **LLM-assisted NL→spec regression corpus grounded on the manifest** | Breadth generator; lower certainty than the metamorphic oracle due to expected-value drift. |
| 11 | **list_sources as a capability-discovery endpoint** | A listing, not a stable typed contract; omits fields/date_range so discovery is thin. |
| 12 | **Sonification / audio-graph data feed from an ordered time/sequence series** | Held low: no interval/step or axis min/max surfaced for scaling; non-additive needs per-point mapping. |
| 13 | **manifest domain/chart/slot labels as a ko UI string catalog** | Usable message set but ko-only, no message IDs or plural/context metadata. |
| 14 | **Cold-start / discoverability study over 112 sources via list_sources** | Thin surface (no default_chart/examples) limits what there is to study. |
| 15 | **Attach sensitivity/retention SKOS concepts for GRC / catalog consumption** | Extend the concept scheme with public/quasi_id/sensitive + retention_class. |

### 0등급 — 가치 낮음(기록 보존)  (12건)

| # | 패턴 | 메모 |
|---|---|---|
| 1 | **role_concept / geo_part_of taxonomy as soft prompt hints** | Advisory only, unwired to querybuilder — near-worthless as an executable capability until made real. |
| 2 | **Cross-source SQL joins / window functions / OLAP drill-across / entity-graph traversal** | Out of scope by design — single-table metrics layer, no join graph, no OVER(); belongs upstream in dbt. |
| 3 | **Direct row-level extraction / bulk training-set / raw export via run_query** | Near-worthless: aggregation-only, single-table, hard-capped 5000, no OFFSET/keyset — go to Trino/dbt directly. |
| 4 | **Direct offensive weaponization (UNION/stacked/blind injection, DML, exfil)** | Genuinely blocked by read-only executors + identifier whitelist + escaped literals; only residual is DoS/cost amplification. |
| 5 | **Native cost/billing analytics ON this data** | No usage/billing/token tables in any of the 6 domains — the ontology cannot answer 'what did this cost' about itself. |
| 6 | **Consume raw positional rows as a durable result contract** | Positional arrays whose meaning depends on column order + data-dependent __label keys — no output schema to bind to. |
| 7 | **ontology_export as static schema-shape fixtures / OWL-DL reasoning substrate / triplestore A-Box** | Single physical table per source, no instance graph or object properties — a reasoner adds nothing; keep the export as honest labeling only. |
| 8 | **Trusting the ECharts canvas for accessibility as-shipped** | canvas init with no aria — fully opaque to AT; useful only as the thing to replace. |
| 9 | **_label_from_desc auto-derived labels as a localization source** | Lossy 28-char truncation splitting on bare '.'/'—'; wrong provenance to translate from. |
| 10 | **Lineage/provenance browser via the current tool surface** | Near-worthless today — raw material exists in the snapshot but is dropped at the Registry boundary; fix wiring to unlock. |
| 11 | **Culture: SUM over rank/attribute measures (seat_count, perf_count, hour_of_day)** | Builds but is domain-nonsense (double-counts capacity across snapshots, sums clock hours). |
| 12 | **Environment facility operation summaries (env_facility_operation)** | 51 rows, weak causal link to commerce dynamics — recorded for catalog completeness only. |
---

## 부록 — 페르소나 리뷰 통계
- 페르소나 30명 · 제시된 활용 178건 · 지적된 리스크 210건 · "이건 만들어야 한다" 제안 30건.
- 위 표는 그것을 중복 제거·등급화한 **111개 고유 패턴**이다(3등급 51 · 2등급 33 · 1등급 15 · 0등급 12).
- 5명 이상이 독립적으로 지목한 결함군(가산성 미집행·롤업 광고 불일치·절단 비결정성·토큰 낭비·
  코드 승격 누락·k익명성/비용 게이트 부재)은 [07-opus-handoff-guideline.md](07-opus-handoff-guideline.md)
  에 구현 지침으로 옮겼고, 그중 즉시 조치분은 이미 반영했다(02 §B, 07 §완료).
