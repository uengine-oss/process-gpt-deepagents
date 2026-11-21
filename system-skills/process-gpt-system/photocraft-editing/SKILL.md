---
name: photocraft-editing
description: >
  사진·이미지 파일을 고칠 때 쓴다 — 밝기·색 보정, 자르기·크기 조정, 배경 바꾸기·합성, 글자 얹기,
  레이어 PSD 저장, 그림 색을 문서 색조에 맞추기 — 사용자가 PhotoCraft 편집기에 열어 둔 이미지를 고칠 때도.
  열기·편집·미리 보기·저장 순서를 정한다. 벡터 로고 디자인, 도면, 영상, 사진 속 글자 읽기·설명에는 쓰지 않는다.
metadata:
  version: 1.0.0
  include_tools: >-
    photocraft_start photocraft_command_batch photocraft_command_list photocraft_command_run
    photocraft_doc_close photocraft_doc_export photocraft_doc_export_data_sets photocraft_doc_export_layers
    photocraft_doc_import_data_sets photocraft_doc_inspect photocraft_doc_new photocraft_doc_open
    photocraft_doc_place photocraft_doc_render_preview photocraft_doc_save photocraft_doc_select
    photocraft_render_preview
---

# PhotoCraft 로 이미지 편집하기

**이 도구들(`photocraft_*`)이 목록에 없으면 이 스킬을 쓰지 않는다.** 스킬 전용 도구로 지정된 회사에서는 이 SKILL.md 를
읽은 다음 호출부터 도구가 보이니 그때 한 번 더 본다. `photocraft_start` 하나만 있으면 그것을 불러 켜고 「다음 메시지부터
쓸 수 있다」고 짧게 알린다. 그래도 없으면 PhotoCraft 가 없는 환경이다 — PhotoCraft 로 고쳤다고 말하지 않는다.

사용자가 PhotoCraft 편집기에 이미지를 열어 둔 상태(메시지의 시스템 사실)면 `references/live-edit.md` 를 따른다.

## 1. 열기

- 첨부 이미지는 `photocraft_doc_open`(`/workspace/…` 경로)으로 연다. 새 그림판은 `photocraft_doc_new`(기본 1920×1080 흰 바탕).
- 다른 이미지를 얹을 때는 `photocraft_doc_place`(스마트 오브젝트 레이어).
- 로고처럼 벡터로 새로 디자인할 것은 이 앱이 아니라 `vectorcraft-graphics` 다.

## 2. 편집

- 명령은 `photocraft_command_run`(id·params), 여러 단계는 `photocraft_command_batch`. id 와 인자는 `photocraft_command_list`.
- 레이어를 나눠 원본을 덮어쓰지 않는다(보정은 새 레이어·조정 레이어로).
- 글자를 얹을 때 글꼴은 **NanumGothic** — 웹 편집기에도 있는 글꼴이다.
- 선 그림·도면처럼 바탕 위에 선이 그려진 그림의 색을 바꿀 때는 밝기 기준 변환을 쓰지 않는다 — `references/recolor-line-art.md`.

## 3. 확인과 저장

- `photocraft_doc_render_preview` 로 직접 보고 고친다.
- `photocraft_doc_save` 로 **이 대화 전용 산출물 폴더**(메시지의 파일 위치 규칙의 base, `/workspace/.bpmn/<대화>/`) 아래
  `.psd`(레이어 유지)로 저장하고, PNG·JPG 가 필요하면 같은 폴더에 함께 낸다. 답에 경로를 적는다.
- 결과에 오는 `warnings`(그 형식이 담지 못한 것)가 있으면 답에 옮긴다.
