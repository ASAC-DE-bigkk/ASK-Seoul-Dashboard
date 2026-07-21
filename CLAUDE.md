# CLAUDE.md — ASK SEOUL Dashboard 진입점 (Claude Code 전용)

이 파일은 Claude Code로 `dashboard/` 저장소를 작업할 때의 진입점이다.
이 저장소는 상위 `sample/` 프로젝트의 Git 서브모듈이며, 데이터 카탈로그와 Charts Studio를
FastAPI로 서빙한다.

## 작업 규약 정본은 SHARE.md

이 저장소의 작업 규약 **정본은 [SHARE.md](SHARE.md)다.** 먼저 SHARE.md를 처음부터 끝까지 읽고
그 규약을 그대로 따른 뒤, 아래 **Claude Code 전용 항목**을 얹어 적용한다. 규약 본문은
SHARE.md에만 두고 여기서 중복하지 않는다([AGENTS.md](AGENTS.md)도 같은 SHARE.md를 체이닝한다).

## 읽는 순서

1. 이 파일 `CLAUDE.md`
2. [SHARE.md](SHARE.md) — 작업 규약 정본 (§0의 나머지 읽기 순서를 따른다)
3. [docs/HERITAGE.md](docs/HERITAGE.md) 이하 SHARE.md §0이 가리키는 문서
4. 실제 구현 파일과 테스트/검증 경로

---

## Claude Code 전용 항목

### 1. 실행 환경 — Windows PowerShell 우선

- 이 저장소의 기본 사용자 셸은 **Windows PowerShell**이다(Bash 도구도 있으나 POSIX 전용).
  SHARE.md·README·docs의 실행 예시 상당수는 bash(`set -a; source .env.local; set +a`,
  `.venv/bin/...`)로 쓰여 있다. **그대로 실행하면 안 된다** — `source`·`set -a`가 없어
  `.env.local`의 `AUTH_MODE=local_auto`가 앱에 주입되지 않고, 기본값 `required`로 떠서
  로그인 화면으로 떨어진다.
- 실행·검증 명령은 항상 SHARE.md **§0.1의 두 환경 대응표**를 참조해 PowerShell 경로로 바꾼다
  (`.venv\Scripts\...`, `py -3 -m venv .venv`).
- 로컬 member 자동 인증(로그인 생략)은 PowerShell에서 아래 한 줄로 실행한다.

  ```powershell
  pwsh scripts/run_local.ps1
  # 또는:  powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1
  ```

  이 스크립트가 `.env.local` 생성·로드 → 인증 DB 초기화 → uvicorn 기동을 일괄 수행한다.
  기대 결과: `/charts`가 로그인 없이 열리고 사용자 `local-analyst`(역할 `일반회원`)로 인식된다.

### 2. 검증

- SHARE.md **§11 검증 게이트**를 변경 범위에 맞게 실행하되, Windows 경로를 쓴다.

  ```powershell
  .venv\Scripts\python -m compileall -q app extract.py
  .venv\Scripts\python -m pytest -q
  ```

- 런타임 동작 확인이 필요한 변경(인증·화면·차트 등)은 문서/테스트만으로 끝내지 말고
  실제 서버를 기동해 `/health`·해당 화면을 구동하고, `/charts?selftest=1`의 탭 제목
  `SELFTEST_ALL_PASS`까지 확인한다. 필요하면 `verify`·`run` 스킬을 사용한다.

### 3. 이슈·브랜치·PR

- SHARE.md **§12**를 따른다: 이슈 → 이슈 번호 브랜치(`dev` 기준) → 한국어 conventional 커밋 → PR.
- **원격 push·PR 생성은 사용자 승인 후**에만 수행한다. 로컬 커밋까지는 논리 단위로 만들 수 있다.
- 상위 `sample/`의 submodule pointer를 이 작업과 함께 임의로 커밋하지 않는다.

### 4. 산출물 언어·경계

- 사람이 읽는 산출물(문서·PR·응답)은 **한국어**로 작성한다.
- 작업 경계는 SHARE.md §2를 지킨다: 원칙적으로 `dashboard/` 내부만 수정하고, 상위 `sample/`·
  `dags/`·`dbt/`·Compose·Trino 설정 변경은 분리해 이유·영향·대안을 먼저 설명하고 동의를 받는다.
