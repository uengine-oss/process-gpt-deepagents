## ADDED Requirements

### Requirement: 요청 형태에 무관한 일관 생성
deepagent 는 단일/다중 문서 업로드, 단일/다중 자연어 프로세스 요청 등 어떤 형태의 요청에도 동일한 단계 흐름으로 BPMN 프로세스를 생성해야 한다(MUST).

#### Scenario: 단일 자연어 요청
- **WHEN** 사용자가 "휴가 신청 프로세스 만들어줘" 라고 요청하면
- **THEN** 컨설팅→JSON 생성→후보선택→폼·참조→검증→완료 단계로 1개 프로세스를 생성한다.

#### Scenario: 다중 프로세스(문서/자연어)
- **WHEN** 한 문서/요청에 독립 프로세스가 2개 이상 포함되면
- **THEN** 각 프로세스마다 동일한 단계 흐름을 수행하고 산출물을 프로세스별로 분리한다.

#### Scenario: 문서 업로드
- **WHEN** 문서가 업로드되면
- **THEN** 에이전트는 `/workspace/uploads/<파일>` 를 직접 읽어 흐름을 추출해 컨설팅으로 잇는다.

### Requirement: 모드 분기 제거
스킬은 초심자/숙련자 모드, 대화형/서비스 모드 등 어떤 분기도 두지 않고 항상 동일하게 동작해야 한다(MUST).

#### Scenario: 모드 판별 없음
- **WHEN** 스킬이 트리거되면
- **THEN** 모드 판별·초심자 배려 분기 없이 곧바로 단일 5단계 흐름을 시작한다.

### Requirement: 산출물 전용(저장·업로드·삭제 금지)
에이전트는 산출물을 샌드박스 workspace 파일로만 생성하며, DB 저장·스킬 서비스 업로드·workspace 자동 삭제를 수행하지 않는다(MUST NOT).

#### Scenario: 스킬은 workspace 산출물
- **WHEN** 스킬을 생성하면
- **THEN** `/workspace/.bpmn/skills/<name>/SKILL.md` 파일로만 남기고 외부 서비스에 업로드하지 않는다.

#### Scenario: 완료 시 파일 보존
- **WHEN** 완료 단계가 끝나면
- **THEN** workspace 산출물 파일을 삭제하지 않고 보존한 채 결과만 프론트로 전달한다.

#### Scenario: DB 미접근
- **WHEN** 전체 흐름 동안
- **THEN** 에이전트는 어떤 DB 쓰기도 하지 않는다(읽기 전용).

### Requirement: 생성 정의 검증
완료 전에 프로세스 정의의 구조 결함을 검사하고 자동 개선해야 한다(MUST).

#### Scenario: 검증 + 자동개선
- **WHEN** 검증 단계에서 흐름 결함(끊김·도달불가 등)이 발견되면
- **THEN** 최대 5회 자동 개선 후 재검증하고, 결과(passed/결함)를 보고한다.

### Requirement: HITL 은 재실행이 아니라 graph resume
컨설팅 승인·후보 선택 등 모든 HITL 응답은 **동일한 LangGraph 실행을 `Command(resume=...)` 로 재개**해야 하며(MUST), 결코 처음부터 재실행(augmented 메시지로 새 run)해서는 안 된다(MUST NOT). 사용자가 HITL 패널의 버튼을 클릭하든 입력창으로 메시지를 보내든 동일하게 resume 으로 처리한다.

#### Scenario: 컨설팅 승인은 resume
- **WHEN** 사용자가 컨설팅 초안 HITL 에 "승인"으로 응답하면
- **THEN** 그래프는 interrupt 지점부터 resume 되어 곧바로 다음 단계(process-definition.json 생성)로 진행하고, 초안/컨설팅을 다시 수행하지 않는다.

#### Scenario: 재실행 금지(중복 생성 방지)
- **WHEN** 어떤 HITL 응답이든 처리될 때
- **THEN** 같은 프로세스를 두 번 생성하지 않으며(서로 다른 processDefinitionId 의 동일 프로세스 산출물이 2벌 생기지 않음), 한 요청당 한 벌의 산출물만 남는다.

#### Scenario: 안전망은 resume 호환이어야 함
- **WHEN** 모델이 `request_human_input` 을 호출하지 않아 실제 interrupt 가 없을 수 있는 경우
- **THEN** 보완책은 반드시 resume 가능한(interrupt 를 만드는) 방식이어야 하며, 메시지 재실행으로 대체해서는 안 된다.

### Requirement: 한 프로세스 = 한 산출물 그룹(단일 탭)
하나의 프로세스 요청은 정확히 하나의 산출물 폴더(`/workspace/.bpmn/<process>/`)와 하나의 프론트 탭으로 표시되어야 한다(MUST). 동일 프로세스가 여러 폴더/탭으로 분리되어서는 안 된다(MUST NOT).

#### Scenario: 단일 프로세스 단일 탭
- **WHEN** 사용자가 단일 프로세스를 요청해 생성이 끝나면
- **THEN** 산출물 패널에 해당 프로세스 탭이 정확히 1개만 표시된다.

### Requirement: 산출물 일습(프로세스·폼·스킬·에이전트)
완료 시 선택된 항목에 대해 process-definition.json, `forms/<activity_id>.form`, `skills/<name>/SKILL.md`, `agents.json`, `manifest.json` 을 모두 생성하고 process-definition.json 에 반영해야 한다(MUST).

#### Scenario: 에이전트 산출
- **WHEN** 후보 선택에서 에이전트가 선택되면
- **THEN** `/workspace/.bpmn/<process>/agents.json` 에 에이전트 정의를 기록하고 process-definition.json 의 activity.agent 에 반영한다.

#### Scenario: 사용자 저장 시 등록(승격)
- **WHEN** 사용자가 산출물 패널/내부 편집기에서 저장을 클릭하면
- **THEN** 프론트가 프로세스(proc_def)·폼(form_def)·스킬(스킬 서비스 업로드)·에이전트(users)를 저장하고 draft 플래그를 해제(is_draft=false)해 목록에 노출한다.

### Requirement: 선택한 후보는 모두 산출(누락 금지)
후보 선택 HITL 에서 사용자가 고른 **모든** 스킬·에이전트·DMN 은 빠짐없이 실제 산출물로 생성되어야 한다(MUST). 일부만 생성하고 완료해서는 안 된다(MUST NOT).

#### Scenario: 선택 개수 = 생성 개수
- **WHEN** 사용자가 `[스킬] A, [스킬] B, [에이전트] C, [에이전트] D, [DMN] E` 를 선택하면
- **THEN** 스킬 A·B 는 각각 `skills/<name>/SKILL.md`, 에이전트 C·D 는 각각 개별 에이전트 json, DMN E 는 process-definition.json 의 dmn_decisions/dmn_rules 로 **모두** 생성되고, 생성 개수가 선택 개수와 일치한다.

#### Scenario: 누락 시 미완료
- **WHEN** 선택된 후보 중 하나라도 생성에 실패하면
- **THEN** complete_process_generation 으로 완료하지 않고 누락 항목을 재생성하거나 오류를 보고한다.

### Requirement: 에이전트는 에이전트별 개별 산출물
에이전트는 **에이전트마다 개별 json 파일**(`agents/<agent_id>.json` 형식)로 산출해야 한다(MUST). 단일 `agents.json` 에 전부 합쳐 편집 화면 연결이 모호해지는 구조를 지양한다.

#### Scenario: 에이전트별 json + 편집 연결
- **WHEN** 에이전트 2개가 선택·생성되면
- **THEN** `agents/<id1>.json`, `agents/<id2>.json` 두 파일이 생성되고, 산출물 패널에서 각 에이전트 항목의 '편집' 이 해당 에이전트 편집 화면(`/agent-chat/<id>`)으로 연결된다.

### Requirement: 스킬·에이전트도 draft 로 저장되어 편집기에서 내용 로드(bpmn 과 동일)
프로세스(bpmn)가 draft(proc_def.is_draft)로 저장돼 `/definitions/{id}` 편집기에서 내용이 로드되듯, 스킬·에이전트도 검증 단계에서 **draft 로 저장**되어 편집기에서 내용을 바로 확인할 수 있어야 한다(MUST). 목록에는 노출하지 않고(draft), 최종 저장 시 승격한다.

#### Scenario: 스킬 draft — 편집기 로드
- **WHEN** 생성·검증이 끝나 스킬 산출물의 '편집' 으로 `/skills/{name}` 에 이동하면
- **THEN** 스킬 파일이 스킬 서비스에 draft 업로드돼 있어 편집기에서 SKILL.md 내용이 로드된다(목록에는 미노출). 최종 저장 시 목록에 등록(승격)된다.

#### Scenario: 에이전트 draft — 편집기 로드(안정적 id)
- **WHEN** 에이전트 산출물의 '편집' 으로 `/agent-chat/{id}` 에 이동하면
- **THEN** 에이전트가 users 에 draft(is_draft=true)로 저장돼 있고, 파일·편집이동·저장이 모두 **동일한 결정적 uuid**(슬러그 파생)를 써서 방 재진입 후에도 같은 에이전트를 로드한다(랜덤 uuid 로 인한 id 불일치 금지).
