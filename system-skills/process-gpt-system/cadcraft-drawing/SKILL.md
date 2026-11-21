---
name: cadcraft-drawing
description: >
  치수가 있는 2D CAD 도면 — 평면도·배치도·설비/가구 배치·단면·입면 같은 기술 도면, DXF·DWG 파일 — 을
  새로 그리거나, 도면 파일을 열어 읽고 고치거나, 사용자가 CADCraft 편집기에 열어 둔 도면을 고칠 때 쓴다.
  단위·치수 스타일·렌더 확인·저장 순서를 정한다. 로고·포스터 같은 그래픽, 순서도·조직도, 3D 모델링에는 쓰지 않는다.
metadata:
  version: 1.0.0
  include_tools: >-
    cadcraft_start cadcraft_command_line cadcraft_script cadcraft_execute cadcraft_list_commands
    cadcraft_inspect_drawing cadcraft_query_entities cadcraft_render cadcraft_find_text
    cadcraft_new_drawing cadcraft_open cadcraft_save cadcraft_cancel
---

# CADCraft 로 도면 그리기·고치기

**이 도구들(`cadcraft_*`)이 목록에 없으면 이 스킬을 쓰지 않는다.** 스킬 전용 도구로 지정된 회사에서는 이 SKILL.md 를
읽은 다음 호출부터 도구가 보이니 그때 한 번 더 본다. 목록에 `cadcraft_start` 하나만 있으면 그것을 불러 켜고
「다음 메시지부터 CADCraft 도구를 쓸 수 있다」고 짧게 알린다. 그래도 없으면 CADCraft 가 없는 환경이다 — CADCraft 로
그렸다고 말하지 않는다.

사용자가 CADCraft 편집기에 도면을 열어 둔 상태(메시지의 시스템 사실)면 **5절**부터 본다.

## 1. 단위와 치수 스타일 먼저

- 단위는 **미터(1 = 1 m)** 로 그린다. 사용자가 mm 로 치수를 주면 m 로 바꿔 그리고 치수 표기는 요청대로 맞춘다.
- 치수를 넣기 전에 **반드시** 미터 도면용 치수 스타일을 만든다 — 기본 스타일은 미터 도면에서 치수 글자가 방 하나만큼 커진다.
  값과 글자 높이 규칙은 `references/dimstyle.md`.

## 2. 그리기

- 정확한 좌표가 있는 요소는 `cadcraft_execute`(JSON 인자, 프롬프트 없음)로 — 예 `line {points:[[0,0],[10,0]]}`,
  `layer.new {name:"Walls", current:true}`. 명령 이름과 인자는 `cadcraft_list_commands`(filter)로 찾는다.
- 사람 손처럼 이어 그리는 것은 `cadcraft_command_line`(한 줄, `rectang 0,0 @6,4`) 또는 `cadcraft_script`(여러 줄).
- 레이어를 나눈다(벽·문창·가구·치수·글자). 방 이름 글자 높이 0.3, 문·창 표시 0.2, 치수선은 벽에서 0.8 m 바깥.
- 사용자가 준 크기·개수·배치 조건을 빠짐없이 반영하고, 정하지 않은 값은 일반적인 값으로 정한 뒤 답에 적는다.

## 3. 확인

`cadcraft_render` 로 직접 본다(큰 도면은 `window` 로 부분). 글자·치수가 겹치거나 도면 밖으로 나가면 고치고 다시 본다.
`cadcraft_inspect_drawing`·`cadcraft_find_text` 로 레이어·글자가 빠짐없는지 확인한다.

## 4. 저장

`cadcraft_save` 로 **이 대화 전용 산출물 폴더**(메시지의 파일 위치 규칙이 알려 준 base, `/workspace/.bpmn/<대화>/`) 아래에
`.dxf` 로 저장하고 답에 그 경로를 적는다. 확장자로 형식이 정해진다 — `.dwg` 도 되고, `.pdf`·`.png`·`.svg` 경로는 내보내기다.
영어로 답하는 대화면 방 이름·글자와 파일 이름도 영어로 쓴다.

## 5. 기존 도면 열기·열린 도면 고치기

- 파일로 받은 DXF 는 `cadcraft_open`(`/workspace/…` 경로)으로 열고 `cadcraft_inspect_drawing`·`cadcraft_query_entities`·
  `cadcraft_find_text` 로 읽은 뒤 고친다.
- **사용자가 편집기에 열어 둔 도면**은 도구가 그 화면에 바로 적용된다. 새 도면을 만들거나 다른 파일을 열지 않고,
  저장도 하지 않는다(사용자가 한다). 순서는 `references/live-edit.md`.
- 다 고치면 무엇을 바꿨는지 짧게 답한다.
