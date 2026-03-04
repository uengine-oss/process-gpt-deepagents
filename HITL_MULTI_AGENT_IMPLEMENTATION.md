## DeepAgents 기반 도메인 독립 멀티 에이전트 하네스 + HITL 구현 정리

이 문서는 2026-02 기준으로 `process-gpt-deepagents-action` 프로젝트에 적용된 **멀티 에이전트 실행 하네스 + HITL(Execution Interrupt)** 설계를 코드 레벨에서 정리한 것입니다.

---

## 1. ExecutionContext 및 events.data 표준 구조

- **위치**: `agent_execution.py`
- **핵심 역할**: 한 번의 실행(run)을 식별하고, 멀티 에이전트 호출 스택을 추적하기 위한 최소 실행 컨텍스트를 유지.

### 1.1 ExecutionContext

- 클래스: `ExecutionContext`
- 주요 필드:
  - `correlation_id: str`  
    - 하나의 실행(run)을 묶는 UUID.  
    - 루트 오케스트레이터 실행 시 생성되며, 재개(resume) 시에는 이전 값을 재사용.
  - `agent_path: list[str]`  
    - 현재 호출 스택. 예: `["root_orchestrator", "sub_agent_a"]`.
  - `current_agent: str | None`  
    - 현재 이벤트를 발생시키는 에이전트 이름 (보통 `agent_path`의 마지막 요소).
  - `task_id: str | None`, `proc_inst_id: str | None`
    - 상위 시스템의 태스크/프로세스 인스턴스 식별자. 트레이싱용 메타 정보.

- 주요 메서드:
  - `push_agent(agent_name)` / `pop_agent()`  
    - 호출 스택에 에이전트 이름을 push/pop.
  - `with_agent(agent_name)`  
    - 컨텍스트 매니저 형태로 push/pop을 감싸는 유틸.
  - `as_dict()`  
    - 실행 메타데이터 스냅샷(dict) 반환.  
    - 이 값은 **events.data.meta** 안에 포함된다.

### 1.2 events.data 표준 구조

- 모든 이벤트의 `data`는 다음과 같은 2계층 구조를 따른다:

```jsonc
{
  "meta": {
    "correlation_id": "...",
    "agent_path": ["root_orchestrator", "sub_agent_a"],
    "agent_name": "sub_agent_a",
    "depth": 2,
    "task_id": "...",
    "proc_inst_id": "...",
    "logical_event_type": "agent_started" | "agent_finished" | "tool_call" | "tool_result" | "interrupt" | "resume" | "artifact",
    "phase": "planning" | "reasoning" | "tool_call" | "tool_result" | "interrupt" | "resume" | "final",
    "tool_name": "..." | null,
    "requires_human": true | false | null,
    "input_schema": { ... } | null,
    "output_schema": { ... } | null
  },
  "payload": {
    // UI/사용자에게 보여줄 실행 로그 본문
  }
}
```

- **DB 스키마 변경 없음**:
  - `database.sql`의 `event_type_enum`은 그대로 사용 (`task_started`, `task_completed`, `tool_usage_started`, `tool_usage_finished`, `crew_completed`, `human_asked`, `human_response`, `human_checked`, `task_working`, `error`).
  - 새 enum 값은 추가하지 않았다.
  - 논리 이벤트 타입(`logical_event_type`)과 `phase`는 오직 `data.meta` 안의 JSON 필드로만 관리한다.

### 1.3 build_event_payload

- 함수: `agent_execution.build_event_payload`
- 역할:
  - `ExecutionContext`에서 메타 필드(`meta`)를 구성.
  - 호출자가 넘긴 `extra_data`를 `payload`로 래핑.
- 반환 값 예시:

```jsonc
{
  "meta": {
    "correlation_id": "...",
    "agent_path": ["root_orchestrator"],
    "agent_name": "root_orchestrator",
    "depth": 1,
    "task_id": "...",
    "proc_inst_id": "...",
    "logical_event_type": "agent_started",
    "phase": "planning"
  },
  "payload": {
    "message": "루트 오케스트레이터가 작업 계획 수립을 시작했습니다.",
    "task_description": "..."
  }
}
```

---

## 2. 오케스트레이터 / 서브 에이전트 / 최종 결과 이벤트

- **위치**: `deepagents_action_executor.py`
- **도메인 독립성 유지**:
  - 특정 업종/스킬 이름(KOSPI, global-investment-analyzer 등)에 의존하는 분기 없음.
  - 모든 메시지는 일반적인 “계획 수립/에이전트 실행/도구 호출/최종 결과” 개념에만 기반.

### 2.1 오케스트레이터(플래너) 이벤트

- **task_started (`events.event_type='task_started'`)**
  - `data.meta`:
    - `logical_event_type = "agent_started"`
    - `phase = "planning"`
    - `agent_name = "root_orchestrator"`
    - `agent_path = ["root_orchestrator"]`
  - `data.payload`:
    - `message`: `"루트 오케스트레이터가 작업 계획 수립을 시작했습니다."`
    - `task_description`: Description/Instruction을 합친 텍스트 (없으면 빈 문자열).

- **task_completed (`events.event_type='task_completed'`)**
  - `data.meta`:
    - `logical_event_type = "agent_finished"`
    - `phase = "planning"`
    - `agent_name = "root_orchestrator"`
  - `data.payload`:
    - `message`: `"작업 계획 수립이 완료되었습니다."`
    - `plan`: 오케스트레이터가 생성한 `[PLAN]...[/PLAN]` 블록 텍스트 (없으면 정제된 전체 출력).

### 2.2 서브 에이전트 이벤트 (폴백 포함)

- DeepAgents의 `task()` 호출이 미들웨어에 의해 감지될 경우:
  - 시작/완료 시 `task_started`/`task_completed` 이벤트를 emit.
- 오케스트레이터가 한 번도 서브 에이전트를 호출하지 않거나, 미들웨어가 동작하지 않는 경우:
  - 실행 후 폴백으로 서브 에이전트 구성/도구 사용 내역을 기반으로 시작/완료 이벤트를 한 번씩 생성.

- **서브 에이전트 시작 (`task_started`)**
  - `data.meta`:
    - `logical_event_type = "agent_started"`
    - `phase = "reasoning"`
    - `agent_name = 서브 에이전트 이름`
    - `agent_path`는 오케스트레이터와 동일한 ExecutionContext 경로를 공유 (현재 구현에서는 단순 루트+이름 레벨).
  - `data.payload`:
    - `message`: `"서브 에이전트 '{name}' 실행을 시작했습니다."`
    - `role`: 에이전트 역할
    - `goal`: 에이전트 목표

- **서브 에이전트 완료 (`task_completed`)**
  - `data.meta.logical_event_type = "agent_finished"`
  - `data.payload`:
    - `message`: `"서브 에이전트 '{name}' 실행이 완료되었습니다."`
    - `summary`:  
      - 사용된 도구가 있으면 `"다음 도구 호출을 통해 작업을 수행했습니다: ..."`  
      - 없으면 `"도구 호출 이벤트 없이 작업을 수행했습니다."`

### 2.3 최종 결과 이벤트 및 아티팩트

- **결과 생성 시작 (`task_started`, crew_type="result")**
  - `data.meta.logical_event_type = "agent_started"`
  - `phase = "final"`
  - `agent_name = "final_result"`
  - `data.payload`:
    - `message`: `"최종 결과 생성을 시작했습니다."`

- **결과 생성 완료 (`task_completed`, crew_type="result")**
  - `data.meta.logical_event_type = "agent_finished"`
  - `phase = "final"`
  - `data.payload`:
    - `message`: `"최종 결과 생성이 완료되었습니다."`
    - `result`:  
      - 폼이 있는 경우: `pure_form_data`  
      - 폼이 없거나 `defaultform`인 경우:  
        `{"output": {"status": "SUCCESS" | "Fail", "result": "<plain text>"}}`

- **아티팩트 이벤트 (`task_completed`, job_id="artifact")**
  - `_enqueue_artifact`가 호출되어:
    - `events.event_type = "task_completed"`
    - `job_id = "artifact"`
    - `data.meta.logical_event_type = "artifact"`
    - `data.meta.phase = "final"`
    - `data.payload = {"name", "description", "text"}`  
    - `last_chunk = True` → `save_task_result` 호출로 `todolist.draft`에 최종 결과 저장

---

## 3. 툴 호출 / HITL 인터럽트 설계

- **위치**: `deepagents_action_executor.py`
- 목표:
  - **툴 이름/도메인에 의존하지 않는** HITL 설계.
  - MCP 도구는 **그냥 자기 일을 하고**,  
    “사람에게 물어봐야 한다”는 결정을 내리는 계층은 **그래프/서브 에이전트 레이어**.

### 3.1 툴 호출 이벤트

- 함수: `_instrument_tools`
- 래핑 대상: MCP/DeepAgents 툴들의 `invoke` / `ainvoke`

- **tool_usage_started**
  - DB:
    - `event_type = "tool_usage_started"`
  - `data.meta`:
    - `logical_event_type = "tool_call"`
    - `phase = "tool_call"`
    - `tool_name`: 툴 이름
  - `data.payload`:
    - `tool_name`
    - `mcp_server`
    - `input`: JSON-직렬화 가능한 형태로 정규화된 입력

- **tool_usage_finished**
  - DB:
    - `event_type = "tool_usage_finished"`
  - `data.meta`:
    - `logical_event_type = "tool_result"`
    - `phase = "tool_result"`
    - `tool_name`: 툴 이름
  - `data.payload`:
    - `tool_name`, `mcp_server`, `input`, `output`, `error?`
  - 이 시점에 **HITL 신호 추출**도 수행한다(아래 3.2).

- 서브 에이전트 작업 요약:
  - `job_tool_log[job_id]`에 `payload.tool_name`을 기록하여,  
    서브 에이전트 완료 시 `summary` 생성에 활용.

### 3.2 HITL 인터럽트 계약 (`requires_human` 구조)

- MCP 도구는 단순히 자신의 작업 결과를 반환하고,  
  **HITL 여부를 구조적으로 표현하는 책임은 그래프/서브 에이전트 레이어**가 갖는다.

- **HITL 계약 형식 (툴/노드 출력)**:

```jsonc
{
  "requires_human": true,
  "question": "사람에게 직접 물어볼 질문",
  "choices": ["선택지1", "선택지2"],           // 옵션
  "reason": "내부용 설명 (옵션)",
  "input_schema": { ... },                    // 옵션
  "output_schema": { ... },                   // 옵션
  // 기타 원본 페이로드 필드
}
```

- `_extract_hitl_decision_from_output(output: Any) -> dict | None`
  - `output`가 dict이고 `requires_human`이 true인 경우 위 구조로 정규화된 `decision` 반환.

- `_log_finished` 내부:
  - 툴 호출이 성공하고 `tool_output`이 dict일 때 `_extract_hitl_decision_from_output` 호출.
  - `decision`이 존재하면:
    - `HitlInterrupt(decision=..., tool_name=..., job_id=...)` 예외를 발생시킴.

- `DeepAgentsActionExecutor.execute`:
  - `orchestrator.ainvoke(...)` 호출을 `try/except HitlInterrupt`로 감싼다.
  - `HitlInterrupt` 발생 시:
    - DB 이벤트:
      - `event_type = "human_asked"`
      - `data.meta`:
        - `logical_event_type = "interrupt"`
        - `phase = "interrupt"`
        - `requires_human = true`
        - `tool_name = hi.tool_name`
      - `data.payload`:
        - `role = "user"`
        - `text = decision.question` (없으면 기본 한국어 문구)
        - `options = decision.choices or []`
        - `reason = decision.reason`
        - `tool_call_context = { "job_id": hi.job_id, "tool_name": hi.tool_name }`
    - 반환 값:

```jsonc
{
  "status": "waiting_for_human",
  "run_state": {
    "reason": decision.reason or "Tool requested human input.",
    "pending_field": "human_input",
    "correlation_id": exec_ctx.correlation_id,
    "agent_path": [...],
    "tool_name": hi.tool_name,
    "tool_call_context": {
      "job_id": hi.job_id,
      "tool_name": hi.tool_name
    }
  }
}
```

- 상위 레이어는 이 반환 값을 보고:
  - `todolist.draft.run_state`를 저장하고,
  - `draft_status = "HUMAN_ASKED"`로 설정,
  - 동시에 `human_asked` 이벤트를 UI에 노출하여 사람에게 질문/선택지를 보여줄 수 있다.

### 3.3 Pre-run HITL 휴리스틱 (보조 안전망)

- `_should_trigger_human_asked(description, instruction, input_data)`:
  - Description/Instruction 텍스트에서 “승인/검토/민감/개인정보/approval…” 같은 일반 키워드를 감지.
  - InputData가 비어 있고 “추가 정보/입력/정보 제공/데이터”를 언급하는 경우 감지.
  - 이 경우:
    - `human_asked` 이벤트 (`data.meta.logical_event_type = "interrupt"`, `phase="interrupt"`, `requires_human=true`).
    - `run_state`에 `pending_field="input_data"`, `correlation_id`, `agent_path` 등을 포함하여 `waiting_for_human` 반환.
  - 이 로직은 **그래프 레벨 HITL가 없는 상황에서의 안전망**이며, 도메인 특화 키워드에 의존하지 않는다.

---

## 4. HITL 재개(resume) 플로우

- **입력 경로**:
  - 상위 시스템은 기존처럼 `events`에 `event_type="human_response"`, `data={"answer": ...}`를 기록.
  - `database.get_latest_human_response(todo_id)`가 최신 `answer`를 가져온다.

- **ExecutionContext 복원**:
  - `deepagents_action_server._prepare_extras`:
    - `todolist.draft.run_state`를 `extras["run_state"]`로 전달.
    - 있으면 `extras["human_response_answer"]`에 최신 응답을 담는다.
  - `DeepAgentsActionExecutor.execute`:
    - `run_state.correlation_id`, `run_state.agent_path`가 있으면 이를 사용해 `ExecutionContext` 생성.
    - 없으면 새 `correlation_id`를 만들되, 가능한 한 기존 값을 우선시.

- **resume 이벤트 기록**:
  - `human_response_answer`가 존재하면:
    - InputData의 `pending_field`에 사람 응답을 주입.
    - DB `events`에 `event_type="task_working"` 한 건을 기록:
      - `data.meta.logical_event_type = "resume"`
      - `data.meta.phase = "resume"`
      - `data.payload`:
        - `resume_source = "human_response"`
        - `human_answer`
        - `previous_interrupt_agent_path = run_state.agent_path`
        - `previous_interrupt_tool_name = run_state.tool_name`

- 이후 DeepAgents 그래프는 새 InputData를 기반으로 정상 실행을 계속 진행하며,  
  그 과정의 툴 호출/서브 에이전트 실행/최종 결과는 위에서 정의한 표준 이벤트 구조로 기록된다.

---

## 5. 구현/설계 상 주의점 요약

- **도메인 독립성**
  - 어떤 도메인(금융, 법률 등)도 코드 레벨에서 하드코딩하지 않는다.
  - 스킬/툴 이름(`global-investment-analyzer`, `ask_human` 등)에 의존한 분기는 두지 않는다.

- **DB 스키마**
  - `event_type_enum`은 `database.sql`에 정의된 값만 사용하며, 새 enum을 추가하지 않는다.
  - 모든 추가 정보는 **`events.data` JSONB** 안의 `meta` / `payload`로만 표현한다.

- **HITL 설계**
  - MCP 도구는 HITL 구조를 직접 반환할 필요가 없다.  
    “사람에게 물어볼지”를 판단하는 책임은 그래프/서브 에이전트 레이어에 둔다.
  - 하네스는 **`requires_human` 구조 또는 명시적인 계약**만 보고 Execution Interrupt를 수행한다.
  - 순수 자연어(예: `"API 키를 알려 주세요"`)만 보고 일일이 추측하는 휴리스틱은 제거했다.

- **멀티 에이전트 트레이싱**
  - 하나의 `correlation_id` 아래에서:
    - `meta.logical_event_type` / `meta.phase` / `meta.agent_path` / `meta.tool_name`을 기반으로
      - 어떤 에이전트들이 실행되었는지,
      - 어떤 툴들이 호출되었는지,
      - 어디서 HITL interrupt/resume가 일어났는지,
      - 최종 결과/아티팩트가 무엇인지
      를 events 로그만으로 재구성할 수 있다.

