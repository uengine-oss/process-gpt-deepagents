# 09 – 실행 모델: 5단계 상세 · 자체 점검 체크리스트 · 역할 분담

이 skill 은 **항상 동일한 단일 5단계**로 동작한다(대화형/서비스 모드 구분 없음).
모든 산출물은 대화 컨텍스트가 아니라 **실제 파일**로 만든다. 경로·파일명 규격은
[12-deepagents-execution.md](12-deepagents-execution.md) 에 있다.

> 🔁 **단일 세션 실행(필수)**: 한 연속 실행으로 5단계를 끝까지 간다. 사용자 확인은
> 오직 `request_human_input` 으로만 한다(프로즈로 응답을 끝내지 말 것 — 턴이 끊겨
> 순서가 꼬인다). 멈춤은 **정확히 2곳**: 1단계 컨설팅 승인, 2단계 후보 선택.
> 그 사이/이후는 멈추지 말고 자동 진행한다.

---

## 5단계

### 1. 컨설팅 & 프로세스 JSON 생성
- 문서가 업로드됐으면 `search_documents` 로 **먼저 읽어** as-is 흐름을 파악한다
  ([10-document-intake.md](10-document-intake.md)).
- 컨설팅 초안을 `request_human_input`(`question`=승인 한 줄, `context`=초안 전체)으로
  제시·승인받는다([01-consulting.md](01-consulting.md)).
- 승인 흐름을 [02-generate-definition.md](02-generate-definition.md) 규격의
  **elements[] JSON**으로 만들어 `write_file` 로 `process-definition.json` 에 쓴다.
  **반드시 실제 elements(StartEvent·EndEvent·UserActivity·Sequence 등)를 채운다**
  (placeholder/빈 elements 금지). 흐름 연결은 **Sequence 요소(source/target)** 로 표현한다.

### 2. 스킬·에이전트·DMN 후보 선택 & 생성
- elements 에서 구체 후보를 도출해([03-elicit-artifacts.md](03-elicit-artifacts.md))
  `request_human_input` 으로 묻는다.
- 선택분만:
  - **스킬**: `task(subagent_type="skill-creator")` 로 `skills/<safe-name>/SKILL.md`
    생성([04-skills.md](04-skills.md)).
  - **에이전트**: `agents/<agent_id>.json` 개별 파일([05-agents.md](05-agents.md)).
  - **DMN**: `dmn_decisions`/`dmn_rules` 를 process-definition.json 안에([06-dmn.md](06-dmn.md)).
- `manifest.json` 을 반드시 쓴다(선택 결과 ↔ activity/gateway 매핑).
- `edit_file` 로 `activity.skills`/`activity.agent`/`agentMode`/`orchestration` 반영.

### 3. 폼 · 참조정보 (자동, 질문 없음)
- 각 UserActivity 폼을 `forms/<activity_id>.form` 으로 만든다([07-forms.md](07-forms.md)).
- 참조정보(`activity.inputData`, gateway `conditionData`)를 반영([08-reference-info.md](08-reference-info.md)).

### 4. 자체 점검 & 보정
- 전용 검증 도구는 없다. `process-definition.json` 을 `read_file` 로 다시 읽고 아래
  체크리스트를 하나씩 직접 대조한다.
- 결함을 발견하면 필드 수정은 `edit_file`, 구조 수정(요소 추가/삭제)은 `write_file` 로
  전체 객체를 다시 쓴 뒤 체크리스트를 재확인한다.
- **점검 과정·결함 목록을 채팅에 설명하지 않는다.** 조용히 고치고 넘어간다.

#### 체크리스트

| 항목 | 통과 조건 |
|---|---|
| 최상위 | `processDefinitionName`(비어있지 않음), `processDefinitionId`, `elements[]` 존재. `activities`/`events` 분리 배열 **없음** |
| 요소 타입 | 모든 요소가 `elementType` ∈ {Event, Activity, Gateway, Sequence} |
| 이벤트 | StartEvent 1개 이상, EndEvent 1개 이상 |
| 액티비티 | `type:"UserActivity"`, `role` 존재, `outputData` 1개 이상, `id` 는 영문 소문자+언더스코어 |
| 금지 키 | `assignee`·`formKey`·`candidateGroups`·`sequenceFlows` 없음 |
| 연결 | 모든 Sequence 가 `source`/`target` 을 갖고 그 값이 실제 존재하는 요소 id |
| 고아 노드 | EndEvent 제외 모든 노드에 outgoing, StartEvent 제외 모든 노드에 incoming |
| 도달성 | StartEvent 에서 모든 노드에 도달 가능하고, 모든 경로가 EndEvent 로 끝남 |
| 분기 | ExclusiveGateway 의 각 outgoing Sequence 에 `condition` 이 있고, 직전 UserActivity 폼에 그 값과 정확히 일치하는 선택 필드가 있으며 `conditionData` 가 `<form_id>.<field_name>` 문자열 배열 |
| 폼 | 모든 UserActivity 에 `forms/<activity_id>.form` 이 있고 `tool`=`formHandler:<activity_id>` |
| 선택 반영 | 사용자가 고른 스킬/에이전트/DMN 이 전부 파일로 존재하고 `manifest.json` 에 매핑됨 |

### 5. 완료
- 산출물을 다 만들었으면 **더 이상 도구를 호출하지 말고 턴을 끝낸다.**
- 시스템이 정의를 최종 검증·보정하고, 폼/스킬/에이전트/DMN 을 통합해 프론트 산출물
  패널로 전달한다. 작업 파일은 **보존**한다.
- 채팅엔 **"프로세스를 생성했어요. 확인 후 저장 버튼을 눌러주세요."** 한 줄만 남긴다.

---

## 역할 분담 (중요)

| 주체 | 하는 일 | 하지 않는 일 |
|---|---|---|
| **이 스킬(에이전트)** | 산출물을 파일로 생성, 자체 점검 | DB 쓰기, 저장, 업로드, 파일 삭제 |
| **시스템(턴 종료 후)** | 스키마·흐름 검증 + 자동 보정, 폼/스킬/에이전트/DMN 통합, 프론트 전달 | 사용자 대신 저장 |
| **프론트** | 임시저장(draft) → 실행엔진 `/validate-and-improve` 검증 → 결과 표시 | — |
| **사용자** | 결과 확인 후 '저장' 버튼으로 최종 확정 | — |

실제 실행엔진 검증은 **프론트가 임시저장(draft) 후 `/validate-and-improve`** 로
수행한다. 이 스킬과 시스템 마무리 단계는 **파일 기준 정적 검증**까지만 한다.

## 최종 통합본 형태 (참고)

시스템이 만들어 프론트로 보내는 형태다. 에이전트가 직접 만들 필요는 없다.

```json
{ "type": "process-definition-result",
  "processDefinition": { "processDefinitionId": "...", "processDefinitionName": "...",
                         "elements": [ ... ], "roles": [ ... ],
                         "dmn_decisions": [...], "dmn_rules": [...] },
  "forms": [ { "activity_id": "...", "form_id": "...", "html": "<section>...</section>" } ],
  "agents": [ { "id": "...", "name": "...", "role": "..." } ],
  "skills": ["..."] }
```
