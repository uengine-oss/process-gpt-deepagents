## ADDED Requirements

### Requirement: 생성 산출물 ArtifactPanel 표시
프론트는 deepagent 가 전달한 BPMN 산출물(process-definition + forms + skills + agents)을 ArtifactPanel(우측 산출물 사이드바)에 표시해야 한다(MUST).

#### Scenario: 산출물 수신 시 패널 표시
- **WHEN** `process_result` 이벤트로 산출물이 도착하면
- **THEN** ArtifactPanel 에 `type: 'process'` 패널을 추가해 프로세스 미리보기 + 스킬/에이전트 목록을 보여준다.

#### Scenario: 저장 전 미리보기
- **WHEN** 사용자가 패널의 프로세스를 미리볼 때
- **THEN** 저장 전이면 계약 정의를 createBpmnXml 로 변환해 미리보기를 표시한다.

#### Scenario: 채팅방 재진입 시 산출물 패널 복원
- **WHEN** 산출물(`pdf2bpmnResult.__contract`)이 메시지에 영속된 채팅방에 사용자가 새로고침/재진입하면
- **THEN** 프론트는 메시지의 영속된 산출물에서 `type: 'process'` 패널을 자동 복원해 사용자가 다시 열어볼 수 있게 한다(슬라이드/HWPX 복원과 동일 동작).
- **AND** 같은 `processDefinitionId` 의 proc_def 가 이미 저장돼 있으면 패널을 '저장됨' 으로 표시한다.

### Requirement: 사용자 저장 확정
저장은 사용자가 패널의 '저장' 버튼을 눌렀을 때만 수행되어야 하며(MUST), 그때 프론트가 proc_def/form_def 를 저장한다.

#### Scenario: 저장 버튼
- **WHEN** 사용자가 ArtifactPanel 에서 '저장' 을 누르면
- **THEN** 프론트가 elements→flattened 변환 후 proc_def·form_def 를 저장하고 '저장됨' 으로 표시한다.

#### Scenario: 저장 전 비저장
- **WHEN** 사용자가 저장을 누르기 전이면
- **THEN** DB 에 아무것도 저장되지 않는다.
