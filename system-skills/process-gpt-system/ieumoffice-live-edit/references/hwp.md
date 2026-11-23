# 한글(hwp) 편집기

`office_live_read` 가 문단과 표 목록을 돌려준다. **표 번호·행·열은 1부터**이고 읽는 순서대로 매겨진다.
사용자가 가리킨 「@표 N」이 이 번호다.

## 표 — `office_live_hwp_table`

| action | 인자 | 하는 일 |
|---|---|---|
| `read` | `table` | 표 내용 |
| `fill` | `table`, `cells: [{row, col, text}]` | 칸에 글 넣기(칸 서식은 그대로, 있던 글은 덮어씀) |
| `add_rows` | `table`, `at`(행), `where`(after·before), `count`(≤50) | 행 더하기(새 행은 이웃 행 서식) |
| `remove_rows` | `table`, `at`, `count` | 행 지우기 |

행을 더하면 새 행은 비어 있다 — **`add_rows` 뒤에 새 행을 `fill` 로 채운다**(필요하면 `read` 로 새 행 번호 확인).
합계·소계 칸이 있으면 숫자를 다시 계산해 `fill` 로 함께 고친다.

## 글 — `office_live_hwp_text`

| action | 인자 | 하는 일 |
|---|---|---|
| `replace` | `find`, `replace`, `occurrence`(같은 글이 여럿일 때 몇 번째, 1부터) | 글 바꾸기 |
| `insert` | `anchor`(이 글이 든 문단), `where`(after·before), `paragraphs: [..]` | 새 문단 넣기 |

## 하지 않는 것

- 그림은 이 도구로 고치지 않는다(이미지 편집은 `photocraft-editing`).
- 한글 문서는 `office_live_apply_ops` 의 워드·엑셀 연산을 받지 않는다.
