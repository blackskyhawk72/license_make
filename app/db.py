"""Oracle 접근 계층. 동기 함수(python-oracledb thin) — 서버에서 asyncio.to_thread로 호출한다.

이 서버가 쓰는 테이블/컬럼은 최소한이다(DB 계정 권한도 이것만 주는 것을 권장):
  DLM_L010M : SELECT, UPDATE (ENC_PC_BIND, ACTV_DT)
  DLM_L011H : INSERT
  DLM_L020H : INSERT
"""

import os
import re
from datetime import datetime
from typing import Optional

import oracledb

SYSTEM_UPDT_ID = "SYSTEM"  # DLM_L011H.UPDT_ID(NOT NULL) — 서버가 자동 반영한 변경의 주체

_pool: Optional[oracledb.ConnectionPool] = None


_SCHEMA_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")


def init_pool() -> None:
    """커넥션 풀을 만든다. ORACLE_SCHEMA가 있으면 새 세션마다 current_schema를 그 값으로 맞춘다.

    접속 계정과 테이블 소유 스키마가 다를 때(예: EPLSADM으로 접속, 테이블은 DOBSLIC 소유)
    쿼리에 스키마 접두사를 박지 않고도 DLM_* 테이블을 그대로 참조하기 위함이다. 접속 계정에는
    해당 스키마 테이블에 대한 권한(GRANT)이 따로 있어야 한다.
    """
    global _pool
    kwargs = {}
    schema = os.environ.get("ORACLE_SCHEMA", "").strip()
    if schema:
        if not _SCHEMA_PATTERN.match(schema):
            raise RuntimeError("ORACLE_SCHEMA 형식이 올바르지 않습니다(영문/숫자/_/$/# 만 허용).")

        # session_callback은 풀이 새 세션을 만들 때만 호출된다 — acquire마다 설정하지 않는다.
        def _set_schema(conn, _requested_tag):
            conn.current_schema = schema

        kwargs["session_callback"] = _set_schema

    _pool = oracledb.create_pool(
        user=os.environ["ORACLE_USER"],
        password=os.environ["ORACLE_PASSWORD"],
        dsn=os.environ["ORACLE_DSN"],
        min=1,
        max=int(os.environ.get("ORACLE_POOL_MAX", "4")),
        increment=1,
        **kwargs,
    )


def close_pool() -> None:
    if _pool is not None:
        _pool.close()


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def fetch_record(kid: str) -> Optional[dict]:
    """DLM_L010M에서 kid 한 행을 읽어 JSON 직렬화 가능한 dict로 반환(없으면 None)."""
    with _pool.acquire() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT KID, ENC_PC_BIND, ACTV_DT, PROD_CD, EXPR_DT, RVK_DT "
            "FROM DLM_L010M WHERE KID = :kid",
            kid=kid,
        )
        row = cur.fetchone()
        if row is None:
            return None
        names = [d[0] for d in cur.description]
        return {n: _iso(v) for n, v in zip(names, row)}


def activate(kid: str, enc_pc_bind: str) -> bool:
    """최초 활성화를 원자적으로 반영한다. 이미 다른 요청이 바인딩했으면 False.

    ENC_PC_BIND IS NULL 조건부 UPDATE라서 동시에 두 PC가 같은 키를 쓰려 해도 한쪽만
    성공한다. 같은 트랜잭션에서 수정 이력(DLM_L011H)도 남긴다 — UPDATE가 행 락을 잡고
    있어 SEQ(MAX+1)가 같은 kid에 대해 충돌하지 않는다.
    """
    with _pool.acquire() as conn, conn.cursor() as cur:
        try:
            cur.execute(
                "UPDATE DLM_L010M SET ENC_PC_BIND = :b, ACTV_DT = SYSTIMESTAMP "
                "WHERE KID = :k AND ENC_PC_BIND IS NULL",
                b=enc_pc_bind, k=kid,
            )
            if cur.rowcount == 0:
                conn.rollback()
                return False
            cur.execute(
                "INSERT INTO DLM_L011H ("
                " KID, SEQ, PRV_ENC_PC_BIND, PRV_ACTV_DT, PRV_PROD_CD, PRV_CUST_CD,"
                " PRV_SEAT_LABL, PRV_ISSD_DT, PRV_EXPR_DT, PRV_KEY_PRFX, PRV_RVK_DT, PRV_RVK_RSN,"
                " ENC_PC_BIND, ACTV_DT, PROD_CD, CUST_CD, SEAT_LABL, ISSD_DT, EXPR_DT,"
                " KEY_PRFX, RVK_DT, RVK_RSN, UPDT_ID) "
                "SELECT KID, (SELECT NVL(MAX(SEQ), 0) + 1 FROM DLM_L011H WHERE KID = :k),"
                " NULL, NULL, PROD_CD, CUST_CD, SEAT_LABL, ISSD_DT, EXPR_DT, KEY_PRFX, RVK_DT, RVK_RSN,"
                " ENC_PC_BIND, ACTV_DT, PROD_CD, CUST_CD, SEAT_LABL, ISSD_DT, EXPR_DT,"
                " KEY_PRFX, RVK_DT, RVK_RSN, :u "
                "FROM DLM_L010M WHERE KID = :k",
                k=kid, u=SYSTEM_UPDT_ID,
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def log_result(kid: str, result: str) -> None:
    """DLM_L020H에 검증 결과 한 줄을 남긴다. VRFY_RSLT는 VARCHAR2(16)."""
    with _pool.acquire() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO DLM_L020H (KID, VRFY_RSLT) VALUES (:k, :r)",
            k=kid, r=result[:16],
        )
        conn.commit()
