"""chat — Ask Chat 온톨로지·D1(SQLite) 근거 조회 LLM 채팅 (격리 번들).

이 패키지 밖(main.py)에서는 `router` 만 가져다 쓴다.
온톨로지 카드·제약 쿼리 빌더·D1/SQLite 실행기·LLM 클라이언트까지 전부 이 폴더 안에서 관리한다.
설계 정본: docs/chat-design.md
"""
from .router import router  # noqa: F401
