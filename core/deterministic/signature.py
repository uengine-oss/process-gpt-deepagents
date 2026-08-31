"""쓰기 도구 호출 판별.

보상(undo) 입력을 만들 때 읽기 전용 호출을 제외해야 한다. 조회를 "되돌리려" 들면
안 되기 때문이다. 그런데 SQL 실행 도구의 이름은 테넌트 MCP 구성마다 다르므로
`execute_sql` 같은 이름으로 분기할 수 없다. 인자 값의 내용으로 판별한다.

고착화 코드 생성 쪽(지문·파라미터 식별)은 completion 서비스가 담당한다
(`deterministic_signature.py`). 여기에는 실행 런타임이 필요로 하는 판별 부분만 둔다.
"""

from __future__ import annotations

import re
from typing import Any

# 인자 값이 SQL인지 판별하는 선두 키워드.
_SQL_HEADS = (
    "SELECT", "INSERT", "UPDATE", "DELETE", "MERGE",
    "WITH", "CREATE", "ALTER", "DROP", "TRUNCATE", "EXPLAIN",
)
# 부수효과가 없는 읽기 전용 구문.
_READONLY_HEADS = ("SELECT", "WITH", "EXPLAIN", "SHOW")

_WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE)\b"
)


def _strip_leading_noise(value: str) -> str:
    """선두 공백과 주석을 걷어낸 나머지를 돌려준다."""
    i, n = 0, len(value)
    while i < n:
        if value[i].isspace():
            i += 1
        elif value.startswith("--", i):
            j = value.find("\n", i)
            i = n if j < 0 else j + 1
        elif value.startswith("/*", i):
            j = value.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            break
    return value[i:]


def _blank_out_literals(sql: str) -> str:
    """문자열 리터럴·식별자·주석을 공백으로 지운다(키워드 탐색용).

    큰따옴표는 PostgreSQL에서 식별자이므로 문자열과 구분해 다룬다. 둘 다 지우지만
    구분해서 훑어야 따옴표 안의 키워드를 오인하지 않는다.
    """
    out = list(sql)
    i, n = 0, len(sql)

    def blank(start: int, end: int) -> None:
        for k in range(start, min(end, n)):
            out[k] = " "

    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif ch in ("'", '"'):
            quote = ch
            start = i
            i += 1
            while i < n:
                if sql[i] == quote:
                    if sql.startswith(quote * 2, i):
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            blank(start, i)
        else:
            i += 1
    return "".join(out)


def looks_like_sql(value: Any) -> bool:
    """문자열 값이 SQL 구문으로 시작하는지 본다."""
    if not isinstance(value, str):
        return False
    head = _strip_leading_noise(value)
    if not head:
        return False
    return head.split(None, 1)[0].upper() in _SQL_HEADS


def is_readonly_sql(value: str) -> bool:
    """읽기 전용 SQL인지 본다. 쓰기 구문이 하나라도 섞이면 False."""
    head = _strip_leading_noise(value)
    if not head:
        return False
    if head.split(None, 1)[0].upper() not in _READONLY_HEADS:
        return False
    # `WITH ... INSERT/UPDATE/DELETE` 형태의 쓰기 CTE를 걸러낸다.
    return not _WRITE_KEYWORDS.search(_blank_out_literals(value).upper())


def is_write_call(tool_name: str | None, args: dict[str, Any], excluded: set[str]) -> bool:
    """보상 대상이 되는 쓰기 호출인지 판정한다.

    도구 이름이 아니라 인자 내용으로 SQL 여부와 읽기 전용 여부를 본다. SQL 인자가
    전혀 없는 도구는 부수효과가 있다고 보수적으로 가정한다.
    """
    if not tool_name or tool_name in excluded:
        return False
    sql_values = [v for v in (args or {}).values() if looks_like_sql(v)]
    if sql_values:
        return any(not is_readonly_sql(v) for v in sql_values)
    return True
