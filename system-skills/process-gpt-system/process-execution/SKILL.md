---
name: process-execution
description: >
  이미 만들어 둔 프로세스를 지금 대화 안에서 실행(새 인스턴스 시작)할 때 쓴다. "휴가 신청 프로세스 실행해줘",
  "도서 대출 신청 시작해줘", "출장 신청 올려줘", "○○ 프로세스 돌려줘" 같은 요청. 역할 배정을 한 번에 하나씩 묻고,
  시작 폼을 받아 execute_process 로 한 번만 시작한다. 프로세스를 새로 설계·생성하는 요청에는 쓰지 않는다.
metadata:
  include_tools: execute_process
---

# 기존 프로세스 실행

이미 있는 프로세스를 실행해 달라고 하면 **정의·실행 화면으로 보내지 말고 지금 대화 안에서 끝낸다.**

## 순서

get_process_list → get_process_detail → (역할 배정) → get_form_fields → execute_process

- get_process_detail 결과의 start_activities 가 **시작 단계**다. get_form_fields 와 execute_process 는
  이 시작 단계의 form_key · activity_id 로만 부른다. 뒤 단계(시스템 작업·검토 등)의 폼은 조회하지 않는다.
- definition.roles 순서대로 **한 번에 하나씩** 묻는다. 질문은 반드시
  "{순번}번 {역할명} 역할은 누구로 설정하시겠습니까?" 형식을 쓴다.
  사용자가 이미 정해 준 역할(예: "역할은 모두 나로")은 다시 묻지 않는다.
- 담당자 후보가 필요하면 get_organization 으로 조회해 선택지를 함께 보인다.
- 모든 역할이 정해진 뒤에 시작 단계의 get_form_fields 를 호출한다. 폼 값은 화면이 입력란으로
  그려 주므로 항목을 하나씩 되묻지 않고, 입력 폼을 채워 제출해 달라고만 말하고 턴을 마친다.
- 단, 사용자가 요청에 첫 폼 값을 이미 다 주었으면(예: '요청 내용은 ○○') 폼을 다시 받지 않는다.
  get_form_fields 로 필드 키만 확인하고 그 값으로 바로 execute_process 를 호출한다.
- 사용자가 폼을 제출하면 모아 둔 역할 배정을 함께 넣어 execute_process 를 바로 호출한다.

## 실행은 한 번만

- **execute_process 는 요청 한 건당 정확히 한 번**이다. 인스턴스가 시작되면 이후 단계는 엔진이
  알아서 진행하므로, 뒤 단계의 activity_id 로 execute_process 를 다시 부르지 않는다(부르면 별개의 인스턴스가
  생긴다). 결과를 확인하려고 다시 부르지도 않는다. already_started 응답이면 새로 실행하지 않은 것이다.
- execute_process 결과에 process_instance_id 가 오기 전에는 **절대 '실행했습니다' 라고 말하지 않는다.**
  도구를 부르지 않았거나 오류가 났으면 그대로 알린다.
- 실행에 성공하면 링크나 인스턴스 ID 를 안내하지 않는다. 화면이 '프로세스가 실행되었습니다' 카드를
  보여 주고, 카드를 누르면 인스턴스 대화로 간다. 무엇을 시작했는지 한두 문장으로만 알리고 끝낸다.
