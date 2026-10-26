"""식별 화면에서 전환한 프로세스 — 도출된 흐름은 그대로 두고 폼·스킬·에이전트만 보강한다.

프로세스 식별 화면(인계 기록 → process-miner)이 도출한 정의를 실행 가능한 프로세스로 바꿀 때,
생성 에이전트가 흐름을 다시 그리면 인계 때 일하던 담당자·순서와 다른 프로세스가 실행된다.
같은 업무를 다시 전환할 때마다 흐름이 달라지기도 한다.

그래서 흐름은 LLM 이 옮겨 적지 않는다.
- 전환 대화방(`chat_rooms.context.identification.definition`)에 실린 도출 정의를 정본으로 삼는다.
- 첫 턴 전에 그 정의를 프로세스 폴더의 `process-definition.json` 으로 깔아 둔다(`seed_identified_process`).
- 검증·통합 때 구조(단계·담당 역할·게이트웨이·연결·분기값)를 정본으로 되돌리고, 보강 필드만
  에이전트 결과에서 가져온다(`keep_identified_structure`). 되돌린 변경은 이유와 함께 남긴다.
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 사람이 하던 단계를 에이전트가 대신 끝내지 않도록, 보강으로 붙는 에이전트는 초안만 만든다.
_DRAFT = "draft"
# 단계(Activity)에서 생성 단계가 보강할 수 있는 필드. 이름·역할·종류는 도출 구조다.
_ACTIVITY_SUPPLEMENT = (
    "tool", "skills", "inputData", "outputData", "description", "instruction",
    "checkpoints", "attachments", "customProperties", "duration", "properties",
)
# 게이트웨이에서 보강할 수 있는 필드(분기 판단을 읽을 폼 필드 · DMN).
_GATEWAY_SUPPLEMENT = ("conditionData", "dmn", "description")
NOTES_FILE = ".identified-notes.json"

_cache: dict[str, dict | None] = {}


def load_identified_definition(room_id: str) -> dict | None:
    """전환 대화방에 실린 도출 정의(flattened). 전환 방이 아니면 None."""
    rid = str(room_id or "").strip()
    if not rid:
        return None
    if rid in _cache:
        return copy.deepcopy(_cache[rid])
    definition = None
    try:
        from ..storage.db import get_supabase

        resp = get_supabase().table("chat_rooms").select("context").eq("id", rid).limit(1).execute()
        rows = getattr(resp, "data", None) or []
        ctx = (rows[0] or {}).get("context") if rows else None
        ident = ctx.get("identification") if isinstance(ctx, dict) else None
        cand = ident.get("definition") if isinstance(ident, dict) else None
        if isinstance(cand, dict) and (cand.get("activities") or cand.get("elements")):
            definition = cand
    except Exception:
        logger.warning("전환 방 정의 조회 실패 | room=%s", rid, exc_info=True)
        return None  # 조회 실패는 캐시하지 않는다(다음 호출에서 다시 본다)
    _cache[rid] = definition
    return copy.deepcopy(definition)


def to_elements(definition: dict) -> dict:
    """도출 정의(flattened)를 생성 작업 파일 형식(elements[])으로. 이미 elements[] 면 그대로 정규화."""
    from .tools import _normalize_to_processgpt_schema

    pd, _ = _normalize_to_processgpt_schema(copy.deepcopy(definition))
    return pd


def seed_identified_process(room_id: str, process_dir: Path) -> bool:
    """전환 방이면 도출 정의를 프로세스 폴더에 깔아 둔다. 이미 있으면 건드리지 않는다."""
    original = load_identified_definition(room_id)
    if not original:
        return False
    target = process_dir / "process-definition.json"
    if target.is_file():
        return False
    from .tools import _atomic_write_json

    _atomic_write_json(target, to_elements(original))
    logger.info("도출 정의를 작업 폴더에 깔았다 | room=%s dir=%s", room_id, process_dir)
    return True


def _by_kind(pd: dict) -> tuple[dict, dict]:
    nodes: dict[str, dict] = {}
    seqs: dict[tuple, dict] = {}
    for el in pd.get("elements") or []:
        if not isinstance(el, dict):
            continue
        if el.get("elementType") == "Sequence":
            seqs[(str(el.get("source") or ""), str(el.get("target") or ""))] = el
        elif el.get("id"):
            nodes[str(el["id"])] = el
    return nodes, seqs


def _label(el: dict) -> str:
    return str(el.get("name") or el.get("id") or "")


def keep_identified_structure(original: dict, current: dict) -> tuple[dict, list[str]]:
    """정본(도출 정의)의 구조 위에 current 의 보강 필드만 얹는다. (결과, 되돌린 변경 목록) 반환.

    구조 = 노드(id·종류·이름·담당 역할·수행 에이전트), 연결(source→target)과 분기값, 역할 목록.
    보강 = 폼(tool)·입출력·설명·지침·스킬·게이트웨이 판단 필드(conditionData)·DMN.
    """
    base = to_elements(original)
    cur = to_elements(current)
    o_nodes, o_seqs = _by_kind(base)
    c_nodes, c_seqs = _by_kind(cur)
    notes: list[str] = []

    for nid, el in o_nodes.items():
        c = c_nodes.get(nid)
        if c is None or c.get("elementType") != el.get("elementType"):
            notes.append(f"'{_label(el)}' 를 빼거나 바꾸려 했지만 도출된 단계라 그대로 두었습니다.")
            continue
        if el.get("elementType") == "Activity":
            for key in ("name", "role"):
                if str(c.get(key) or "") != str(el.get(key) or ""):
                    notes.append(
                        f"'{_label(el)}' 의 {'이름' if key == 'name' else '담당'}을 "
                        f"'{c.get(key)}'(으)로 바꾸려 했지만 인계 기록의 값('{el.get(key)}')을 유지했습니다."
                    )
            for key in _ACTIVITY_SUPPLEMENT:
                if key in c:
                    el[key] = c[key]
            if el.get("agent"):
                # 인계 기록에서 에이전트가 맡던 단계 — 그 에이전트·모드를 그대로 둔다
                pass
            elif c.get("agent") or c.get("skills"):
                # 사람이 하던 단계에 붙인 에이전트·스킬은 초안만 만든다(담당자가 확인하고 제출)
                if c.get("agent"):
                    el["agent"] = c["agent"]
                el["agentMode"] = _DRAFT
                el["orchestration"] = c.get("orchestration") or "deepagents"
        elif el.get("elementType") == "Gateway":
            if str(c.get("type") or "") != str(el.get("type") or ""):
                notes.append(f"'{_label(el)}' 게이트웨이 종류를 바꾸려 했지만 도출 구조를 유지했습니다.")
            for key in _GATEWAY_SUPPLEMENT:
                if key in c:
                    el[key] = c[key]

    for nid, c in c_nodes.items():
        if nid not in o_nodes:
            notes.append(f"'{_label(c)}' 을(를) 추가하려 했지만 인계 기록에 없는 단계라 넣지 않았습니다.")

    for key, seq in o_seqs.items():
        c = c_seqs.get(key)
        if c is None:
            notes.append(
                f"'{_label(o_nodes.get(key[0], {'id': key[0]}))}' → '{_label(o_nodes.get(key[1], {'id': key[1]}))}' "
                "연결을 빼려 했지만 관측된 흐름이라 유지했습니다."
            )
        elif str(c.get("condition") or "") != str(seq.get("condition") or ""):
            notes.append(
                f"분기값 '{seq.get('condition')}' 을(를) '{c.get('condition')}'(으)로 바꾸려 했지만 그대로 두었습니다."
            )
    for key in c_seqs:
        if key not in o_seqs and key[0] in o_nodes and key[1] in o_nodes:
            notes.append(
                f"'{_label(o_nodes[key[0]])}' → '{_label(o_nodes[key[1]])}' 연결을 더하려 했지만 "
                "관측되지 않은 순서라 넣지 않았습니다."
            )

    merged = dict(cur)
    for key in ("processDefinitionName", "roles"):
        if base.get(key):
            merged[key] = base[key]
    merged["elements"] = base["elements"]
    # 같은 문장이 여러 번 나오지 않게(같은 노드를 여러 이유로 건드린 경우)
    seen: set[str] = set()
    uniq = [n for n in notes if not (n in seen or seen.add(n))]
    return merged, uniq


def apply_to_file(room_id: str, target: Path) -> list[str] | None:
    """전환 방이면 작업 파일의 구조를 정본으로 되돌려 다시 쓰고 되돌린 변경을 기록한다.

    전환 방이 아니면 None. 되돌린 변경은 같은 폴더의 `.identified-notes.json` 에 누적한다
    (턴이 끝나면 채팅 안내에 그 이유를 보인다).
    """
    original = load_identified_definition(room_id)
    if not original or not target.is_file():
        return None
    try:
        current = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        current = {}
    if isinstance(current, dict) and isinstance(current.get("processDefinition"), dict):
        current = current["processDefinition"]
    merged, notes = keep_identified_structure(original, current if isinstance(current, dict) else {})
    from .tools import _atomic_write_json

    _atomic_write_json(target, merged)
    if notes:
        record_notes(target.parent, notes)
    return notes


def record_notes(process_dir: Path, notes: list[str]) -> None:
    path = process_dir / NOTES_FILE
    try:
        prev = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    except Exception:
        prev = []
    merged = list(prev) if isinstance(prev, list) else []
    for n in notes:
        if n not in merged:
            merged.append(n)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")


def read_notes(process_dir: Path) -> list[str]:
    path = process_dir / NOTES_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [str(x) for x in data] if isinstance(data, list) else []
    except Exception:
        return []
