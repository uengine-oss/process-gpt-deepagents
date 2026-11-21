---
name: filmcraft-video
description: >
  영상 파일을 편집해 낼 때 쓴다 — 클립 이어 붙이기·자르기, 자막·글자, 효과·전환, 배경음 깔기, MP4 내보내기,
  데모 풍경 소재로 짧은 영상 만들기 — 사용자가 FilmCraft 편집기에 열어 둔 프로젝트를 고칠 때도.
  유튜브 영상 찾기, 영상 내용 요약·받아쓰기, 대본 쓰기, 썸네일 이미지에는 쓰지 않는다.
metadata:
  version: 1.0.0
  include_tools: >-
    filmcraft_start filmcraft_command_batch filmcraft_command_list filmcraft_command_run filmcraft_doc_inspect
    filmcraft_media_import filmcraft_project_inspect filmcraft_render_frame filmcraft_render_preview
    filmcraft_sequence_inspect
---

# FilmCraft 로 영상 편집하기

**이 도구들(`filmcraft_*`)이 목록에 없으면 이 스킬을 쓰지 않는다.** 스킬 전용 도구로 지정된 회사에서는 이 SKILL.md 를
읽은 다음 호출부터 도구가 보이니 그때 한 번 더 본다. `filmcraft_start` 하나만 있으면 그것을 불러 켜고 「다음 메시지부터
쓸 수 있다」고 짧게 알린다. 그래도 없으면 FilmCraft 가 없는 환경이다 — FilmCraft 로 만들었다고 말하지 않는다.

사용자가 FilmCraft 편집기에 프로젝트를 열어 둔 상태(메시지의 시스템 사실)면 `references/live-edit.md` 를 따른다.

## 1. 프로젝트와 소재

1. `filmcraft_command_run` `file.newProject`.
2. 소재 가져오기 — 사용자 파일은 `/workspace/…` 경로로 `filmcraft_media_import`. 소재가 없고 풍경 영상이면 되는 요청은
   데모 소재(`references/demo-footage.md`).
3. `file.newSequenceFromClip` 으로 시퀀스를 만든다(1920×1080 데모 클립이면 그대로 꽉 찬다).

## 2. 편집

- 자르기·이어 붙이기·자막·효과·전환·음악은 `filmcraft_command_list`(filter)로 찾은 명령을 `filmcraft_command_run`
  (예 `timeline.razor {seconds: 3.5}`, `effects.apply {effect: "Gaussian Blur"}`)이나 `filmcraft_command_batch` 로.
- 자막 글꼴은 **NanumGothic** — 웹 편집기에도 있는 글꼴이다.
- 지금 상태는 `filmcraft_sequence_inspect`(트랙·클립·길이, 254016000000 틱 = 1초)와 `filmcraft_project_inspect`.

## 3. 확인과 내보내기

- `filmcraft_render_frame`(`seconds`)으로 장면 몇 곳을 직접 보고 고친다.
- `filmcraft_command_run` `file.exportMedia {path: "<base>/<이름>.mp4", format: "h264"}` 로 내보낸다.
  `<base>` = 이 대화 전용 산출물 폴더(메시지의 파일 위치 규칙의 base, `/workspace/.bpmn/<대화>/`). 답에 경로를 적는다.
- 영어로 답하는 대화면 자막·파일 이름도 영어로.
