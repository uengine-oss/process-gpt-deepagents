---
name: bpmn-process-generation-skill
description: 업무 프로세스/워크플로우/BPMN 을 만들거나 설계·생성하려는 모든 요청에서 **다른 도구·스킬보다 우선적으로(최우선) 사용해야 하는** 프로세스 생성 전용 스킬. 사용자가 만들고 싶은 업무 프로세스를 컨설팅한 뒤 ProcessGPT 서비스용 BPMN 프로세스 정의(JSON)를 단계별로 생성한다. 사용자가 "프로세스 만들고 싶어", "업무 흐름 자동화", "휴가 신청 프로세스 만들어줘", "결재 프로세스 설계", "BPMN 만들어줘", "워크플로우 만들어줘", "/bpmn", "/bpmn:consult", "/bpmn:generate", "프로세스 정의 생성", "이 업무를 프로세스로 만들고 싶다" 같은 표현을 쓰거나, 어떤 반복 업무·승인 흐름·자동화하고 싶은 절차를 설명하면서 그것을 실행 가능한 프로세스로 만들고 싶어할 때 **반드시 이 스킬을 트리거**하세요(범용 도구로 직접 처리하지 말 것). 또한 사용자가 **업무 규정·매뉴얼·SOP·양식 같은 문서(PDF·docx·xlsx·이미지)를 업로드/첨부하거나 절차 텍스트를 붙여넣으며** "이 문서대로 프로세스 만들어줘", "이 매뉴얼 기준으로", "첨부 보고 흐름 만들어줘" 라고 할 때도 트리거해, 문서 내용에서 흐름을 추출해 생성합니다. BPMN에 익숙하지 않은 사용자도 컨설팅(초안 제안·질문)을 통해 흐름을 함께 다듬고, 스킬·에이전트·DMN 규칙·폼·참조정보까지 단계별로 붙여 완성된 프로세스 정의 JSON을 만들 수 있도록 안내합니다. 선택된 재사용 스킬은 skill-creator 로 정식 스킬로 생성합니다. 프로세스 생성 요청을 받으면 조직도·사용자 정보를 조회하거나 사용자 토큰·테넌트 ID 를 요청하지 말고, **곧바로 컨설팅 초안부터** 제시하세요(역할은 이 스킬 절차가 정합니다).
---

# BPMN Process Generation

## ⛔ 단일 5단계 절차 (항상 동일 — 모드 분기 없음)

이 스킬이 트리거되면 **항상 아래 5단계를 그대로** 수행한다. 산출물은 **대화
컨텍스트가 아니라 실제 파일**로 만든다. 파일 위치는 시스템 프롬프트의
**`📁 이번 대화 전용 산출물 경로`** 섹션이 매 대화마다 정확한 절대경로로 알려준다 —
그 값을 그대로 쓴다(이 문서의 예시 경로를 그대로 베끼지 말 것).

> ✅ **맨 먼저(필수): `write_todos`** 로 아래 **고정 이름 5개**를 그대로 등록하고
> 시작한다(이름 변경·압축 금지, 각 단계 완료 시 갱신):
> `1. 프로세스 초안 설계 & JSON 생성` · `2. 스킬·에이전트·DMN 선택 & 생성` ·
> `3. 입력 폼 & 참조정보 생성` · `4. 프로세스 검증 & 자동 보정` · `5. 결과 통합 & 완료`

| # | 단계 | 핵심 | reference |
|---|------|------|------|
| 1 | **컨설팅 & JSON 생성** | (문서가 있으면 `search_documents` 로 먼저 읽고) 실무 수준 초안을 만들어 `request_human_input` 으로 **승인**받은 뒤, elements[] 규격 정의 전체를 `write_file` 로 `<프로세스폴더>/process-definition.json` 에 생성 | [01](references/01-consulting.md) [02](references/02-generate-definition.md) [10](references/10-document-intake.md) |
| 2 | **후보 선택 & 생성** | elements 에서 스킬/에이전트/DMN **구체 후보**를 `request_human_input` 으로 묻고, 선택분만 생성(스킬은 `task(subagent_type="skill-creator")`), 매핑을 `manifest.json` 에 기록, 정의는 `edit_file` 로 반영 | [03](references/03-elicit-artifacts.md) [04](references/04-skills.md) [05](references/05-agents.md) [06](references/06-dmn.md) |
| 3 | **폼 · 참조정보** | 각 UserActivity 폼을 `forms/<activity_id>.form` 으로 만들고 `inputData`/`conditionData` 를 반영. ExclusiveGateway 직전 폼에는 실제 분기값 선택 필드를 둔다 (추가 질문 없이 자동) | [07](references/07-forms.md) [08](references/08-reference-info.md) |
| 4 | **자체 점검 & 보정** | 만든 정의를 `read_file` 로 다시 읽어 [02](references/02-generate-definition.md) 체크리스트와 직접 대조하고, 결함이 있으면 `edit_file`/`write_file` 로 고친다 (도구 호출 없음) | [09](references/09-service-execution.md) |
| 5 | **완료** | 산출물 파일을 다 만들었으면 **그냥 턴을 끝낸다.** 시스템이 자동으로 검증·통합해 프론트 산출물 패널로 전달한다. 채팅엔 한 줄 안내만 | [09](references/09-service-execution.md) [12](references/12-deepagents-execution.md) |

**각 단계 진입 시 해당 reference 를 `read_file` 로 읽고 그 규칙대로** 한다(progressive
disclosure). 경로·파일명·자동 마무리 규칙은 [references/12-deepagents-execution.md](references/12-deepagents-execution.md)
에 있으니 **1단계 전에 12번을 먼저 읽는다.**

> 🛑 **사용자에게 멈춰 묻는 것은 오직 `request_human_input` 로만** 한다(프로즈로 턴을
> 끝내지 말 것 — 턴이 끊겨 순서가 꼬인다). 멈춤은 **정확히 2곳**: 1단계 컨설팅 승인,
> 2단계 후보 선택. 그 외(3·4·5단계)는 **확인 없이 자동** 진행한다.
>
> 🔴 **2단계 후보 질문**: 후보는 `request_human_input` 의 **`question`** 에
> `[스킬]`/`[에이전트]`/`[DMN]` 섹션(대괄호만) + `• 라벨: 설명` 으로 나열한다.
> 이 형식이어야 프론트가 체크박스 선택 패널로 그린다. "어떤 자동화 요소를…",
> "스킬을 만들까요?" 같은 **빈 질문·생성여부 질문·모호어('자동화 요소') 금지** —
> 항상 구체 후보를 나열한다. 후보가 없는 종류는 섹션 자체를 만들지 않는다.
>
> 🚫 **채팅 메시지에 산출물 JSON 을 덤프하지 않는다.** 산출물은 **파일**로만 전달한다.
>
> 🚫 **사용자/조직도 조회 불필요**: `get_current_user`·조직도 조회 등을 호출하거나
> 이메일·토큰·테넌트 ID 를 요청하지 마라(tenant 는 환경변수로 주입). 곧바로 1단계
> 컨설팅 초안부터 시작한다.

---

## 이 스킬이 쓰는 도구 (이게 전부)

| 용도 | 도구 |
|---|---|
| 파일 읽기 / 쓰기 / 부분수정 | `read_file` / `write_file` / `edit_file` |
| 사용자에게 묻기(HITL) | `request_human_input` (`question`/`context`) |
| 진행상황 | `write_todos` |
| 업로드 문서 읽기 | `search_documents` (memento RAG) |
| 재사용 스킬 생성 | `task(subagent_type="skill-creator")` |

⛔ **위 목록에 없는 도구는 이 절차에서 쓰지 않는다.** 특히
`write_process_definition`·`update_process_definition`·`validate_process_definition`·
`complete_process_generation`·`save_process_definition` 은 **존재하지 않는 도구다** —
호출하려 하지 마라. 검증·통합은 턴이 끝나면 시스템이 자동으로 한다.

> ℹ️ Claude Code CLI 에서 이 스킬이 실행되는 경우에만 도구명을 이렇게 치환한다:
> `read_file`→`Read`, `write_file`→`Write`, `edit_file`→`Edit`,
> `request_human_input`→`AskUserQuestion`, `write_todos`→`TodoWrite`,
> 산출물 경로→`.bpmn/`. 이때 5단계 자동 마무리는 없으므로 4단계 자체 점검 후
> 완성본 요약을 채팅에 출력하고 끝낸다. 그 외 규칙은 전부 동일하다.

---

사용자가 만들고 싶은 업무 프로세스를 위 5단계로 함께 완성하는 skill입니다. 핵심 책임:

1. 사용자가 BPMN을 몰라도 **말로 설명한 업무(또는 업로드 문서)를 흐름(초안)으로 바꿔**
   제안하고, 승인받아 **프로세스 정의 JSON** 을 생성한다.
2. 생성된 프로세스에서 **스킬·에이전트·DMN 규칙 후보**를 뽑아 묻고(HITL), 선택한 것만
   만들어 JSON·manifest 에 반영한다.
3. 각 액티비티 **폼**과 **참조정보(inputData/conditionData)** 를 연결해 JSON을 최종 업데이트한다.
4. 규칙 체크리스트로 **스스로 점검**해 흐름 결함(끊김·도달불가 등)을 고친다.
5. 파일을 남기고 턴을 끝내면 시스템이 검증·통합해 사용자에게 제시한다.
   한 문서에 **여러 프로세스**가 있으면 [references/11-multi-process.md](references/11-multi-process.md) 의 일괄 절차를 따른다.

---

## 진입 패턴

- **자연어 요청**("휴가 신청 프로세스 만들어줘") 또는 **문서 업로드**("이 매뉴얼대로") —
  둘 다 1단계 컨설팅으로 진입한다. 문서가 있으면 `search_documents` 로 먼저 읽어 as-is
  흐름을 파악한다([references/10-document-intake.md](references/10-document-intake.md)).
- 정보가 거의 없으면(예: "영업이익 올리고 싶어") 흐름을 추측하지 말고 현황부터 컨설팅으로 묻는다.
- **독립 프로세스가 2개 이상**이면 [references/11-multi-process.md](references/11-multi-process.md) 의 **일괄** 절차를 쓴다
  (프로세스마다 따로 묻지 않는다).

## 산출물 구조 (프로세스 폴더 안)

```
<프로세스 폴더>/
├── process-definition.json      # elements[] 형식. 1단계 생성, 2·3단계 갱신
├── manifest.json                # 선택된 스킬/에이전트/DMN ↔ activity 매핑 (2단계, 필수)
├── skills/<safe-name>/SKILL.md  # 2단계 skill-creator 산출
├── agents/<agent_id>.json       # 2단계 에이전트 1명 = 파일 1개
└── forms/<activity_id>.form     # 3단계 ProcessGPT 폼
```

- 기존 파일은 **묻지 말고 덮어쓴다.**
- 산출물 파일은 **지우지 않는다**(보존).
- 경로 규칙·멀티 프로세스 폴더 규칙은 [12-deepagents-execution.md](references/12-deepagents-execution.md) 참조.

---

## 절대 하지 말 것

- **컨설팅 없이 바로 JSON 부터 만들지 않는다.** 사용자가 명시적으로 "그냥 바로 생성해"
  라고 하거나 이미 충분히 흐름을 설명한 경우가 아니면 1단계 컨설팅으로 흐름 초안을 먼저 합의한다.
- ⛔ **1단계 컨설팅 초안을 후보 나열 형식(`[스킬]` + `• 라벨: 설명`)으로 쓰지 않는다.**
  초안은 `context` 에 **번호 목록 문장**으로 담고 `question` 은 "이대로 진행할까요?"
  한 줄로 둔다(승인/반려 패널로 표시됨). 후보 나열 형식은 **2단계 전용**이다
  ([references/01-consulting.md](references/01-consulting.md)).
- 컨설팅에서 **시스템/도구/프로그램을 무엇을 쓰는지 묻지 않는다.** (우리가 그 도구를
  만들어주기 때문 — 사용자에게 혼란만 준다.) 소요 시간 등 프로세스 정의에 불필요한
  질문도 하지 않는다. 자세한 금지 질문은 [references/01-consulting.md](references/01-consulting.md) 참조.
- ⛔ **일반 BPMN/Camunda 스키마로 만들지 마라.** `type:"UserTask"`·`assignee`·`formKey`·
  `sequenceFlows`·`activities/events` 분리배열 **금지**. 반드시 **`elements[]` +
  `elementType`**(Event/Activity/Gateway/Sequence), Activity 는 `type:"UserActivity"`+
  `role`+`outputData`(1개+). 어기면 자동 검증에서 critical 결함으로 막힌다 — 추측 말고
  [02](references/02-generate-definition.md) 를 `read_file` 로 읽고 그대로 만든다.
- 2단계에서 **사용자에게 묻지 않고** 스킬/에이전트/DMN 을 임의로 다 생성하지 않는다.
  반대로 **선택받은 것은 하나도 빠뜨리지 않는다**(선택 개수 = 생성 개수).
- **스킬은 손으로 쓰지 말고 `task(subagent_type="skill-creator")` 로 위임한다.**
  위임 시 `output_path`(절대경로)·`skill_name`·`context`(관련 활동의 이름·설명·입출력)를
  반드시 함께 넘긴다([references/04-skills.md](references/04-skills.md)).
- **폼·참조정보(3단계)는 "만들까요?" 묻지 않는다.** 후보 선택 답변 직후 3·4·5단계를 자동 진행한다.
- **ExclusiveGateway는 그림만 분기시키면 안 된다.** 바로 전 UserActivity 폼에 각 outgoing
  Sequence 의 `condition` 값과 정확히 일치하는 선택 필드를 만들고, gateway `conditionData` 를
  `<form_id>.<field_name>` 문자열 배열로 연결한다. 결정 필드 없는 `승인/반려`, `충분/미흡`
  분기는 실행엔진에서 멈추므로 금지한다.
- **DMN 은 '노드/활동'이 아니다.** `elements` 에 별도 요소로 만들지 말 것 — 분기
  `ExclusiveGateway` 의 속성이며 top-level `dmn_decisions`/`dmn_rules` 에 넣는다.
  분기 게이트웨이가 없는 프로세스에는 DMN 을 추가하지 않는다.
- **DB 에 직접 쓰지 않는다(읽기 전용).** 저장은 사용자가 프론트 '저장' 버튼으로 한다.
  `save_process_definition` 호출·셸을 통한 원격 저장 등 **어떤 DB 쓰기 시도도 금지.**
- 산출물에 placeholder만 남기지 않는다 — **실제 내용으로** 채운다(빈 elements 금지).

---

## 참조 문서

이 skill 본문은 흐름만 담고, 각 단계의 디테일·규칙은 reference 에 분리되어 있습니다.
단계 진입 시 해당 파일만 읽으면 됩니다.

| 파일 | 무엇이 들어있나 |
|------|----------------|
| [references/00-orientation.md](references/00-orientation.md) | **진입 판별** — 문서 유무·프로세스 개수·정보 충분도 빠른 분류 |
| [references/01-consulting.md](references/01-consulting.md) | **컨설팅 규칙** — 흐름 초안 제안법, 금지 질문, 질문 방식, 생성 제안 타이밍 |
| [references/02-generate-definition.md](references/02-generate-definition.md) | **프로세스 정의 JSON 생성 규칙(엄격)** — 전체 스키마, 요소 타입, 역할/서브프로세스 규칙 |
| [references/03-elicit-artifacts.md](references/03-elicit-artifacts.md) | **HITL** — 스킬/에이전트/DMN 후보 도출 + 선택 질문 형식 + `manifest.json` |
| [references/04-skills.md](references/04-skills.md) | **skill-creator 로 재사용 스킬 생성** + JSON 반영(`activity.skills`) |
| [references/05-agents.md](references/05-agents.md) | 에이전트(역할) 생성 규칙 + JSON 반영(`activity.agent`, `roles`) |
| [references/06-dmn.md](references/06-dmn.md) | DMN 의사결정/규칙 생성 + JSON 반영(`dmn_decisions`, `dmn_rules`) |
| [references/07-forms.md](references/07-forms.md) | 폼 생성 규칙(ProcessGPT 폼 컴포넌트 규격) + JSON 반영(`activity.tool`) |
| [references/08-reference-info.md](references/08-reference-info.md) | 참조정보 — `activity.inputData`, gateway `conditionData` 연결 |
| [references/09-service-execution.md](references/09-service-execution.md) | **실행 모델** — 5단계 상세 + 자체 점검 체크리스트 + 역할 분담 |
| [references/10-document-intake.md](references/10-document-intake.md) | **문서 업로드 기반 생성** — 업로드 문서에서 as-is 흐름 추출 → 컨설팅 초안 |
| [references/11-multi-process.md](references/11-multi-process.md) | **여러 프로세스 생성** — 일괄 컨설팅/일괄 후보 선택 JSON 페이로드, 폴더 분리 |
| [references/12-deepagents-execution.md](references/12-deepagents-execution.md) | **실행 환경 규격** — 산출물 경로·파일명, 도구 제한, 턴 종료 후 자동 마무리, 멀티 프로세스 폴더 |

템플릿은 [assets/templates/](assets/templates/) 에 있습니다.

---

## 출처

이 skill 의 컨설팅·프로세스 정의·스킬/DMN/폼/참조정보 생성 규칙은 사내 **ProcessGPT /
pdf2bpmn** 프로젝트의 정의를 기반으로 합니다. 흐름·진행 방식은
[ddd-starter-modelling-process](https://github.com/ddd-crew/ddd-starter-modelling-process)
스타일과 GitHub Spec Kit 의 단계형 사용 방식을 참고했습니다.
