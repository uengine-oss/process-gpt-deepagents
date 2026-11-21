---
name: soundcraft-audio
description: >
  오디오를 편집해 파일로 낼 때 쓴다 — 음원을 트랙에 놓고 자르기·이어 붙이기, 볼륨·팬·페이드, 여러 소리
  믹스다운, 효과음·알림음·짧은 배경음 만들기, WAV 내보내기 — 사용자가 SoundCraft 편집기에 열어 둔 세션을
  고칠 때도. 받아쓰기·음성 요약, 글 읽어 주기(TTS), 음악 추천, 영상에 음악 넣기에는 쓰지 않는다.
metadata:
  version: 1.0.0
  include_tools: >-
    soundcraft_start soundcraft_list_commands soundcraft_execute soundcraft_batch soundcraft_inspect_session
    soundcraft_new_session soundcraft_open_session soundcraft_save_session soundcraft_import_audio soundcraft_bounce_mix
---

# SoundCraft 로 오디오 편집하기

**이 도구들(`soundcraft_*`)이 목록에 없으면 이 스킬을 쓰지 않는다.** 스킬 전용 도구로 지정된 회사에서는 이 SKILL.md 를
읽은 다음 호출부터 도구가 보이니 그때 한 번 더 본다. `soundcraft_start` 하나만 있으면 그것을 불러 켜고 「다음 메시지부터
쓸 수 있다」고 짧게 알린다. 그래도 없으면 SoundCraft 가 없는 환경이다 — SoundCraft 로 만들었다고 말하지 않는다.

사용자가 SoundCraft 편집기에 세션을 열어 둔 상태(메시지의 시스템 사실)면 `references/live-edit.md` 를 따른다.

## 1. 세션과 소재

- 새 세션: `soundcraft_new_session`(`name`, `sample_rate`, `template`: blank·demo).
- 소재: `soundcraft_import_audio`(`/workspace/…` 경로, WAV·AIFF·FLAC·MP3·OGG·M4A …, `track`·`at` 로 위치).
- 짧은 효과음·알림음·멜로디처럼 소재 파일이 없는 소리는 MIDI 클립에 음표를 넣고 악기를 골라 만든다
  (`soundcraft_list_commands` 의 `midi.*`, `mix.instrument` 계열 — 인자는 목록의 params 설명).

## 2. 편집

- 명령은 `soundcraft_execute`(command·params), 여러 개는 `soundcraft_batch`. 명령 id 와 인자는 `soundcraft_list_commands`
  (예: `mix.volume`, `mix.pan`, `track.new`, `track.rename`, `edit.fades_create`, `edit.trim_clip`, `edit.insert_silence`).
- 지금 상태는 `soundcraft_inspect_session`(트랙·클립·버스·마커)으로 확인한다.
- 길이·음량 같은 요구가 있으면 지킨다. 정하지 않은 값은 일반적인 값으로 정하고 답에 적는다.

## 3. 내보내기

`soundcraft_bounce_mix` 로 **이 대화 전용 산출물 폴더**(메시지의 파일 위치 규칙의 base, `/workspace/.bpmn/<대화>/`) 아래
`.wav` 로 내보내고 답에 경로를 적는다. 결과의 최대 음량(dBFS)·LUFS 를 함께 적는다. 사용자가 이어서 고칠 수 있게
`soundcraft_save_session` 으로 세션 파일(.scraft)도 같은 폴더에 남긴다.
