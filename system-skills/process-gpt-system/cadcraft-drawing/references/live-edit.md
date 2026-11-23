# CADCraft 편집기에 열린 도면 고치기

메시지의 시스템 사실이 「사용자가 지금 CADCraft 에서 '<파일>' 을 열어 두었다」고 알려 주면 `cadcraft_` 도구는 그 화면에 바로 적용된다.

1. 확인: `cadcraft_inspect_drawing`(레이어·개수·범위), `cadcraft_find_text`(방 이름·주석으로 위치 찾기),
   `cadcraft_query_entities`(종류·레이어·창 범위로 요소와 handle).
2. 고치기: `cadcraft_command_line`·`cadcraft_script`·`cadcraft_execute`(예 `move {handles:["10A"], delta:[0.5,0]}`).
   치수를 새로 넣으면 그 도면의 치수 스타일을 먼저 확인한다(미터 도면이면 `dimstyle.md`).
3. `cadcraft_render` 로 직접 본다.

하지 않는 것: `cadcraft_new_drawing`·`cadcraft_open`(열린 도면이 바뀐다), `cadcraft_save`(저장은 사용자가 한다).
끝나면 바꾼 것을 짧게 답한다.
