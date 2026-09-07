# 12 – 실행 환경 규격: 경로 · 파일명 · 도구 제한 · 자동 마무리

**1단계에 들어가기 전에 이 파일을 먼저 읽는다.** 산출물을 어디에 어떤 이름으로
만들어야 하는지, 무엇을 호출하면 안 되는지, 턴을 끝내면 무슨 일이 일어나는지를
규정한다.

---

## 1. 산출물 경로

경로는 이 문서가 아니라 **시스템 프롬프트의 `📁 이번 대화 전용 산출물 경로` 섹션**이
정한다. 거기 적힌 값을 그대로 쓴다.

- 기준 디렉터리(`<base>`): 대화(채팅방)마다 다르다. 예) `/workspace/.bpmn/<방슬러그>`
- **프로세스 폴더명**: 시스템 프롬프트가 지정한 **정확한 이름**(예: `process-a1b2c3d`)을
  그대로 쓴다. 두 번째 프로세스부터는 그 뒤에 `-2`, `-3` 만 붙인다.
- ⚠️ `/workspace/.bpmn/...` 처럼 **base 를 빼고 쓰지 말 것.** 폴더명에 `<`, `>` 나
  '랜덤'·'id'·'new_id' 같은 placeholder 단어를 절대 넣지 말 것(`process-<랜덤 id>` ❌).
- 기존 파일은 **묻지 말고 덮어쓴다.** 산출물 파일은 삭제하지 않는다.

### 파일 이름 규격 (이 이름이어야 시스템이 인식한다)

```
<base>/<프로세스폴더>/
├── process-definition.json      # elements[] 정의
├── manifest.json                # 선택 결과 ↔ activity 매핑
├── skills/<safe-name>/SKILL.md  # 스킬 1개 = 폴더 1개
├── agents/<agent_id>.json       # 에이전트 1명 = 파일 1개 (배열 금지)
└── forms/<activity_id>.form     # 확장자 `.form`, 파일명 = activity id
```

**단독 스킬 생성**(프로세스가 아님)이면 프로세스 폴더 없이
`<base>/skills/<safe-name>/SKILL.md` 에 바로 만든다.

---

## 2. 쓸 수 있는 도구 / 쓰면 안 되는 도구

**쓰는 것**: `read_file` · `write_file` · `edit_file` · `request_human_input` ·
`write_todos` · `search_documents` · `task(subagent_type="skill-creator")`.

**존재하지 않으므로 호출 금지**: `write_process_definition`,
`update_process_definition`, `validate_process_definition`,
`complete_process_generation`, `save_process_definition`.
정의 파일은 `write_file`(최초·구조 변경) / `edit_file`(필드 변경) 로 다룬다.

**있어도 프로세스 '생성' 중에는 호출 금지**: process-gpt-mcp 의 조회/실행 계열
(`get_current_user`, `get_process_list`, `get_process_detail`, `get_form_fields`,
`execute_process`, 조직도 조회, `create_consulting_process_workitem`,
`create_pdf2bpmn_workitem` 등)과 Git 계열(`list_git_repos`, `checkout_to_workspace`,
`publish_skill`). 이 도구들은 '이미 존재하는 프로세스의 실행/조회'용이지 '새 정의
생성'용이 아니다. 프로세스 생성에는 현재 사용자·JWT·테넌트·조직도 정보가 필요 없다
(tenant 는 `TENANT_ID` 환경변수로 주입되고 역할·폼은 이 스킬 절차가 정한다) —
사용자에게 JWT·테넌트 ID·이메일을 요청하지 말고 막힘 없이 진행한다.
단, 사용자가 '기존 프로세스 실행/조회/업무 처리'를 요청한 경우(생성이 아님)엔 이
제한이 적용되지 않는다.

---

## 3. 단계별 실행 메모

### 1단계 — 컨설팅 & JSON 생성

업로드된 문서는 이미 memento 에 임베딩되어 있다. `pdf2markdown` 같은 변환 도구를
쓰지 말고 **`search_documents`** 로 파악한다(가장 안정적으로 본문 청크를 준다).
요청 주제와 관련된 키워드('프로세스', '절차', '단계', '승인', '신청', 핵심 업무명)로
2~3회 호출해 본문을 모은다. `list_documents`/`summarize_document`/
`read_document_page`/`grep_in_document` 는 보조 도구이며 이들이 '찾을 수 없음'·404·
빈 결과를 반환해도 무시하고 `search_documents` 결과로 진행한다. `search_documents`
가 청크를 하나라도 반환하면 그것이 곧 문서 내용이므로 절대 "문서를 읽지 못했다"고
말하지 말고 그 내용으로 초안을 만든다. 정말로 모든 키워드에서 0건일 때만 그렇게 알린다.

열린 질문을 던지지 말고 곧바로 실무 수준의 상세한 초안(보통 5~8단계, 담당 역할,
승인/반려 분기, 반려 후 재신청 같은 현실적 흐름 포함)을 만든다
([01-consulting.md](01-consulting.md) 규칙).

**초안을 어시스턴트 텍스트로 쓰고 턴을 끝내지 말고, 반드시 `request_human_input`
으로 승인/반려를 묻는다**:

- `question`: `"이대로 진행할까요? 추가하거나 바꿀 단계가 있으면 알려주세요."` 한 줄만.
- `context`: 초안 전체(번호 단계·담당 역할·분기 포함).

이러면 승인/반려 + 자유수정 패널로 표시된다. 사용자가 승인('승인'·'진행'·'좋아요'·
'네' 등)하면 다시 초안을 제시하거나 되묻지 말고 곧바로 JSON 생성으로 진행한다.

승인받으면 [02-generate-definition.md](02-generate-definition.md) 규격 그대로
`write_file` 로 `process-definition.json` 을 만든다(빈 elements 금지).
`processDefinitionName` 은 사용자가 요청한 프로세스 이름으로 반드시 채운다.

### 2단계 — 후보 선택 & 생성

`request_human_input` 의 `question` 에 `[스킬]`/`[에이전트]`/`[DMN]` 섹션(대괄호만) +
`• 라벨: 설명` 으로 나열한다([03-elicit-artifacts.md](03-elicit-artifacts.md)).
라벨은 구체적이고 서로 다른 고유 이름이어야 한다(예: "휴가신청서 작성 도우미:
신청서를 정확히 작성하도록 안내") — "역할"·"규칙"·"자동화 요소" 같은 일반 단어 금지.
후보가 없으면 그 섹션은 생략(빈 질문 금지). DMN 후보는 분기 `ExclusiveGateway` 가
실제로 있을 때만 제시.

후보 채택 기준: **스킬**은 같은 성격의 작업이 2개 이상 활동에서 반복되거나 재사용
가능한 전문 역량일 때만. **에이전트**는 반복·규칙적이라 자동화 이득이 큰 경우에만
(최종 승인·책임 소재·정책 결재처럼 사람 판단이 중요한 작업엔 붙이지 않음).
**DMN**은 분기 게이트웨이가 있을 때만.

선택을 받으면:

- **스킬** — `task(subagent_type="skill-creator")` 로 위임한다. task 설명에
  `output_path`(= `<프로세스폴더>/skills/<safe-name>/SKILL.md` 절대경로),
  `skill_name`, `context`(관련 활동들의 이름·설명·입출력·지침)를 **반드시** 담는다.
  직접 손으로 빈약하게 쓰지 말 것.
- **에이전트** — `agents/<agent_id>.json` 에 **에이전트마다 개별 파일**로
  `{"id","name","role","goal","persona","description"}` 단일 객체를 쓴다. 여러
  에이전트를 한 파일에 합치지 말 것(산출물 패널의 에이전트별 '편집' 연결이 끊긴다).
- **DMN** — `process-definition.json` 의 top-level `dmn_decisions`/`dmn_rules`.

**선택된 모든 후보를 빠짐없이 생성한다** — 선택 개수와 생성 개수가 일치해야 한다.

그리고 반드시 `manifest.json` 을 `write_file` 한다(스킬/에이전트/DMN 을 activity ·
gateway 에 연결하는 핵심 매핑):

```json
{ "skills": [{"name":"<safe-name>", "activity_ids":["..."]}],
  "agents": [{"id":"<agent id>", "activity_ids":["..."]}],
  "dmn": [{"id":"<dmn id>", "gateway_id":"<gateway id>", "decisions":[], "rules":[]}] }
```

`activity_ids` 는 실제 수행 Activity 의 id 다 — StartEvent/EndEvent/Gateway id 금지.
마무리 단계가 이 매핑으로 `activity.skills`/`agent`/`agentMode`/`orchestration`·`dmn`
을 자동 반영한다.

### 3단계 — 폼 & 참조정보

각 UserActivity 폼을 `forms/<activity_id>.form`(확장자 `.form`)로 만든다. 폼은 평문
`<form><input>` 이 아니라 ProcessGPT 폼 컴포넌트 규격([07-forms.md](07-forms.md))으로
작성한다. `date-field`·`number-field` 같은 태그는 없다(날짜·숫자는 `text-field` 의
`type` 으로). 정보가 부족하면 `free_input` textarea-field 하나라도 넣는다.
참조정보(inputData/conditionData)는 [08-reference-info.md](08-reference-info.md)
규칙대로 자동 반영(추가 질문 없이).

### 4단계 — 자체 점검

전용 검증 도구는 없다. `process-definition.json` 을 `read_file` 로 다시 읽고
[09-service-execution.md](09-service-execution.md) 의 체크리스트를 직접 대조해
결함을 `edit_file`/`write_file` 로 고친다. **점검 내용을 채팅에 나열하거나 "결함이
발견됐다"고 설명하지 말 것** — 조용히 끝낸다.

### 5단계 — 완료

산출물을 다 만들었으면 **아무 도구도 더 호출하지 말고 턴을 끝낸다.**
시스템이 자동으로:

1. 각 프로세스 폴더의 정의를 스키마·흐름 검증하고 필요하면 LLM 으로 보정하며,
2. 폼·스킬·에이전트·DMN 을 manifest 기준으로 정의에 통합하고,
3. 산출물 파일 전체를 프론트 산출물 패널로 전달한다.

채팅엔 **"프로세스를 생성했어요. 확인 후 저장 버튼을 눌러주세요."** 한 줄만 남긴다
(반복 안내 금지, JSON 덤프 금지). 실제 저장은 사용자가 화면에서 '저장' 버튼을 눌러
수행한다 — 이 스킬은 DB 에 쓰지 않는다.

---

## 4. 불변 규칙

- **사용자에게 멈춰 묻는 것은 오직 `request_human_input`.** 멈춤은 정확히 2곳:
  1단계 컨설팅 승인, 2단계 후보 선택. 나머지는 자동.
- **산출물만 생성한다(저장·업로드·삭제 금지).**
- **채팅 메시지에 산출물 JSON 을 덤프하지 않는다.**
- **DMN 은 '노드/활동'이 아니다** — `elements` 에 넣지 말고 top-level
  `dmn_decisions`/`dmn_rules` 에 넣어 해당 gateway 에 연결한다.
- **생성한 스킬/에이전트/DMN/폼을 정의에 반영**: activity 의 `skills`(배열)·`agent`·
  `tool`(`formHandler:<activity_id>`), top-level `dmn_decisions`/`dmn_rules`.
  (`manifest.json` 만 정확하면 누락분은 마무리 단계가 보정한다.)

---

## 5. 멀티 프로세스 (서로 다른 프로세스 여러 개)

요청을 서로 다른 프로세스 목록으로 먼저 분해한다. 각 프로세스는 서로 다른 이름과
폴더(`<프로세스폴더>`, `<프로세스폴더>-2`, …)를 쓴다. 단계를 프로세스별로 끝내지
말고, 같은 단계를 모든 프로세스에 대해 한꺼번에 진행한다.

- **1단계 컨설팅(일괄)**: `request_human_input` 을 **딱 한 번** 호출하되 `question`
  에 아래 JSON 한 개만 넣는다:
  `{"multi_process": true, "stage": "consult", "processes": [{"name": "...", "draft": "<5~8단계 상세 초안>"}, ...]}`
  응답은 `[프로세스명] 승인` / `[프로세스명] 반려 - 사유` 형식 줄들로 온다.
- 승인 후 각 프로세스의 `process-definition.json` 을 각자 폴더에 생성한다.
- **2단계 후보(일괄)**: `request_human_input` 을 **딱 한 번** 호출하되 `question` 에
  아래 JSON 한 개만 넣는다(조건 충족 종류만):
  `{"multi_process": true, "stage": "candidates", "processes": [{"name": "...", "skills": [{"label":"...","desc":"..."}], "agents": [...], "dmn": [...]}, ...]}`
  응답은 `[프로세스명] 스킬: A, B / 에이전트: C / DMN: (없음)` 형식 줄들로 온다.
  선택 항목 전량을 각 프로세스 폴더에 생성한다.
- 폼/참조정보(3단계)·자체 점검(4단계)도 모든 프로세스에 대해 진행한 뒤 턴을 끝낸다.
  마무리는 모든 프로세스 폴더를 한 번에 처리한다.
- ⚠️ 1·2단계 `request_human_input` 은 위 JSON으로 **각 한 번씩만** 호출한다
  (프로세스마다 따로 묻지 말 것).
- 상세 규칙: [11-multi-process.md](11-multi-process.md).
