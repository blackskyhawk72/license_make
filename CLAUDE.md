# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

언어/주석/커밋 규칙은 [.claude/CLAUDE.md](.claude/CLAUDE.md)를 따른다(한국어, 식별자는 영어).

## 개요

라이선스 **검증 전용** 초경량 FastAPI 서버. 판정 로직은 이 저장소에 없고, 별도 프로젝트 `license-kit`
(`D:\projects\라이센스 작업용 파일들\license관련\license-kit`)의 CLI(`python -m license_kit.cli`)에 위임한다.
발급은 `license-tools` 프로젝트(별도, 비밀키 보유)가 담당하며 이 서버와 무관하다 — 이 서버는 **공개키만** 가진다.

## 명령어

```powershell
# 최초 1회: .venv 생성 후 의존성 + license-kit(CLI/테스트용) 설치
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt python-dotenv pytest httpx
.\.venv\Scripts\pip install -e "D:\projects\라이센스 작업용 파일들\license관련\license-kit"

.\run.ps1                                      # 서버 실행(.env 로드, 기본 127.0.0.1:8000)
.\run.ps1 -BindHost 0.0.0.0 -Port 8000         # 외부 노출 시

.\.venv\Scripts\python -m pytest tests -q      # 프로젝트 루트에서 실행 (app 패키지 import에 cwd 필요)
.\.venv\Scripts\python -m pytest tests/test_api.py::test_verify_does_not_bind_and_register_does
```

설정은 `.env`(gitignore 대상, 양식은 [.env.example](.env.example))로 둔다. 필수(없으면 기동 실패):
`LICENSE_PUBLIC_KEY_PATH`, `LICENSE_PC_BIND_SECRET`, `ORACLE_DSN`, `ORACLE_USER`, `ORACLE_PASSWORD`.
그 외 옵션은 [app/main.py](app/main.py) 상단 docstring 참고. 접속 계정과 `DLM_*` 테이블 소유 스키마가 다르면
`ORACLE_SCHEMA`를 지정한다(세션 `current_schema`로 설정 — 쿼리에 스키마 접두사를 박지 않는다).
`tests/test_api.py`는 DB를 메모리 가짜로 바꾸므로 Oracle 없이 돌지만, 판정 CLI는 실제 subprocess로 실행된다.

## 아키텍처

엔드포인트는 둘이고 같은 핸들러(`_handle`)를 `--mode`만 달리해서 쓴다:
- `POST /verify` (mode=verify): **조회 전용.** 미바인딩 키는 `not_registered`로 거부하고 DB를 바꾸지 않는다.
- `POST /register` (mode=register): 미바인딩 키를 요청 PC에 바인딩. 같은 PC면 멱등 성공, 다른 PC면
  `pc_mismatch`. **PC 교체(재바인딩)는 이 서버의 책임이 아님** — 발급 도구가 `ENC_PC_BIND`를 비운다.

처리 흐름은 여러 파일에 걸쳐 있다:

1. [app/main.py](app/main.py): 키에서 **서명 검증 없이** kid만 추출(`peek_kid`) → `DLM_L010M` 레코드 조회에만 사용.
2. 서버가 `{license_key, pc_uuid, record, today}`를 JSON으로 CLI의 stdin에 전달(키를 argv에 넣지 않기 위함).
3. CLI(license-kit의 `server_verify.verify_against_record`)가 서명 → product → 레코드 존재 → 만료 → 폐기 →
   PC 바인딩 순으로 판정하고, register 모드에서 바인딩이 필요하면 `bind_action`(DB에 반영할 변경)을 돌려준다.
   **CLI는 DB를 모른다.**
4. 서버가 `bind_action`을 [app/db.py](app/db.py)의 `activate()`로 원자적으로 반영하고(`ENC_PC_BIND IS NULL`
   조건부 UPDATE + `DLM_L011H` 이력), 경쟁에서 지면 최신 레코드로 CLI를 한 번 더 호출해 재판정한다.
5. `DLM_L020H`에 결과 기록(best-effort; 등록 성공은 `reg_ok`, 검증 성공은 `ok`), 응답 반환.

## 지켜야 할 규칙

- **서명에는 `kid` + `product`만 신뢰한다.** 만료(`EXPR_DT`)/폐기(`RVK_DT`)/PC 바인딩(`ENC_PC_BIND`)은 전부 DB 기준.
  만료는 `EXPR_DT`의 **날짜 부분만** 보고 당일까지 유효.
- 서명이 확정되지 않은 결과(`signature`, `malformed_policy`)와 DB에 없는 kid는 `DLM_L020H`에 **기록하지 않는다**
  — peek한 kid는 위조 가능하므로 남의 KID 이력을 오염시킬 수 있다.
- 에러 코드는 `DLM_L020H.VRFY_RSLT VARCHAR2(16)`에 그대로 들어가므로 **16자 이내**여야 한다.
- `ENC_PC_BIND`는 `HMAC-SHA256(LICENSE_PC_BIND_SECRET, pc_uuid)`의 64자 hex. **비밀값을 바꾸면 기존 바인딩이
  전부 `pc_mismatch`가 된다.**
- DB 계정에 필요한 권한은 최소한이다: `DLM_L010M` SELECT + `UPDATE(ENC_PC_BIND, ACTV_DT)`, `DLM_L011H`/`DLM_L020H` INSERT.
  이력의 `UPDT_ID`는 `SYSTEM`(NOT NULL 컬럼용).
- 이 서버는 `license_kit`을 import하지 않는다(CLI와 분리 유지). 검증 로직을 서버에 복사하지 말 것.
