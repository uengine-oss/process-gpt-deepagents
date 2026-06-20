## 1. 백엔드: file_artifact emit
- [x] 1.1 `executor.py`: write_file/edit_file tool_call 추적 → tool_end 에서 샌드박스 파일 읽어 `file_artifact` SSE emit.

## 2. 프론트: 서비스 콜백
- [x] 2.1 `DeepAgentRouterService.js` / `FixedBaseWorkAssistantAgentService.js`: `file_artifact` case → `onFileArtifact`.

## 3. 프론트: 파일 UI
- [x] 3.1 `WorkspaceFilesViewer.vue` 신규(작업 폴더 목록 + .md/.json/.html 미리보기).
- [x] 3.2 `ArtifactPanel.vue`: `type:'files'` 렌더 + 아이콘 등록.

## 4. 프론트: 배선 & 전환
- [x] 4.1 `ChatRoomPage.vue` `onFileArtifact`: 파일 수집 → `pushArtifactPanel({type:'files'})`.
- [x] 4.2 deepagent `onProcessResult` 의 pdf2bpmn 카드 표시 경로 제거(완전 대체).
- [x] 4.3 `workspaceFiles` 영속(carry/persist) + 재진입 복원(checkExistingArtifactPanels).

## 5. 검증
- [x] 5.1 라이브 테스트(실서버, 자동 OK HITL): 우측 '산출물 파일' 탭에 작업 폴더(process-definition.json 등) + JSON 미리보기 표시, 새로고침 복원까지 PASS(`playwright/e2e/bpmn-artifact/live-deepagent-vacation.spec.ts`, EXIT 0).
- [x] 5.2 `openspec validate deepagent-file-artifacts` 통과.
