from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import gspread
from google.oauth2.service_account import Credentials


SCOPES_READONLY_WITH_SEARCH = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    # 사용자가 "시트 ID"를 모르는 경우가 많아 제목으로 매칭하려면 Drive 조회 권한이 필요합니다.
    "https://www.googleapis.com/auth/drive.readonly",
]

# 수정이 필요하면 위 스코프를 아래처럼 바꾸세요.
# SCOPES_EDIT_WITH_SEARCH = [
#     "https://www.googleapis.com/auth/spreadsheets",
#     "https://www.googleapis.com/auth/drive",
# ]


def _normalize_title(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _looks_like_spreadsheet_id(s: str) -> bool:
    # Google file IDs are URL-safe base64-ish strings; keep it permissive.
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{20,}", s.strip()))


def _extract_spreadsheet_id(value: str) -> str | None:
    v = value.strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", v)
    if m:
        return m.group(1)
    if _looks_like_spreadsheet_id(v):
        return v
    return None


def _iter_spreadsheet_files(client: gspread.Client) -> Iterable[dict[str, Any]]:
    # Requires drive.readonly (or drive) scope.
    # gspread returns items like: {"id": "...", "name": "..."} (fields may vary by version).
    return client.list_spreadsheet_files()


def open_spreadsheet_smart(client: gspread.Client, sheet_name_or_id_or_url: str) -> gspread.Spreadsheet:
    sheet_id = _extract_spreadsheet_id(sheet_name_or_id_or_url)
    if sheet_id:
        return client.open_by_key(sheet_id)

    target = _normalize_title(sheet_name_or_id_or_url)
    files = list(_iter_spreadsheet_files(client))
    if not files:
        raise RuntimeError(
            "접근 가능한 스프레드시트를 찾지 못했습니다. "
            "서비스 계정 이메일이 대상 스프레드시트에 공유되어 있는지 확인해 주세요."
        )

    def get_title(f: dict[str, Any]) -> str:
        return str(f.get("name") or f.get("title") or "")

    exact_matches = [f for f in files if _normalize_title(get_title(f)) == target]
    if len(exact_matches) == 1:
        return client.open_by_key(exact_matches[0]["id"])
    if len(exact_matches) > 1:
        ids = ", ".join(f.get("id", "") for f in exact_matches)
        raise RuntimeError(f"같은 제목의 스프레드시트가 {len(exact_matches)}개 있습니다. ID로 지정해 주세요. 후보 ID: {ids}")

    partial_matches = [f for f in files if target and target in _normalize_title(get_title(f))]
    if len(partial_matches) == 1:
        return client.open_by_key(partial_matches[0]["id"])

    # 매칭 실패(또는 다수) 시: 사용자가 다음 입력을 쉽게 할 수 있도록 후보를 보여줍니다.
    candidates = sorted(((get_title(f), f.get("id", "")) for f in partial_matches[:20]), key=lambda x: _normalize_title(x[0]))
    if not candidates:
        sample = sorted(((get_title(f), f.get("id", "")) for f in files[:20]), key=lambda x: _normalize_title(x[0]))
        hint = "\n".join(f"- {name} ({fid})" for name, fid in sample if name)
        raise RuntimeError(f"'{sheet_name_or_id_or_url}' 제목과 매칭되는 스프레드시트를 찾지 못했습니다. 접근 가능한 예시(최대 20개):\n{hint}")

    hint = "\n".join(f"- {name} ({fid})" for name, fid in candidates if name)
    raise RuntimeError(f"'{sheet_name_or_id_or_url}' 제목과 유사한 스프레드시트가 여러 개입니다. 더 정확한 제목이나 ID로 지정해 주세요:\n{hint}")


def _is_blank(s: str | None) -> bool:
    return s is None or str(s).strip() == ""


def _stringish_ratio(values: Sequence[str]) -> float:
    # Values already strings; treat numeric-like strings as not-stringish.
    if not values:
        return 0.0
    numeric_like = 0
    non_blank = 0
    for v in values:
        t = v.strip()
        if t == "":
            continue
        non_blank += 1
        if re.fullmatch(r"[-+]?(\d+(\.\d+)?|\.\d+)", t):
            numeric_like += 1
    if non_blank == 0:
        return 0.0
    return 1.0 - (numeric_like / non_blank)


def _dedupe_headers(headers: list[str]) -> list[str]:
    """
    Make headers unique, even when the original headers already contain suffix-like names.

    Example that breaks naive implementations:
      ["A", "A", "A_2"]  -> must become ["A", "A_2", "A_2_2"] (or similar), not ["A", "A_2", "A_2"].
    """
    used: set[str] = set()
    out: list[str] = []
    for h in headers:
        base = str(h)
        candidate = base
        if candidate not in used:
            used.add(candidate)
            out.append(candidate)
            continue

        # Suffix until we find a unique key.
        n = 2
        while True:
            candidate = f"{base}_{n}"
            if candidate not in used:
                used.add(candidate)
                out.append(candidate)
                break
            n += 1
    return out


def _normalize_header_cell(value: str | None, col_index_1based: int) -> str:
    if value is None:
        return f"col_{col_index_1based}"
    s = str(value).strip()
    if s == "":
        return f"col_{col_index_1based}"
    s = re.sub(r"\s+", " ", s)
    return s


@dataclass(frozen=True)
class TableParseResult:
    header_row_1based: int
    headers: list[str]
    records: list[dict[str, str]]


def _trim_rect(rows: list[list[str]]) -> list[list[str]]:
    """
    Trim leading/trailing all-blank columns based on full rectangular data.
    rows: list of rows (each row list of cells) already padded to equal width.
    """
    if not rows:
        return rows
    width = max((len(r) for r in rows), default=0)
    if width == 0:
        return rows
    padded = [r + [""] * (width - len(r)) for r in rows]
    non_blank_cols = [False] * width
    for r in padded:
        for i, v in enumerate(r):
            if str(v).strip() != "":
                non_blank_cols[i] = True
    if not any(non_blank_cols):
        return padded
    left = next(i for i, ok in enumerate(non_blank_cols) if ok)
    right = len(non_blank_cols) - 1 - next(i for i, ok in enumerate(reversed(non_blank_cols)) if ok)
    return [r[left : right + 1] for r in padded]


def detect_header_row_1based(values: list[list[str]], max_scan_rows: int = 200) -> int:
    """
    Heuristic header-row detector.
    - Works for sheets where the top has 안내/설명/빈행 and the table starts later.
    - Picks the row with many non-blank, mostly string-like, high uniqueness.
    """
    scan = values[: max_scan_rows if max_scan_rows > 0 else len(values)]
    best_row = 1
    best_score = float("-inf")
    for idx0, row in enumerate(scan):
        # ignore very short rows
        if not row:
            continue
        non_blank = [c for c in row if str(c).strip() != ""]
        if len(non_blank) < 2:
            continue
        # uniqueness among non-blank header candidates
        norm = [re.sub(r"\s+", " ", str(c)).strip().lower() for c in non_blank]
        uniq = len(set(norm))
        uniq_ratio = uniq / max(len(norm), 1)
        str_ratio = _stringish_ratio([str(c) for c in non_blank])
        # penalize obvious "안내/설명" rows that are usually one long cell
        long_cell = max((len(str(c).strip()) for c in non_blank), default=0)
        multi_col_bonus = 1.0 if len(non_blank) >= 4 else 0.0
        score = (len(non_blank) * 2.0) + (uniq_ratio * 3.0) + (str_ratio * 2.0) + multi_col_bonus - (long_cell / 80.0)
        if score > best_score:
            best_score = score
            best_row = idx0 + 1
    return best_row


def worksheet_to_records_smart(
    worksheet: gspread.Worksheet,
    *,
    header_row_1based: int | None = None,
    max_scan_rows: int = 200,
    max_records: int | None = None,
) -> TableParseResult:
    # Using get_all_values is more robust than get_all_records for messy headers.
    values = worksheet.get_all_values()
    if not values:
        return TableParseResult(header_row_1based=1, headers=[], records=[])

    header_row = header_row_1based or detect_header_row_1based(values, max_scan_rows=max_scan_rows)
    header_idx0 = max(0, min(header_row - 1, len(values) - 1))

    # Trim columns based on header + data block below it.
    rect = _trim_rect(values[header_idx0 :])
    header_cells = rect[0] if rect else []
    headers = [_normalize_header_cell(v, i + 1) for i, v in enumerate(header_cells)]
    headers = _dedupe_headers(headers)

    data_rows = rect[1:] if len(rect) > 1 else []
    records: list[dict[str, str]] = []
    for r in data_rows:
        if all(str(v).strip() == "" for v in r):
            continue
        rec = {headers[i]: (r[i] if i < len(r) else "") for i in range(len(headers))}
        records.append(rec)
        if max_records is not None and len(records) >= max_records:
            break

    return TableParseResult(header_row_1based=header_idx0 + 1, headers=headers, records=records)


def main() -> None:
    parser = argparse.ArgumentParser(description="Google Sheets 조회/수정 핸들러 (헤더 자동 탐지 포함)")
    parser.add_argument("--sheet", required=True, help="스프레드시트 제목/ID/URL")
    parser.add_argument("--worksheet", default=None, help="워크시트 이름(미지정 시 첫 번째 시트)")
    parser.add_argument("--header-row", type=int, default=None, help="헤더 행 번호(1부터). 미지정 시 자동 탐지")
    parser.add_argument("--max-scan-rows", type=int, default=200, help="헤더 자동 탐지 시 스캔할 최대 행 수")
    parser.add_argument("--max-records", type=int, default=50, help="출력/검증용으로 최대 몇 행까지 변환할지")
    args = parser.parse_args()

    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES_READONLY_WITH_SEARCH)
    client = gspread.authorize(creds)

    # 사용자가 시트 ID를 모르는 경우를 대비해 "제목/ID/URL" 모두 받습니다.
    sheet = open_spreadsheet_smart(client, args.sheet)
    worksheet = sheet.worksheet(args.worksheet) if args.worksheet else sheet.sheet1

    parsed = worksheet_to_records_smart(
        worksheet,
        header_row_1based=args.header_row,
        max_scan_rows=args.max_scan_rows,
        max_records=args.max_records,
    )
    print(f"헤더 행(1부터): {parsed.header_row_1based}")
    print(f"헤더 수: {len(parsed.headers)}")
    print(f"변환된 레코드 수(비어있는 행 제외): {len(parsed.records)}")
    if parsed.records:
        print("첫 레코드 미리보기:")
        # 너무 길어지지 않게 일부만 출력
        first = parsed.records[0]
        preview_keys = list(first.keys())[: min(10, len(first))]
        preview = {k: first[k] for k in preview_keys}
        print(preview)

    # 데이터 수정 예제 (수정 스코프로 변경 필요)
    # worksheet.update("B2", [["Updated Value"]])

    print("데이터 조회 및 (필요 시) 수정이 완료되었습니다.")


if __name__ == "__main__":
    main()
