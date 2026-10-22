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


# ---------------------------------------------------------------------------
# 셸 명령의 부수효과
# ---------------------------------------------------------------------------
# SQL은 읽기 전용을 가려내면서 셸은 무조건 부수효과로 세면, 아무것도 바꾸지 않는
# `date`·`printf` 가 되돌릴 대상에 들어간다. 셸은 되돌릴 방법이 이력에 없으므로 그
# 한 줄 때문에 활동 전체가 "되돌릴 수 없음"이 되어 재작업이 늘 에이전트로 넘어간다.
# 판정 규칙은 completion 의 `deterministic_signature` 와 같아야 한다 — 어긋나면 굳힐
# 때 "되돌릴 수 있다"고 저장해 둔 활동을 실행기가 되돌리지 못한다.

# 세상을 바꾸지 않는 프로그램. 여기 없는 이름은 바꾸는 것으로 본다 — 놓치는 쪽이 더 위험하다.
_READONLY_PROGRAMS = {
    "date", "echo", "printf", "ls", "ll", "cat", "pwd", "whoami", "hostname",
    "wc", "head", "tail", "grep", "egrep", "fgrep", "find", "which", "type",
    "stat", "du", "df", "env", "printenv", "basename", "dirname", "realpath",
    "readlink", "sort", "uniq", "cut", "tr", "diff", "file", "test", "true",
    "false", "sleep", "id", "uname", "seq", "expr", "jq", "awk", "sed", "python",
    "python3", "node",
}
# 자리에서 고쳐 쓰는 옵션. `sed -i` 는 읽기 도구처럼 보이지만 파일을 바꾼다.
_IN_PLACE = re.compile(r"^-i(\.|$)|^--in-place")
# 표준 스트림을 옮기는 표기(`2>&1`, `>&2`). 파일로 쓰는 리다이렉션과 구분한다.
_STREAM_REDIRECT = re.compile(r"\d*>&\d*|&>")
# 절 구분자. 이 중 하나라도 쓰는 절이 있으면 명령 전체가 부수효과를 낸다.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")
# 인터프리터에 스크립트를 먹이는 형태. 안에서 무엇을 하는지 알 수 없다.
_SCRIPT_INTERPRETERS = {"python", "python3", "node"}


def _mask_quoted(text: str) -> str:
    """따옴표 안을 공백으로 덮는다. 구분자·리다이렉션 탐색용."""
    out = list(text)
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
            out[index] = " "
        elif char in "'\"":
            quote = char
            out[index] = " "
    return "".join(out)


def _shell_segments(command: str) -> list[str]:
    """명령을 절 단위로 나눈다. 따옴표 안의 구분자는 무시한다."""
    masked = _mask_quoted(command)
    segments: list[str] = []
    cursor = 0
    for match in _SEGMENT_SPLIT.finditer(masked):
        segments.append(command[cursor:match.start()])
        cursor = match.end()
    segments.append(command[cursor:])
    return [segment for segment in segments if segment.strip()]


def _program_and_args(segment: str) -> tuple[str, list[str]]:
    """절의 실행 프로그램과 인자. 앞에 붙는 환경변수 대입은 건너뛴다."""
    tokens = [token for token in segment.split() if token]
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
        tokens = tokens[1:]
    if not tokens:
        return "", []
    program = tokens[0].strip("\"'").rsplit("/", 1)[-1]
    return program, tokens[1:]


def is_readonly_shell(command: str) -> bool:
    """셸 명령이 세상을 바꾸지 않는가.

    절 하나라도 바꾸면 명령 전체가 바꾸는 것이다 — `mkdir -p x && date` 는 읽기 전용이
    아니다. 파일로 내보내는 리다이렉션(`>`, `>>`)이 있어도 마찬가지다. 스트림을 옮기는
    `2>&1` 은 파일을 만들지 않으므로 리다이렉션으로 세지 않는다.
    """
    text = str(command or "")
    if not text.strip():
        return False
    masked = _STREAM_REDIRECT.sub(" ", _mask_quoted(text))
    if ">" in masked or "<<" in masked:
        return False
    for segment in _shell_segments(text):
        program, arguments = _program_and_args(segment)
        if program not in _READONLY_PROGRAMS:
            return False
        if program in _SCRIPT_INTERPRETERS:
            # 스크립트를 먹이는 호출은 안에서 무엇을 하는지 알 수 없다.
            return False
        if any(_IN_PLACE.match(argument) for argument in arguments):
            return False
    return True
