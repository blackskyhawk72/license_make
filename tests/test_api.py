"""API 흐름 테스트. DB는 메모리 가짜로 바꾸고, 판정 CLI는 실제 subprocess로 돌린다."""

import os
import tempfile
from pathlib import Path

# app.main은 import 시점에 환경변수를 읽는다 — import 전에 설정한다.
_tmp = Path(tempfile.mkdtemp())
os.environ.update({
    "LICENSE_PUBLIC_KEY_PATH": str(_tmp / "pub.pem"),
    "LICENSE_PRODUCT_ID": "demo",
    "LICENSE_PC_BIND_SECRET": "test-secret",
})

from license_kit import generate_master_keypair, sign_license  # noqa: E402

generate_master_keypair(_tmp / "priv.pem", _tmp / "pub.pem")

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

KID = "kid0000000000001"
KEY = sign_license({"product": "demo", "kid": KID}, _tmp / "priv.pem")


class FakeDb:
    def __init__(self):
        self.rows = {KID: {"KID": KID, "ENC_PC_BIND": None, "ACTV_DT": None, "PROD_CD": "demo",
                           "EXPR_DT": "2999-12-31T00:00:00", "RVK_DT": None}}
        self.log = []

    def fetch_record(self, kid):
        row = self.rows.get(kid)
        return dict(row) if row else None

    def activate(self, kid, enc):
        row = self.rows[kid]
        if row["ENC_PC_BIND"] is not None:
            return False
        row["ENC_PC_BIND"], row["ACTV_DT"] = enc, "2026-10-14T00:00:00"
        return True

    def log_result(self, kid, result):
        self.log.append((kid, result))


def _client(fake):
    for name in ("fetch_record", "activate", "log_result"):
        setattr(main.db, name, getattr(fake, name))
    return TestClient(main.app)  # lifespan 미실행(컨텍스트 매니저 미사용) → 실제 DB 풀 안 만든다


def _post(c, path, pc, key=KEY):
    return c.post(path, json={"license_key": key, "pc_uuid": pc}).json()


def test_verify_does_not_bind_and_register_does():
    fake = FakeDb()
    c = _client(fake)

    r = _post(c, "/verify", "PC-A")
    assert r["valid"] is False and r["error"] == "not_registered"
    assert fake.rows[KID]["ENC_PC_BIND"] is None  # verify는 DB를 바꾸지 않는다

    r = _post(c, "/register", "PC-A")
    assert r["valid"] is True and r["kid"] == KID
    assert len(fake.rows[KID]["ENC_PC_BIND"]) == 64

    assert _post(c, "/verify", "PC-A")["valid"] is True
    assert _post(c, "/register", "PC-A")["valid"] is True  # 멱등
    assert _post(c, "/verify", "PC-B")["error"] == "pc_mismatch"
    assert _post(c, "/register", "PC-B")["error"] == "pc_mismatch"  # PC 교체 불가
    assert [x[1] for x in fake.log] == [
        "not_registered", "reg_ok", "ok", "reg_ok", "pc_mismatch", "pc_mismatch",
    ]


def test_lost_registration_race_is_revalidated():
    fake = FakeDb()
    c = _client(fake)
    real_activate = fake.activate

    def racing_activate(kid, enc):
        fake.rows[kid]["ENC_PC_BIND"] = "f" * 64  # 다른 요청이 먼저 바인딩
        return real_activate(kid, enc)

    main.db.activate = racing_activate
    r = _post(c, "/register", "PC-A")
    assert r["valid"] is False and r["error"] == "pc_mismatch"


def test_forged_signature_is_not_logged_against_real_kid():
    fake = FakeDb()
    c = _client(fake)
    for path in ("/verify", "/register"):
        assert _post(c, path, "PC-A", key="bm90LWEta2V5")["error"] == "signature"
    assert fake.log == []


def test_unknown_kid_and_revoked_and_validation():
    fake = FakeDb()
    c = _client(fake)
    fake.rows[KID]["RVK_DT"] = "2026-10-01T00:00:00"
    assert _post(c, "/register", "PC-A")["error"] == "revoked"
    assert fake.rows[KID]["ENC_PC_BIND"] is None  # 폐기된 키는 바인딩되지 않는다

    fake.rows.pop(KID)
    assert _post(c, "/register", "PC-A")["error"] == "unknown_kid"
    assert fake.log == [(KID, "revoked")]  # DB에 없는 kid는 이력에 남기지 않는다

    assert c.post("/register", json={"license_key": "", "pc_uuid": "x"}).status_code == 422
    assert c.post("/register", json={"license_key": KEY}).status_code == 422
