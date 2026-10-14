"""라이선스 검증 요청을 받아 DB 레코드를 조회하고, 판정은 CLI(license_kit.cli)에 위임하는 초경량 서버.

엔드포인트: POST /verify(조회 전용), POST /register(PC 바인딩 등록), GET /health.

판정 로직은 이 프로젝트에 없다. 이 파일이 하는 일: 입력 검증 → DB 조회 → CLI 호출 →
(등록이면) DB 반영 → 검증 이력 기록 → 응답.

환경변수:
  LICENSE_PUBLIC_KEY_PATH  (필수) 공개키 PEM 경로
  LICENSE_PRODUCT_ID       기본 product id(요청의 product_id로 덮어쓰기 가능, 둘 중 하나는 필수)
  LICENSE_PC_BIND_SECRET   (필수) PC 바인딩 해시용 비밀값 — 바꾸면 기존 바인딩이 전부 불일치가 된다
  ORACLE_DSN / ORACLE_USER / ORACLE_PASSWORD  (필수) DB 접속 정보, ORACLE_POOL_MAX(기본 4)
  ORACLE_SCHEMA            (선택) 테이블 소유 스키마. 접속 계정과 다를 때 current_schema로 설정한다.
                           생략하면 접속 계정 스키마를 그대로 쓴다.
  LICENSE_CLI_CMD          CLI 실행 명령. 기본: "<현재 python> -m license_kit.cli"
  LICENSE_CLI_TIMEOUT      CLI 타임아웃(초), 기본 30
  LICENSE_MAX_CONCURRENCY  동시 CLI 실행 수, 기본 4
"""

import asyncio
import base64
import json
import logging
import os
import shlex
import sys
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import db

logger = logging.getLogger("license-api")

PUBLIC_KEY_PATH = os.environ.get("LICENSE_PUBLIC_KEY_PATH")
DEFAULT_PRODUCT_ID = os.environ.get("LICENSE_PRODUCT_ID")
CLI_TIMEOUT = float(os.environ.get("LICENSE_CLI_TIMEOUT", "30"))
_sem = asyncio.Semaphore(int(os.environ.get("LICENSE_MAX_CONCURRENCY", "4")))

# 서명이 확정되지 않은 결과는 KID를 신뢰할 수 없으므로(위조 키로 남의 KID 이력을 오염시킬
# 수 있다) 검증 이력에 남기지 않는다.
_UNTRUSTED_ERRORS = {"signature", "malformed_policy"}


def _cli_cmd() -> list:
    raw = os.environ.get("LICENSE_CLI_CMD")
    if not raw:
        return [sys.executable, "-m", "license_kit.cli"]
    # Windows 경로의 백슬래시를 보존하기 위해 non-posix 분할 후 따옴표만 제거한다.
    return [t.strip('"') for t in shlex.split(raw, posix=False)]


def peek_kid(license_key: str) -> Optional[str]:
    """서명 검증 없이 키에서 kid만 꺼낸다 — DB 레코드를 찾는 용도로만 쓰고 신뢰하지 않는다
    (판정은 CLI가 서명을 검증한 뒤 레코드의 KID와 대조해서 한다). 실패하면 None."""
    try:
        decoded = json.loads(base64.b64decode(license_key.encode("utf-8"), validate=True))
        kid = json.loads(decoded["data"]).get("kid")
    except Exception:
        return None
    return kid if isinstance(kid, str) and 0 < len(kid) <= 128 else None


@asynccontextmanager
async def lifespan(_app):
    missing = [
        n for n in ("LICENSE_PUBLIC_KEY_PATH", "LICENSE_PC_BIND_SECRET",
                    "ORACLE_DSN", "ORACLE_USER", "ORACLE_PASSWORD")
        if not os.environ.get(n)
    ]
    if missing:
        raise RuntimeError(f"필수 환경변수 누락: {', '.join(missing)}")
    await asyncio.to_thread(db.init_pool)
    yield
    await asyncio.to_thread(db.close_pool)


class VerifyRequest(BaseModel):
    # license_kit의 키 길이 상한(20,000)과 동일 — 거대한 입력은 CLI를 띄우기 전에 차단.
    license_key: str = Field(min_length=1, max_length=20_000)
    # 클라이언트 PC의 원본 식별값(license_kit.get_pc_uuid()). 서버가 HMAC으로 변환해 저장한다.
    pc_uuid: str = Field(min_length=1, max_length=128)
    product_id: Optional[str] = Field(default=None, min_length=1, max_length=200)


class VerifyResponse(BaseModel):
    valid: bool
    error: Optional[str] = None
    days_left: Optional[int] = None
    warn: bool = False
    product: Optional[str] = None
    activated_at: Optional[str] = None
    kid: Optional[str] = None


app = FastAPI(title="License Verify API", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


async def _run_cli(
    license_key: str, pc_uuid: str, product_id: str, record: Optional[dict], mode: str,
) -> dict:
    cmd = [*_cli_cmd(), "--public-key", PUBLIC_KEY_PATH, "--product", product_id, "--mode", mode]
    payload = json.dumps({
        "license_key": license_key,
        "pc_uuid": pc_uuid,
        "record": record,
        "today": date.today().isoformat(),
    }).encode("utf-8")

    async with _sem:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(payload), timeout=CLI_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HTTPException(504, "verification timed out")
        except OSError:
            logger.exception("CLI 실행 실패: %s", cmd[0])
            raise HTTPException(502, "verifier unavailable")

    # 검증 실패(exit 1)도 정상 응답이므로 종료 코드가 아니라 stdout JSON 유무로 판단한다.
    try:
        return json.loads(stdout.decode("utf-8"))
    except Exception:
        logger.error("CLI 출력 파싱 실패 (exit=%s): %s", proc.returncode,
                     stderr.decode(errors="replace")[-500:])
        raise HTTPException(502, "verifier returned an invalid response")


async def _db(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except Exception:
        logger.exception("DB 오류: %s", fn.__name__)
        raise HTTPException(503, "database unavailable")


@app.post("/verify", response_model=VerifyResponse)
async def verify(req: VerifyRequest):
    """조회 전용 — 아직 PC에 등록되지 않은 키는 error=not_registered로 거부한다."""
    return await _handle(req, "verify")


@app.post("/register", response_model=VerifyResponse)
async def register(req: VerifyRequest):
    """키를 이 PC에 바인딩한다(미바인딩 키에만). 이미 같은 PC면 멱등하게 성공, 다른 PC면
    pc_mismatch. PC 교체(재바인딩)는 이 서버의 책임이 아니다 — 발급 도구에서 처리한다."""
    return await _handle(req, "register")


async def _handle(req: VerifyRequest, mode: str) -> VerifyResponse:
    product_id = req.product_id or DEFAULT_PRODUCT_ID
    if not PUBLIC_KEY_PATH or not product_id:
        logger.error("LICENSE_PUBLIC_KEY_PATH / LICENSE_PRODUCT_ID 미설정")
        raise HTTPException(500, "server misconfigured")

    # kid는 서명 검증 전의 값이라 레코드 조회에만 쓰고, 판정은 CLI가 서명 검증 후 대조한다.
    kid = peek_kid(req.license_key)
    record = await _db(db.fetch_record, kid) if kid else None

    result = await _run_cli(req.license_key, req.pc_uuid, product_id, record, mode)

    action = result.pop("bind_action", None)
    if action:
        if not await _db(db.activate, kid, action["enc_pc_bind"]):
            # 동시 요청이 먼저 바인딩했다 — 최신 레코드로 한 번 다시 판정한다
            # (같은 PC면 통과, 다른 PC면 pc_mismatch).
            record = await _db(db.fetch_record, kid)
            result = await _run_cli(req.license_key, req.pc_uuid, product_id, record, mode)
            if result.pop("bind_action", None):
                raise HTTPException(503, "activation conflict")

    # 이력 기록 실패가 응답을 막으면 안 된다(best-effort). 등록 성공은 일반 검증 성공("ok")과
    # 구분되도록 "reg_ok"로 남긴다(VRFY_RSLT는 16자 이내).
    if record is not None and result.get("error") not in _UNTRUSTED_ERRORS:
        ok_label = "reg_ok" if mode == "register" else "ok"
        try:
            await asyncio.to_thread(db.log_result, kid, ok_label if result["valid"] else result["error"])
        except Exception:
            logger.exception("검증 이력 기록 실패: kid=%s", kid)

    return VerifyResponse(**result)
