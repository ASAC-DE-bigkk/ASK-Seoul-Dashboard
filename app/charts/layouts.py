"""레이아웃 페이지 저장소 — JSON 파일 하나로 관리하는 얇은 영속 계층.

runtime 파일(data/layouts.json)은 gitignore 대상, 시드(data/layouts.seed.json)는 커밋 대상.
첫 기동 시 시드를 복사해 시작한다. 쓰기는 락 + 임시파일 원자 교체.
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
RUNTIME_PATH = DATA_DIR / "layouts.json"
SEED_PATH = DATA_DIR / "layouts.seed.json"

_lock = threading.Lock()


class NotFound(KeyError):
    pass


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


def _load() -> dict:
    if RUNTIME_PATH.exists():
        return json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
    if SEED_PATH.exists():
        doc = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    else:
        doc = {"version": 1, "pages": []}
    _save(doc)
    return doc


def _save(doc: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RUNTIME_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(RUNTIME_PATH)


def _find(doc: dict, page_id: str) -> dict:
    for p in doc["pages"]:
        if p["id"] == page_id:
            return p
    raise NotFound(page_id)


def list_pages() -> list[dict]:
    with _lock:
        doc = _load()
        return [{"id": p["id"], "name": p["name"], "chart_count": len(p.get("charts", []))}
                for p in doc["pages"]]


def get_page(page_id: str) -> dict:
    with _lock:
        return _find(_load(), page_id)


def create_page(name: str, charts: list[dict] | None = None) -> dict:
    with _lock:
        doc = _load()
        page = {"id": _new_id(), "name": name.strip() or "새 레이아웃", "charts": charts or []}
        doc["pages"].append(page)
        _save(doc)
        return page


def update_page(page_id: str, name: str | None = None, charts: list[dict] | None = None) -> dict:
    with _lock:
        doc = _load()
        page = _find(doc, page_id)
        if name is not None:
            page["name"] = name.strip() or page["name"]
        if charts is not None:
            page["charts"] = charts
        _save(doc)
        return page


def delete_page(page_id: str) -> None:
    with _lock:
        doc = _load()
        page = _find(doc, page_id)
        doc["pages"].remove(page)
        _save(doc)


def duplicate_page(page_id: str) -> dict:
    with _lock:
        doc = _load()
        src = _find(doc, page_id)
        copy = json.loads(json.dumps(src, ensure_ascii=False))
        copy["id"] = _new_id()
        copy["name"] = src["name"] + " (사본)"
        for chart in copy.get("charts", []):
            chart["id"] = _new_id()
        doc["pages"].insert(doc["pages"].index(src) + 1, copy)
        _save(doc)
        return copy


def reorder(ids: list[str]) -> list[dict]:
    with _lock:
        doc = _load()
        by_id = {p["id"]: p for p in doc["pages"]}
        # 길이까지 검사 — 중복 id 가 set 비교를 통과해 페이지가 복제 저장되는 것을 막는다
        if len(ids) != len(doc["pages"]) or set(ids) != set(by_id):
            raise NotFound("reorder id 목록이 현재 페이지와 다릅니다")
        doc["pages"] = [by_id[i] for i in ids]
        _save(doc)
        return [{"id": p["id"], "name": p["name"], "chart_count": len(p.get("charts", []))}
                for p in doc["pages"]]
