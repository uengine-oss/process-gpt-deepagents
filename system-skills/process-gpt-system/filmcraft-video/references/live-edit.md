# FilmCraft 편집기에 열린 프로젝트 고치기

메시지의 시스템 사실이 「사용자가 지금 FilmCraft 에서 '<파일>' 을 열어 두었다」고 알려 주면 `filmcraft_` 도구는 그 화면에 바로 적용된다.

1. 확인: `filmcraft_project_inspect`·`filmcraft_sequence_inspect`.
2. 고치기: `filmcraft_command_run`·`filmcraft_command_batch`(명령은 `filmcraft_command_list`). 자막 글꼴은 NanumGothic.
3. `filmcraft_render_frame` 으로 바뀐 장면을 본다.

하지 않는 것: `file.newProject`·소재 새로 가져오기(열린 프로젝트가 바뀐다), `file.exportMedia`·저장(사용자가 한다).
끝나면 바꾼 것을 짧게 답한다.
