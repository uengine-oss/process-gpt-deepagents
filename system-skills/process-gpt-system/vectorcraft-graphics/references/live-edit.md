# VectorCraft 편집기에 열린 그림 고치기

메시지의 시스템 사실이 「사용자가 지금 VectorCraft 에서 '<파일>' 을 열어 두었다」고 알려 주면 `vectorcraft_` 도구는 그 화면에 바로 적용된다.

1. 확인: `vectorcraft_doc_inspect`(대지·레이어 트리·선택, 큰 문서는 `depth`·`ids` 로 좁힘) 또는 `vectorcraft_inspect_document`.
2. 고치기: 글자는 `vectorcraft_add_text`(새 글자) 또는 `vectorcraft_command_run`(있는 글자의 내용·글꼴), 색은 `vectorcraft_set_paint`
   (`ids` 로 대상), 위치는 `vectorcraft_transform`.
3. `vectorcraft_render_preview` 로 본다.

하지 않는 것: `vectorcraft_open_file`(다른 문서가 열린다), `vectorcraft_save_file`·`vectorcraft_export`(저장은 사용자가 한다).
끝나면 바꾼 것을 짧게 답한다.
