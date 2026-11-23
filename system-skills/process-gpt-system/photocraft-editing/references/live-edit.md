# PhotoCraft 편집기에 열린 이미지 고치기

메시지의 시스템 사실이 「사용자가 지금 PhotoCraft 에서 '<파일>' 을 열어 두었다」고 알려 주면 `photocraft_` 도구는 그 화면에 바로 적용된다.

1. 확인: `photocraft_doc_inspect`(레이어 트리·선택·활성 레이어).
2. 고치기: `photocraft_command_run`·`photocraft_command_batch`(예: 글자 레이어의 색·내용 바꾸기, 새 글자 레이어, 보정).
   선 그림의 재칠은 `recolor-line-art.md`.
3. `photocraft_doc_render_preview` 로 본다(이때는 앱 창 화면이 온다).

하지 않는 것: `photocraft_doc_new`·`photocraft_doc_open`(다른 문서가 열린다), `photocraft_doc_save`·`photocraft_doc_export`
(저장은 사용자가 한다). 끝나면 바꾼 것을 짧게 답한다.
