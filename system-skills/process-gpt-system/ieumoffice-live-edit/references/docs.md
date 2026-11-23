# 워드(docs) 편집기

`office_live_read` 는 블록 목록을 `번호|종류|미리보기` 줄로 돌려준다(번호는 0부터).

## 고르는 순서

1. **글 일부를 바꿀 때**(표 칸 안의 글 포함) — 서식이 그대로 남는 `office_live_apply_ops` 의 `findReplace`:
   ```json
   [{"op": "findReplace", "find": "2026년 10월", "replace": "2026년 11월"}]
   ```
   `matchCase`, `target`({"blockIndexes": [5]} 등)으로 범위를 좁힐 수 있다. 한 블록 안에서만 찾는다(블록을 넘는 글은 안 걸린다).
2. **문단 하나를 통째로 바꿀 때만** `office_live_replace` — `match_text`(바꿀 블록에 든 글)를 주면 번호가 밀려도 맞는다.
   번호(`start_block_index`~`end_block_index`)로 바꾸면 뒤 블록 번호가 달라지니 여러 곳은 뒤에서부터 바꾸거나 바꿀 때마다 다시 읽는다.
   **표 블록은 통째로 바꾸지 않는다** — 칸 글은 findReplace, 행은 insertTableRow.
3. **새 문단** — `office_live_write`(`html`, `after_block_index`). html 은 p·h1~h3·ul/ol/li·table·strong·em 만.
   위치를 안 주면 끝에 붙는다.
4. **그 밖의 서식·구조** — `office_live_apply_ops` 의 연산(`office_guide` format=docx):
   `setFont`, `setParagraphFormat`, `setHeadingLevel`, `deleteBlocks`, `moveBlocks`, `setList`,
   `insertTableRow {target, row(0부터), position, count}`, `deleteTableRow`, `insertTableColumn`, `setTableCellFormat` 등.

## 주의

- 자리 표시 글(「(작성 예정)」「○○○」 같은 것)을 채울 때는 findReplace 나 match_text 를 쓴다.
- 표에 행을 더한 뒤 칸 글을 채울 때는 다시 읽어 새 행의 위치를 확인한다.
