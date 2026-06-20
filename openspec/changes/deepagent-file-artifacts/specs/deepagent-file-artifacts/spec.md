## ADDED Requirements

### Requirement: 샌드박스 산출물 파일 실시간 전달
백엔드는 에이전트가 write_file/edit_file 로 /workspace 하위 파일을 만들거나 수정하면 그 파일을 읽어 file_artifact SSE 이벤트로 전달 MUST 한다.

#### Scenario: 파일 생성 시 이벤트 전송
- **WHEN** 에이전트가 `write_file` 로 `/workspace/.bpmn/process-definition.json` 을 생성하면
- **THEN** 백엔드는 `{type:"file_artifact", op:"create", path, name, ext:".json", content}` 를 emit 한다.

#### Scenario: 파일 수정 시 이벤트 전송
- **WHEN** 에이전트가 `edit_file` 로 기존 파일을 수정하면
- **THEN** 백엔드는 수정 후 파일 내용을 읽어 `op:"edit"` 로 emit 한다.

### Requirement: Claude Desktop식 파일 UI 표시
프론트는 file_artifact 들을 모아 ArtifactPanel 의 'files' 패널(작업 폴더 목록 + 미리보기)로 표시 MUST 한다.

#### Scenario: 파일 목록 + 미리보기
- **WHEN** file_artifact 가 도착하면
- **THEN** 우측 패널에 파일 목록(이름/생성·수정 배지)을 보여주고, 선택 파일을 .md=마크다운 렌더,
  .json=코드, .html=렌더/소스 토글로 미리본다.

#### Scenario: pdf2bpmn 카드 미사용
- **WHEN** deepagent 가 프로세스 산출물을 만들면
- **THEN** 더 이상 pdf2bpmn 전용 카드(생성된 프로세스/스킬/미리보기)로 표시하지 않고 파일 UI 로만 표시한다.

#### Scenario: 재진입 시 복원
- **WHEN** 산출물 파일이 영속된 채팅방에 새로고침/재진입하면
- **THEN** 영속된 `workspaceFiles` 에서 'files' 패널을 복원한다.
