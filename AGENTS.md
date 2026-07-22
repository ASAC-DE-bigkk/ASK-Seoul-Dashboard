# AGENTS.md — ASK SEOUL Dashboard 진입점 (범용 에이전트·사람)

이 파일은 `dashboard/` 저장소에서 작업하는 사람과 범용 AI 에이전트의 진입점이다.
이 저장소는 상위 `sample/` 프로젝트의 Git 서브모듈이며, 데이터 카탈로그와 Charts Studio를
FastAPI로 서빙한다.

## 작업 규약 정본은 SHARE.md

이 저장소의 작업 규약 **정본은 [SHARE.md](SHARE.md)다.** 코드를 설계·수정하기 전에
반드시 SHARE.md를 처음부터 끝까지 읽고 그 규약을 따른다. 규약 본문(실행 모드, 작업 경계,
데이터 경로, Charts 격리, 온톨로지·드리프트, 데이터 정확성, 레이아웃, API·보안, 검증 게이트,
이슈·PR 규칙, 최종 체크리스트)은 SHARE.md에 한 벌만 두고 이 진입점에는 중복하지 않는다.
그래야 진입점이 늘어도 규약이 어긋나지 않는다(문서 드리프트 방지).

## 읽는 순서

1. 이 파일 `AGENTS.md`
2. [SHARE.md](SHARE.md) — 작업 규약 정본 (여기서부터 §0의 나머지 읽기 순서를 따른다)
3. [docs/HERITAGE.md](docs/HERITAGE.md) 이하 SHARE.md §0이 가리키는 문서
4. 실제 구현 파일과 테스트/검증 경로

## 진입점 사용 규칙

- 이 `AGENTS.md`만 읽는 러너(Codex 등)도 위 링크를 따라 [SHARE.md](SHARE.md) 전체를 적용한다.
  AGENTS.md에 규약이 짧게 보인다고 해서 규약이 적은 것이 아니다.
- **Claude Code로 작업한다면** [CLAUDE.md](CLAUDE.md)를 진입점으로 사용한다.
  CLAUDE.md도 같은 SHARE.md를 체이닝하며, Claude Code 전용 실행·검증 항목을 추가로 적용한다.
- 실행 환경은 Windows(PowerShell)와 macOS/Linux(bash) 둘 다일 수 있다. 실행·검증 명령은
  SHARE.md §0.1의 두 환경 대응표를 반드시 함께 확인한다.
