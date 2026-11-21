# 엑셀(sheets) 편집기

`office_live_read` 는 시트 요약을, `addresses`(예: `["A1:F20"]` 또는 `["B7","C7"]`)와 `sheet` 를 주면 그 셀 값을 돌려준다.
고치기는 모두 `office_live_apply_ops` 의 연산 배열이다(형식 전체는 `office_guide` format=xlsx). 주소는 A1 꼴, 행은 1부터.

| 하려는 일 | 연산 |
|---|---|
| 값 넣기 | `{"op":"set_cell","sheet":"견적","address":"C7","value":1200000}` — 숫자는 숫자로 |
| 수식 넣기 | `{"op":"set_formula","address":"F12","formula":"=SUM(F3:F11)"}` — 수식을 `type:"text"` 로 쓰지 않는다 |
| 여러 칸 한 번에 | `{"op":"set_range","start":"A8","values":[["품목",3,15000]]}` |
| 행 끼우기 | `{"op":"insert_rows","row":8,"count":1}` — 같은 배치의 뒤 연산은 밀린 주소를 쓴다 |
| 행 지우기 | `{"op":"delete_rows","row":8,"count":1}` |
| 찾아 바꾸기 | `{"op":"find_replace","range":"A1:H40","find":"…","replace":"…"}` |
| 정렬 | `{"op":"sort_range","range":"A2:F30","byColumn":"C","order":"desc","hasHeader":false}` |

## 행을 더할 때의 순서

1. 합계 행 바로 위에 `insert_rows` 로 끼운다(합계 아래에 붙이면 합계 범위에 안 들어간다).
2. 새 행에 값과, 이웃 행에 있던 수식(단가×수량 등)을 같은 꼴로 넣는다.
3. 합계·소계 수식의 범위가 새 행을 포함하는지 읽어 보고, 아니면 범위를 넓힌다.
4. 다시 읽어 합계 값이 맞는지 확인한다.

이미 있는 표 서식(테두리·채우기·숫자 형식)은 바꾸지 않는다. 시트가 여럿이면 `sheet` 를 꼭 적는다.
