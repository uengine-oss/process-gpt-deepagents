# -*- coding: utf-8 -*-
"""SQL 역연산 — 관측된 구문에서 되돌리는 구문을 만든다.

completion 의 `deterministic_signature` 와 같은 규칙이다. 두 서비스는 빌드 컨텍스트가
갈려 모듈을 공유할 수 없어 사본으로 둔다. 규칙이 어긋나면 굳힐 때 "되돌릴 수 있다"고
판정한 것을 실행할 때 되돌리지 못한다 — `tests/test_compensation.py` 가 양쪽 동작을
같은 표본으로 고정한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .signature import _blank_out_literals, looks_like_sql

@dataclass(frozen=True)
class SqlLiteral:
    """정규화 과정에서 `?`로 치환된 리터럴 하나."""

    value: Any
    kind: str  # "string" | "number"
    start: int  # 원문에서의 시작 오프셋
    end: int  # 원문에서의 끝 오프셋(배타)
    name_hint: str | None = None

def _scan(sql: str) -> list[tuple[str, int, int]]:
    """SQL을 훑어 (종류, 시작, 끝) 구간 목록을 만든다.

    종류: literal-string, literal-number, ident-quoted, comment, plain.
    큰따옴표는 PostgreSQL에서 **식별자**이므로 리터럴로 취급하지 않는다. 이를 지우면
    서로 다른 테이블이 같은 지문을 갖게 되어 잘못된 고착화로 이어진다.
    """
    spans: list[tuple[str, int, int]] = []
    i, n = 0, len(sql)
    plain_start = 0

    def flush(upto: int) -> None:
        if upto > plain_start:
            spans.append(("plain", plain_start, upto))

    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            flush(i)
            j = sql.find("\n", i)
            j = n if j < 0 else j
            spans.append(("comment", i, j))
            i = plain_start = j
        elif sql.startswith("/*", i):
            flush(i)
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            spans.append(("comment", i, j))
            i = plain_start = j
        elif ch == "'" or (ch in "eE" and sql.startswith("'", i + 1)):
            flush(i)
            start = i
            escaped = ch in "eE"
            i += 2 if escaped else 1
            while i < n:
                if escaped and sql[i] == "\\":
                    i += 2
                    continue
                if sql[i] == "'":
                    if sql.startswith("''", i):  # 표준 SQL의 '' 이스케이프
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            spans.append(("literal-string", start, i))
            plain_start = i
        elif ch == '"':
            flush(i)
            start = i
            i += 1
            while i < n:
                if sql[i] == '"':
                    if sql.startswith('""', i):
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            spans.append(("ident-quoted", start, i))
            plain_start = i
        elif ch.isdigit() and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            flush(i)
            start = i
            while i < n and (sql[i].isdigit() or sql[i] == "."):
                i += 1
            spans.append(("literal-number", start, i))
            plain_start = i
        else:
            i += 1
    flush(n)
    return spans

def _literal_value(kind: str, raw: str) -> Any:
    if kind == "literal-number":
        return float(raw) if "." in raw else int(raw)
    body = raw
    if body[:1] in ("e", "E"):
        body = body[1:]
    body = body[1:-1] if len(body) >= 2 else body
    return body.replace("''", "'")

_INSERT_STMT = re.compile(
    r"\bINSERT\s+INTO\s+([^\s(]+)\s*\(([^()]*)\)\s*VALUES\s*\(", re.IGNORECASE
)
# `UPDATE t SET c = c + 3 WHERE …` 처럼 제자리 증감만 되돌린다. 값을 덮어쓴 UPDATE는
# 이전 값을 모르므로 되돌릴 수 없다.
_UPDATE_DELTA = re.compile(
    r"^\s*UPDATE\s+(?P<table>[^\s]+)\s+SET\s+(?P<column>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(?P=column)\s*(?P<sign>[+-])\s*(?P<amount>\d+(?:\.\d+)?)\s*(?P<rest>WHERE\b.*)?$",
    re.IGNORECASE | re.DOTALL,
)

def invert_sql(sql: str) -> str | None:
    """이 SQL을 되돌리는 SQL. 되돌릴 수 없으면 None.

    - `INSERT INTO t (컬럼들) VALUES (값들)` → 넣은 값 전부를 조건으로 하는 `DELETE`.
      일부 컬럼만 조건에 넣으면 남의 행까지 지운다. 값은 원문 그대로 옮겨 따옴표·형변환을
      보존한다.
    - `UPDATE t SET c = c + n …` → 부호를 뒤집은 같은 구문. `WHERE` 절은 그대로 둔다.
    - `DELETE`, 값을 덮어쓴 `UPDATE`, `CREATE`/`DROP` → 이전 상태를 모르므로 되돌릴 수 없다.
    """
    text = str(sql or "")
    if not looks_like_sql(text):
        return None
    blanked = _blank_out_literals(text)

    match = _INSERT_STMT.search(blanked)
    if match:
        table = text[match.start(1):match.end(1)]
        columns = [c.strip() for c in text[match.start(2):match.end(2)].split(",")]
        close = blanked.find(")", match.end())
        if close < 0 or not all(columns):
            return None
        literals = [
            lit for lit in _sql_literals(text) if match.end() <= lit.start < close
        ]
        if len(literals) != len(columns):
            # 값 자리에 함수 호출 같은 것이 섞였다. 짝이 어긋난 조건으로 지우면
            # 엉뚱한 행이 사라진다.
            return None
        pairs = [
            f"{column} = {text[lit.start:lit.end]}"
            for column, lit in zip(columns, literals)
        ]
        return f"DELETE FROM {table} WHERE " + " AND ".join(pairs)

    delta = _UPDATE_DELTA.match(text.strip())
    if delta:
        flipped = "-" if delta.group("sign") == "+" else "+"
        column, amount = delta.group("column"), delta.group("amount")
        rest = (delta.group("rest") or "").strip()
        statement = f"UPDATE {delta.group('table')} SET {column} = {column} {flipped} {amount}"
        return f"{statement} {rest}".strip()

    return None


def _sql_literals(sql: str) -> list[SqlLiteral]:
    """치환 가능한 리터럴과 원문에서의 위치. 역연산이 값을 원문 그대로 옮기는 데 쓴다."""
    literals: list[SqlLiteral] = []
    for kind, start, end in _scan(sql):
        if kind in ("literal-string", "literal-number"):
            literals.append(
                SqlLiteral(
                    value=_literal_value(kind, sql[start:end]),
                    kind="number" if kind == "literal-number" else "string",
                    start=start,
                    end=end,
                )
            )
    return literals

