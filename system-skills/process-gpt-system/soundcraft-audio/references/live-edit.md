# SoundCraft 편집기에 열린 세션 고치기

메시지의 시스템 사실이 「사용자가 지금 SoundCraft 에서 '<파일>' 을 열어 두었다」고 알려 주면 `soundcraft_` 도구는 그 화면에 바로 적용된다.

1. 확인: `soundcraft_inspect_session`(트랙·클립·볼륨·팬).
2. 고치기: `soundcraft_execute`·`soundcraft_batch` — `soundcraft_list_commands` 의 명령(`mix.volume`, `mix.pan`, `track.rename`, `track.new` 등).
3. 다시 `soundcraft_inspect_session` 으로 바뀐 값을 확인한다.

하지 않는 것: `soundcraft_new_session`·`soundcraft_open_session`(열린 세션이 바뀐다), 저장·내보내기(사용자가 한다).
끝나면 바꾼 것을 짧게 답한다.
