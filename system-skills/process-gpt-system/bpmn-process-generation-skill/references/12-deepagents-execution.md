# 12 – deepagents 실행 환경: 도구 이름 매핑 + 전용 절차

이 파일은 **process-gpt 채팅(deepagents)** 에서 이 스킬이 실행될 때만 적용된다.
Claude Code CLI에서는 이 파일을 무시하고 위 표준 5단계(`Read`/`Write`/`Edit`/
`AskUserQuestion`/`TodoWrite` 기반)를 그대로 따른다. deepagents는 그 도구들이
없는 대신 아래 표의 네이티브 도구와, 이 스킬 전용 도구(`validate_process_definition`,
`complete_process_generation`)가 있다 — 있으면 반드시 이 파일 절차를 따른다.

## 도구 이름 매핑

| 표준 절차(위 5단계 표)의 도구 | deepagents 실제 도구 |
|---|---|
| `Read` | `read_file` |
| `Write` | `write_file` |
| `Edit` | `edit_file` |
| `AskUserQuestion` | `request_human_input`(`question`/`context` 인자) |
| `TodoWrite` | `write_todos` |
| (없음 — 4단계를 직접 재확인) | `validate_process_definition`(전용 도구, 최대 5회 자동교정) |
| (없음 — 5단계에서 채팅에 JSON 출력) | `complete_process_generation`(전용 도구, 인자 없음) |

산출물 위치도 다르다: `.bpmn/` 상대경로가 아니라, **이번 대화 전용 산출물
경로**(대화별로 동적으로 정해짐 — 시스템 프롬프트에 매 대화마다 주입되는
`📁 이번 대화 전용 산출물 경로` 섹션의 정확한 절대경로를 그대로 쓴다.
`/workspace/.bpmn/...` 처럼 그 base 를 빼고 쓰지 말 것). 그 경로 아래
`process-definition.json`·`forms/<activity_id>.form`·`skills/<name>/SKILL.md`·
`agents/<agent_id>.json`(에이전트 1개=파일 1개)·`manifest.json` 을 만든다.

## 절차 (5단계, 도구만 다르고 흐름은 동일)

**시작하자마자 `write_todos`** 로 고정 이름 5개를 그대로 등록(이름 변경 금지):
`1. 프로세스 초안 설계 & JSON 생성` / `2. 스킬·에이전트·DMN 선택 & 생성` /
`3. 입력 폼 & 참조정보 생성` / `4. 프로세스 검증 & 자동 보정` / `5. 결과 통합 & 완료`

### 도구 제한 (엄격 — 프로세스 '생성' 중에는)

process-gpt-mcp 의 **조회/실행 계열 도구를 절대 호출하지 않는다**: `get_current_user`,
`get_process_list`, `get_process_detail`, `get_form_fields`, `execute_process`,
조직도(organization) 조회, `create_consulting_process_workitem`,
`create_pdf2bpmn_workitem` 등. **Git 관련 도구**(`list_git_repos`·
`checkout_to_workspace`·`publish_skill`)와 `repo-scout` 서브에이전트도 호출하지
않는다 — 스킬은 산출물 경로에 직접 만들고 저장은 사용자(프론트)가 한다. 이
도구들은 '이미 존재하는 프로세스의 실행/조회'용이지 '새 프로세스 정의 생성'용이
아니다. 프로세스 '생성'에는 현재 사용자·JWT·테넌트·조직도 정보가 필요 없다
(저장용 tenant는 `TENANT_ID` 환경변수로 자동 주입, 역할·폼은 이 스킬 절차가
정함) — 그러므로 사용자에게 JWT·테넌트 ID·이메일을 요청하지 말고 막힘 없이
진행한다. 단, 사용자가 '기존 프로세스 실행/조회/업무 처리'를 요청한 경우
(생성이 아님)엔 이 제한이 적용되지 않는다.

### 1단계 — 컨설팅 & JSON 생성

문서가 업로드된 경우, 그 문서는 이미 memento 에 임베딩되어 있다.
`pdf2markdown` 같은 변환 도구를 쓰지 말고 memento RAG 도구로 파악한다 —
주력 도구는 `search_documents` 다(항상 임베딩되어 있어 본문 청크를 가장
안정적으로 읽을 수 있다). 요청 주제와 관련된 키워드(예: '프로세스', '절차',
'단계', '승인', '신청', 핵심 업무명)로 `search_documents` 를 2~3회 호출해
본문 청크를 모은다. `list_documents`/`summarize_document`/`read_document_page`/
`grep_in_document` 는 보조 도구이며 이들이 '찾을 수 없음'·404·빈 결과를
반환해도 무시하고 `search_documents` 결과로 진행한다. `search_documents` 가
청크를 하나라도 반환하면 그것이 곧 문서 내용이므로 절대 "문서를 읽지 못했다"고
말하지 말고 그 내용으로 초안을 만든다. 정말로 모든 키워드에서 0건일 때만
그렇게 알린다.

그 내용에서 프로세스 흐름을 추출해([references/01-consulting.md](01-consulting.md)
규칙대로) 초안을 만든다. 열린 질문을 던지지 말고 곧바로 실무 수준의 상세한
초안(보통 5~8단계, 담당 역할, 승인/반려 분기, 반려 후 재신청 같은 현실적
흐름 포함)을 제시한다. **초안을 본문(어시스턴트 텍스트)으로 쓰고 턴을 끝내지
말고, 반드시 `request_human_input` 도구로 승인/반려를 묻는다** — `question`
에는 "이대로 진행할까요? 추가하거나 바꿀 단계가 있으면 알려주세요." 만 넣고,
`context` 에 초안 전체(번호 단계·담당 역할·분기 포함)를 담는다(승인/반려형
HITL 패널로 표시됨). 사용자가 승인('승인'·'진행'·'좋아요'·'네' 등)하면 다시
초안을 제시하거나 되묻지 말고 곧바로 JSON 생성으로 진행한다.

승인받으면 프로세스 정의(`elements[]` 형식, [02-generate-definition.md](02-generate-definition.md)
규칙 그대로)를 `write_file` 로 산출물 경로의 `process-definition.json` 에
만든다(빈 elements 금지). `processDefinitionName` 은 사용자가 요청한 프로세스
이름으로 반드시 채운다.

### 2단계 — 후보 선택 & 생성

elements 에서 스킬/에이전트/DMN 구체 후보를 도출해 `request_human_input` 으로
묻는다([03-elicit-artifacts.md](03-elicit-artifacts.md) 규칙). `question` 에
`[스킬]`/`[에이전트]`/`[DMN]` 섹션(대괄호만) + `• 라벨: 설명` 으로 나열한다.
라벨은 구체적이고 서로 다른 고유 이름이어야 한다(예: "휴가신청서 작성 도우미:
신청서를 정확히 작성하도록 안내") — "역할"·"규칙"·"자동화 요소" 같은 일반
단어 금지. 후보가 없으면 그 섹션은 생략(빈 질문 금지). DMN 후보는 분기
`ExclusiveGateway` 가 실제로 있을 때만 제시.

후보 채택 기준: **스킬**은 같은 성격의 작업이 2개 이상 활동에서 반복되거나
재사용 가능한 전문 역량일 때만. **에이전트**는 반복·규칙적이라 자동화 이득이
큰 경우에만(최종 승인·책임 소재·정책 결재처럼 사람 판단이 중요한 작업엔 붙이지
않음, 가능하면 기존 에이전트 재사용). **DMN**은 분기 게이트웨이가 있을 때만.

선택을 받으면 스킬은 **반드시 `skill-creator` 서브에이전트에 `task()` 로
위임해 생성**한다(활동들의 이름·설명·지침을 풍부한 컨텍스트로 함께 전달해
품질 높은 SOP 형식 SKILL.md 가 나오게 함, 직접 손으로 빈약하게 쓰지 말 것).
결과는 산출물 경로의 `skills/<safe-name>/SKILL.md`. 에이전트는 **에이전트마다
개별 파일** `agents/<agent_id>.json` 로 만든다(단일 에이전트 객체
`{"id","name","role","goal","persona","description"}`; 여러 에이전트를 한
파일에 합치지 말 것 — 산출물 패널의 에이전트별 '편집' 연결을 위해 1에이전트=
1파일). 그리고 `edit_file` 로 process-definition.json 을 업데이트한다.

**선택된 모든 후보를 빠짐없이 생성한다** — 선택 개수와 생성 개수가 일치해야
한다. 그리고 반드시 `manifest.json` 을 `write_file` 로 만든다(스킬/에이전트/
DMN 을 activity·gateway 에 연결하는 핵심):
```json
{ "skills": [{"name":"<safe-name>", "activity_ids":["..."]}],
  "agents": [{"id":"<agent id>", "activity_ids":["..."]}],
  "dmn": [{"id":"<dmn id>", "gateway_id":"<gateway id>", "decisions":[...], "rules":[...]}] }
```
(`activity_ids` 는 실제 수행 Activity의 id — StartEvent/EndEvent/Gateway id
금지. `complete_process_generation` 이 이 매핑으로 `activity.skills`/`agent`/
`agentMode`/`orchestration`·`dmn` 을 자동 반영한다.)

### 3단계 — 폼 & 참조정보

각 UserActivity 폼을 산출물 경로의 `forms/<activity_id>.form`(확장자 `.form`)
로 만든다. 폼은 평문 `<form><input>` 이 아니라 ProcessGPT 폼 컴포넌트 규격
([07-forms.md](07-forms.md))으로 작성한다. `date-field`·`number-field` 같은
태그는 없다(날짜·숫자는 `text-field` 의 `type` 으로). 정보가 부족하면
`free_input` textarea-field 하나라도 넣는다. 참조정보(inputData/conditionData)
는 [08-reference-info.md](08-reference-info.md) 규칙대로 자동 반영(추가 질문
없이).

### 4단계 — 자체 검증

`validate_process_definition` 도구로 검증한다. 결함이 있으면 도구가 최대 5회
자동교정하며, 반환 결과가 `passed=false` 면 파일을 보고 직접 더 고친 뒤 다시
호출한다. **검증 결함 내용을 채팅에 나열하거나 "결함이 발견됐다"고 설명하지
말 것** — 검증·보정은 이 단계에서 조용히 끝낸다.

### 5단계 — 전략 정합성 게이트, 결과 통합 & 완료

`complete_process_generation`을 호출하기 **직전**, 각 프로세스 초안의 이름·설명·주요
활동을 합친 설명으로 `check_strategy_alignment(description, tenant_id)`를 반드시 호출한다.

- KPI 후보가 있으면 후보와 근거를 `request_human_input`으로 제시한다. 사용자가 선택한
  KPI만 완료 후 반환된 프로세스 ID와 `link_kpi_to_process`로 연결한다. 자동 연결 금지.
- 후보가 없으면 "관련 전략 항목 없음"을 알리고 계속 진행 여부를 확인한 뒤 진행한다.
- 조회 오류이면 확인 불가 사유를 알리고 계속 진행 여부를 확인한다. 오류 자체로 생성을 막지 않는다.
- 프로세스 정의 최상위 `strategyAlignment`에 `status`, `checkedAt`, `selectedKpiIds`,
  `candidates`, `confirmation`(미연결/관련 항목 없음/확인 불가 시 사용자 확인)을 기록한 뒤
  검증하고 완료한다. 이 필드가 없으면 확정하지 않는다.

정합성 확인을 위한 질문은 필수 게이트이므로 아래의 일반적인 "멈춤은 정확히 2곳" 제한에
추가되는 예외다. 멀티 프로세스일 때는 모든 후보를 한 번의 구조화된 질문으로 묶는다.

`complete_process_generation` 도구를 인자 없이 호출한다. 도구가 산출물 경로
파일들을 모아 프론트로 전달한다(작업 파일은 보존). 그 뒤 채팅엔 **"프로세스를
생성했어요. 확인 후 저장 버튼을 눌러주세요."** 한 줄만 남긴다(반복 안내 금지).

**완료 후 — KPI-프로세스 자동 연계(조건부)**: 이번 대화에서 이 프로세스를
생성하기 전에 BSC 전략 인터뷰 등을 통해 이미 저장된 전략 목표
(`save_strategy_objective`)나 KPI(`save_strategy_kpi` 로 만든 `kpi_id`)가
있고, 사용자가 "이 프로세스는 OO KPI의 선행 지표를 만든다/측정한다" 같은
취지로 이 KPI와 지금 만드는 프로세스의 연관성을 언급했다면,
`complete_process_generation` 이 반환한 `processes[].processDefinitionId` 를
받은 직후 `link_kpi_to_process(kpi_id, proc_def_id, tenant_id)` 를 호출해
연결한다(여러 프로세스를 함께 만들었다면 해당 KPI가 언급된 프로세스의 id로).
대화에 그런 맥락이 전혀 없으면 이 단계는 건너뛴다 — 매번 강제하지 않는다.

## 불변 규칙 (deepagents)

- **사용자에게 멈춰 묻는 것은 오직 `request_human_input`**(프로즈로 턴 종료
  금지). 멈춤은 정확히 2곳: 1단계 컨설팅 승인, 2단계 후보 선택. 나머지는 자동.
- **산출물만 생성한다(저장·업로드·삭제 금지).** DB 에 직접 쓰지 않으며(읽기
  전용) 산출물 파일도 지우지 않는다. **프로세스/스킬 저장은 사용자가
  프론트에서** 한다. `save_process_definition`·`run_postprocess.py`·셸을 통한
  원격 DB 저장 등 **어떤 DB 쓰기 시도도 금지.**
- **채팅 메시지에 산출물 JSON 을 덤프하지 않는다.** 산출물은 파일 +
  `complete_process_generation` 으로만 전달한다.
- **DMN 은 '노드/활동'이 아니다.** `elements`/`activities` 에 별도 요소로
  만들지 말 것. 분기 `ExclusiveGateway` 의 속성이며, `process-definition.json`
  의 top-level `dmn_decisions`/`dmn_rules` 에 넣고 해당 gateway 에 연결한다.
  분기 게이트웨이가 없는 프로세스에는 DMN 을 추가하지 않는다.
- **생성한 스킬/에이전트/DMN/폼을 process-definition.json 에 반드시 반영**:
  activity 의 `skills`(배열)·`agent`·`tool`(`formHandler:<form_id>`), top-level
  `dmn_decisions`/`dmn_rules`. 반영 없이 5단계로 넘어가지 않는다.

## 멀티 프로세스(서로 다른 프로세스 여러 개 요청) — 일괄 처리

요청을 서로 다른 프로세스 목록으로 먼저 분해한다. 각 프로세스는 서로 다른
이름과 폴더(`process-<id>`, `process-<id>-2`, ...)를 쓴다. 단계를 프로세스별로
끝내지 말고, 같은 단계를 모든 프로세스에 대해 한꺼번에 진행한다:

- **1단계 컨설팅(일괄)**: `request_human_input` 을 **딱 한 번** 호출하되
  `question` 에 아래 JSON 한 개만 넣는다:
  `{"multi_process": true, "stage": "consult", "processes": [{"name": "...", "draft": "<5~8단계 상세 초안>"}, ...]}`
  응답은 `[프로세스명] 승인` / `[프로세스명] 반려 - 사유` 형식 줄들로 온다.
- 승인 후 각 프로세스의 `process-definition.json` 을 각자 폴더에 생성한다.
- **2단계 후보(일괄)**: `request_human_input` 을 **딱 한 번** 호출하되
  `question` 에 아래 JSON 한 개만 넣는다(조건 충족 종류만):
  `{"multi_process": true, "stage": "candidates", "processes": [{"name": "...", "skills": [...], "agents": [...], "dmn": [...]}, ...]}`
  응답은 `[프로세스명] 스킬: A, B / 에이전트: C / DMN: (없음)` 형식 줄들로 온다.
  선택 항목 전량을 각 프로세스 폴더에 생성한다.
- 폼/참조정보(3단계)·검증(4단계)도 모든 프로세스에 대해 진행하고,
  `complete_process_generation` 은 마지막에 **한 번만** 호출한다(모든
  `process-*/` 폴더 자동 처리).
- ⚠️ 1·2단계 `request_human_input` 은 위 JSON으로 **각 한 번씩만** 호출한다
  (프로세스마다 따로 묻지 말 것).
