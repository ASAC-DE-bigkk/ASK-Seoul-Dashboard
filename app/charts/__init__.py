"""charts — Commerce Gold 차트 스튜디오 모듈 (격리 번들).

이 패키지 밖(main.py)에서는 `router` 만 가져다 쓴다.
온톨로지(필드 의미역)·쿼리 빌더·Trino 실행기·레이아웃 저장까지 전부 이 폴더 안에서 관리한다.
"""
from .router import router  # noqa: F401
