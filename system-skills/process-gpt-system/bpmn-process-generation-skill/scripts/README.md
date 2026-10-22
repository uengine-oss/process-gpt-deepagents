# scripts — 정의 변환 · 정적 검증 보조

이 스킬은 어떤 모드에서도 **DB 에 쓰지 않는다.** 산출물은 `.bpmn/` 폴더의 파일로만 만들고,
임시저장(draft)·검증·최종 저장은 **프론트가 사용자 자격증명으로** 수행한다.

- **임시저장(draft)** : 프론트가 `proc_def`(`is_draft=true`)·`form_def`·`users` 에 기록
- **실행엔진 검증** : 프론트가 `/validate-and-improve` 호출 → 완료엔진이 draft 를 구동·자동교정
- **최종 저장** : 사용자가 [저장] 버튼을 누르면 `is_draft=false` 로 승격 + `tenants.skills`·`proc_map` 등록

deepagent 쪽은 턴이 끝나면 executor 가 여기 있는 모듈로 **파일 기준 정적 검증 + LLM 자동교정**만 한다
(에이전트에게는 검증/완료 도구를 바인딩하지 않는다).

## 구성

| 파일 | 역할 |
|------|------|
| `save_to_supabase.py` | 순수 변환 함수. `flatten()`(elements[]→flattened), `html_to_fields_json()`. **DB 접근 없음** |
| `validate_process.py` | 정적 결함 자동교정에 쓰는 LLM 호출자(`_make_llm_call`). **DB 접근 없음** |
| `validation/process_validator.py` | **pdf2bpmn 에서 그대로 vendoring** 한 검증 엔진 (수정 금지). deepagent 는 이 중 `_static_check` 만 사용 |
| `requirements.txt` | openai / anthropic (자동교정 LLM, 선택) |

> 파일명 `save_to_supabase.py` 는 기존 import 경로 호환을 위해 유지한다. 더 이상 저장하지 않는다.

## 환경변수

deepagent 의 Docker 샌드박스(`core/sandbox/docker_sandbox.py`)는 **Supabase 자격증명을 컨테이너로
전달하지 않는다.** 스킬이 DB 에 직접 쓸 수 없도록 구조적으로 막기 위함이다.

| 변수 | 용도 |
|------|------|
| `TENANT_ID` | 요청 tenant (deepagent 가 자동 주입) |
| `LLM_PROXY_URL` + `LLM_PROXY_API_KEY` (+`LLM_MODEL`) | 자동교정 LLM (OpenAI 호환 프록시, 1순위) |
| `ANTHROPIC_API_KEY` | 자동교정 LLM 2순위 |
| `OPENAI_API_KEY` | 자동교정 LLM 3순위 (셋 다 없으면 교정 없이 검증만) |

> LLM 우선순위는 deepagent `core/model.py` 와 동일: `LLM_PROXY_URL`+`LLM_PROXY_API_KEY` → `ANTHROPIC_API_KEY` → `OPENAI_API_KEY`. 모델명은 `LLM_MODEL`.

## flattened 변환 메모

우리 스킬의 `processDefinition` 은 `elements[]` 형식이다. `save_to_supabase.flatten()` 이
이를 ProcessGPT 가 소비하는 flattened 형식(`activities`/`sequences`/`gateways`/`events`/`roles`
분리 배열, `type`=userTask/startEvent/exclusiveGateway 등, `properties`=JSON 문자열)으로
변환한다. pdf2bpmn 의 `proc_def.definition` 형태와 동일하다.
