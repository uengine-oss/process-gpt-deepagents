## Why

deepagent 가 (bpmn-process-generation 등) 어떤 스킬을 쓰든 샌드박스 workspace 에 만든 산출물 파일
(process-definition.json, skills/*/SKILL.md, forms/*.html, agents.json …)을 사용자가 **Claude Desktop 과
동일한 방식** — 진행상황(Todo) + '작업 폴더' 파일 목록 + 파일 미리보기 — 으로 확인할 수 있어야 한다.

기존에는 스킬 결과를 pdf2bpmn 전용 카드(생성된 프로세스/생성된 스킬/BPMN 미리보기)로 매핑해 표시했는데,
이는 (a) 특정 스킬(pdf2bpmn)에 종속적이고 (b) 실제 생성 파일을 그대로 보여주지 못한다. 산출물 파일을
**파일 그대로** 표시하는 범용 UI 로 전환한다.

## What Changes

- **백엔드(executor)**: 에이전트의 `write_file`/`edit_file` 도구가 `/workspace` 하위 파일을 만들/수정할 때마다
  해당 파일을 샌드박스에서 읽어 `{type:"file_artifact", op, path, name, ext, content}` SSE 이벤트로 실시간 전송.
- **프론트(서비스)**: `file_artifact` 이벤트 → `onFileArtifact` 콜백 추가(DeepAgentRouter/FixedBase 공통).
- **프론트(UI)**: `WorkspaceFilesViewer` 신규 — Claude Desktop식 '작업 폴더' 파일 목록 + 미리보기
  (.md 마크다운 렌더, .json 코드, .html 렌더/소스 토글). ArtifactPanel `type:'files'` 로 연결.
- **프론트(전환)**: deepagent 의 `process_result → pdf2bpmnResult → ProcessArtifactViewer` 표시 경로 제거
  (PDF→BPMN 업로드 기능의 process 카드는 그대로 유지). 진행상황(Todo)/도구/활동 패널은 기존대로 사용.
- **영속/복원**: 메시지에 `workspaceFiles` 영속 → 새로고침/재진입 시 'files' 패널 복원.

## Capabilities

### New Capabilities
- `deepagent-file-artifacts`: deepagent 샌드박스 산출물 파일을 실시간으로 프론트에 전달하고 Claude Desktop식
  파일 목록 + 미리보기 UI 로 표시한다(스킬 종류 무관).

## Impact

- **백엔드(process-gpt-deepagents)**: `executor.py`(file_artifact emit).
- **프론트(process-gpt-vue3)**: `src/services/DeepAgentRouterService.js`, `src/services/FixedBaseWorkAssistantAgentService.js`,
  `src/components/WorkspaceFilesViewer.vue`(신규), `src/components/ArtifactPanel.vue`, `src/views/chat/ChatRoomPage.vue`.
- pdf2bpmn 업로드 기능은 영향 없음(별도 경로).
