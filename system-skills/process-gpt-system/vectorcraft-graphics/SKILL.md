---
name: vectorcraft-graphics
description: >
  로고·아이콘·배너·포스터·명함·간단한 일러스트처럼 편집 가능한 벡터 그래픽(SVG·AI·PDF)을 만들거나 고칠 때,
  사용자가 VectorCraft 편집기에 열어 둔 그림을 고칠 때 쓴다. 대지·도형·글자·미리 보기·저장 순서와 글꼴을 정한다.
  사진 보정·합성, 치수 도면, 슬라이드 파일, 코드로 만드는 생성 아트에는 쓰지 않는다.
metadata:
  version: 1.0.0
  include_tools: >-
    vectorcraft_start vectorcraft_command_list vectorcraft_command_run vectorcraft_command_batch
    vectorcraft_doc_inspect vectorcraft_render_preview vectorcraft_list_commands vectorcraft_run_command
    vectorcraft_inspect_document vectorcraft_inspect_ui vectorcraft_draw_path vectorcraft_draw_shape
    vectorcraft_set_paint vectorcraft_open_file vectorcraft_save_file vectorcraft_export vectorcraft_add_text
    vectorcraft_apply_effect vectorcraft_pathfinder vectorcraft_transform vectorcraft_create_graph
    vectorcraft_text_wrap vectorcraft_undo vectorcraft_redo
---

# VectorCraft 로 벡터 그래픽 만들기·고치기

**이 도구들(`vectorcraft_*`)이 목록에 없으면 이 스킬을 쓰지 않는다.** 스킬 전용 도구로 지정된 회사에서는 이 SKILL.md 를
읽은 다음 호출부터 도구가 보이니 그때 한 번 더 본다. `vectorcraft_start` 하나만 있으면 그것을 불러 켜고 「다음 메시지부터
쓸 수 있다」고 짧게 알린다. 그래도 없으면 VectorCraft 가 없는 환경이다 — VectorCraft 로 만들었다고 말하지 않는다.

사용자가 VectorCraft 편집기에 그림을 열어 둔 상태(메시지의 시스템 사실)면 `references/live-edit.md` 를 따른다.

## 1. 대지

용도에 맞는 크기로 대지부터 맞춘다(필요하면 `vectorcraft_command_run` — 명령 id 는 `vectorcraft_command_list`).
요청에 크기가 없으면 흔한 크기(정사각 로고, 가로 배너 등)로 정하고 답에 적는다.

## 2. 도형·글자·색

- 도형: `vectorcraft_draw_shape`(사각형·원·다각형·별·선), 자유 경로는 `vectorcraft_draw_path`. 여러 단계는 `vectorcraft_command_batch`.
- 글자: `vectorcraft_add_text`. 글꼴은 **NanumGothic** — 웹 편집기에도 있는 글꼴이라 사람이 열어도 같게 보인다.
  결과에 `overflow: true` 가 오면 틀을 키우거나 글자를 줄인다.
- 색·선: `vectorcraft_set_paint`(`#rrggbb`, `"none"`). 테두리 없는 채운 도형은 `stroke:"none"` 을 꼭 준다.
- 배치: `vectorcraft_transform`, 합치기·빼기는 `vectorcraft_pathfinder`.

## 3. 확인과 저장

- `vectorcraft_render_preview` 로 직접 보고, 글자 잘림·겹침·여백을 고친다.
- `vectorcraft_save_file` 로 **이 대화 전용 산출물 폴더**(메시지의 파일 위치 규칙의 base, `/workspace/.bpmn/<대화>/`) 아래
  `.svg` 로 저장하고 답에 경로를 적는다. PNG·PDF 가 필요하면 `vectorcraft_export` 로 같은 폴더에 함께 낸다.
- 영어로 답하는 대화면 그림 속 글자·파일 이름도 영어로.
