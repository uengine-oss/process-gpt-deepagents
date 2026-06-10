---
name: google-sheets-handler
description: 구글 시트의 데이터를 빠르고 정확하게 조회 및 수정하는 스킬입니다. 데이터 보안과 개인정보 보호를 최우선으로 고려합니다.
---

## 개요

이 스킬은 사용자가 제공한 `credentials.json` 파일을 활용하여 특정 구글 시트의 데이터를 1분 이내에 정확하게 조회하고, 요청에 따라 수정 작업을 수행합니다. 데이터 보안과 개인정보 보호를 최우선으로 고려하며, 사용자가 이해하기 쉬운 말투로 안내합니다.

## 사용 시기

- 구글 시트의 데이터를 빠르게 조회해야 할 때
- 구글 시트의 데이터를 수정해야 할 때
- 데이터 보안과 개인정보 보호가 중요한 경우
- 사용자가 **시트 ID를 모르고 “시트명(제목)”만 알고** 있을 때

## 핵심 원칙

- **데이터 보안**: 모든 작업은 안전한 방식으로 수행되며, 개인정보 보호를 최우선으로 고려합니다.
- **정확성**: 모든 조회 및 수정 작업은 99% 이상의 성공률을 목표로 합니다.
- **사용자 친화적**: 사용자가 이해하기 쉬운 말투로 안내하며, 기술적인 질문에도 전문적으로 답변합니다.

## 단계별 절차

### 1. `credentials.json` 파일 준비

사용자는 구글 API 콘솔에서 `credentials.json` 파일을 다운로드하여 준비합니다. 이 파일은 구글 시트 API에 접근하기 위한 인증 정보를 포함하고 있습니다.

### 1.1 서비스 계정 접근 권한(공유) 확인 (중요)

- **서비스 계정은 권한이 “자동으로” 생기지 않습니다.**
- 조회/수정 대상 스프레드시트(파일)에서, 서비스 계정 이메일(예: `...@...gserviceaccount.com`)에 **공유(뷰어/편집자)**가 되어 있어야 합니다.

### 2. 스크립트 실행

`scripts/sheet_handler.py` 스크립트를 실행하여 구글 시트에 접근합니다. 이 스크립트는 `credentials.json` 파일을 사용하여 인증을 수행합니다.

#### 2.0 실행 예시(권장)

아래 예시는 조회 시 “헤더가 1행이 아닐 수 있는” 시트(상단 안내/공백/병합셀 등)를 포함해 **대부분의 형식을 자동으로 커버**하도록 설계되어 있습니다.

```bash
# 1) 시트명(제목)으로 열기 (ID를 몰라도 됨)
python3 scripts/sheet_handler.py --sheet "Your Google Sheet Name"

# 2) 스프레드시트 URL로 열기
python3 scripts/sheet_handler.py --sheet "https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit"

# 3) 스프레드시트 ID로 열기
python3 scripts/sheet_handler.py --sheet "<SPREADSHEET_ID>"

# 4) 워크시트(탭) 이름 지정
python3 scripts/sheet_handler.py --sheet "Your Google Sheet Name" --worksheet "Sheet1"

# 5) 헤더 행을 명시(자동 탐지가 애매할 때)
python3 scripts/sheet_handler.py --sheet "Your Google Sheet Name" --header-row 27
```

### 2.1 시트 식별자 입력 가이드 (ID를 몰라도 됨)

이 스킬은 사용자가 입력한 값을 아래 우선순위로 해석합니다.

- **스프레드시트 URL**: `.../spreadsheets/d/<ID>` 형태에서 ID를 추출해 엽니다.
- **스프레드시트 ID**: 보통 20자 이상인 영숫자/`_`/`-` 문자열을 ID로 간주해 엽니다.
- **시트명(제목)**: Drive에서 접근 가능한 스프레드시트 목록을 조회한 뒤, 제목을 매칭해 엽니다.

제목이 **중복**되거나 **유사 후보가 여러 개**면, 후보(제목 + ID)를 출력하므로 다음 시도에서 ID로 지정하면 빠르게 해결됩니다.

### 2.2 헤더(컬럼명) 자동 탐지 원칙 (형식이 제각각인 시트 대응)

현실의 시트는 다음처럼 “표가 1행부터 시작하지 않는” 경우가 많습니다.

- 상단에 안내/설명 영역이 있고, 실제 표는 중간 행부터 시작
- 헤더 행에 빈 칸이 있거나, 같은 컬럼명이 반복(중복)
- 표 좌측에 공백 열이 있거나(예: A열 비고정), 데이터 영역이 오른쪽에서 시작

이 스킬은 기본적으로 **전체 값을 읽은 뒤 헤더로 보이는 행을 자동 탐지**하고, 다음 보정도 수행합니다.

- 헤더 셀이 비어 있으면 `col_1`, `col_2`…처럼 자동 이름을 부여
- 헤더가 중복되면 `제목`, `제목_2`…처럼 중복을 해소

자동 탐지가 애매하면 `--header-row`로 **헤더 행을 강제 지정**하는 것을 권장합니다.

### 2.3 OAuth Scopes 설정 원칙 (실패의 1순위 원인)

스코프가 누락되거나 잘못 설정되면, API 호출 이력이 남더라도 실제 데이터 조회/수정이 **`insufficient authentication scopes` / 403** 등으로 실패할 수 있습니다.

- **조회 전용(권장 기본)**:
  - `https://www.googleapis.com/auth/spreadsheets.readonly`
  - (**시트명(제목) 매칭/목록 조회가 필요하면 필수**) `https://www.googleapis.com/auth/drive.readonly`
- **수정 필요 시**:
  - `https://www.googleapis.com/auth/spreadsheets`
  - (시트명(제목) 매칭/파일 탐색이 필요하면) `https://www.googleapis.com/auth/drive`

특히 다음 두 생성자는 스코프 적용 방식이 다릅니다.

- `Credentials.from_service_account_file(..., scopes=scopes)` ✅
- `Credentials.from_service_account_info(..., scopes=scopes)` ✅ (**scopes 인자 누락 금지**)

### 3. 데이터 조회 및 수정

- **데이터 조회**: 특정 구글 시트의 데이터를 조회합니다. 조회된 데이터는 사용자가 이해하기 쉽게 출력됩니다.
- **데이터 수정**: 요청에 따라 구글 시트의 데이터를 수정합니다. 모든 수정 작업은 정확성을 보장하기 위해 확인 절차를 거칩니다.

## 추가 파일

- `scripts/sheet_handler.py`: 구글 시트의 데이터를 조회하고 수정하는 스크립트입니다. `credentials.json` 파일을 사용하여 인증을 수행합니다.

## 예시 (스코프 적용이 빠지지 않게)

아래 예시는 “서비스 계정 정보(dict)”를 이미 메모리에서 가지고 있을 때도 **반드시 scopes를 적용**하는 형태입니다.

```python
from google.oauth2.service_account import Credentials
import gspread

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    # 필요 시만:
    # "https://www.googleapis.com/auth/drive.readonly",
]

creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
client = gspread.authorize(creds)
```

## 자주 발생하는 문제(빠른 점검)

- **목록은 나오는데 원하는 시트가 없다**
  - 서비스 계정 이메일이 해당 스프레드시트에 **공유**되어 있는지 확인하세요.
- **목록 조회에서 403이 난다**
  - `drive.readonly`(또는 `drive`) 스코프가 빠졌을 가능성이 큽니다.
- **제목 매칭이 애매하다**
  - 출력된 후보의 **ID**로 다시 지정하면 즉시 해결됩니다.
- **헤더가 이상해서 레코드 변환이 깨진다(컬럼명이 비어있거나 안내문이 헤더로 잡힘)**
  - `--header-row`로 실제 헤더 행을 지정해 보세요. (예: `--header-row 27`)
