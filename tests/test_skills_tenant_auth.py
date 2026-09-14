"""/skills* 테넌트 접근 제어 테스트 (processgpt_agent_sdk.tenant_auth).

과거에는 요청 파라미터 tenant_id 를 그대로 믿어서, 값만 바꾸면 다른 테넌트의
스킬 디렉토리·git 자격증명에 접근할 수 있었다. 여기서는 그 경로가 막혔는지,
그리고 정상 사용자는 그대로 통과하는지를 확인한다.
"""

import time
from unittest.mock import MagicMock, patch

import jwt
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from processgpt_agent_sdk import tenant_auth
from core.api.skills_router import routes

pytestmark = pytest.mark.real_tenant_auth

SECRET = "test-jwt-secret-0123456789abcdef-32b"
USER_A = "00000000-0000-0000-0000-00000000000a"
USER_B = "00000000-0000-0000-0000-00000000000b"


@pytest.fixture
def client():
    return TestClient(Starlette(routes=routes), raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def jwt_secret(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    tenant_auth.clear_caches()
    yield
    tenant_auth.clear_caches()


def _token(user_id: str, tenant_id: str | None, *, expires_in: int = 3600, secret: str = SECRET) -> str:
    claims: dict = {"sub": user_id, "exp": int(time.time()) + expires_in}
    if tenant_id is not None:
        claims["app_metadata"] = {"tenant_id": tenant_id}
    return jwt.encode(claims, secret, algorithm="HS256")


def _auth(user_id: str, tenant_id: str | None = "tenant-a") -> dict:
    return {"Authorization": f"Bearer {_token(user_id, tenant_id)}"}


class TestTenantIsolation:
    def test_no_token_is_rejected(self, client):
        resp = client.get("/skills?tenant_id=tenant-a")
        assert resp.status_code == 401

    def test_other_tenant_is_forbidden(self, client):
        """핵심 회귀 테스트 — 파라미터만 바꿔 남의 테넌트를 조회할 수 없어야 한다."""
        resp = client.get("/skills?tenant_id=tenant-b", headers=_auth(USER_A, "tenant-a"))
        assert resp.status_code == 403

    def test_own_tenant_is_allowed(self, client):
        with patch("core.api.skills_router._scan_skills", return_value=[]) as scan:
            resp = client.get("/skills?tenant_id=tenant-a", headers=_auth(USER_A, "tenant-a"))
        assert resp.status_code == 200
        scan.assert_called_once_with("tenant-a")

    def test_tenant_id_omitted_falls_back_to_token_tenant(self, client):
        with patch("core.api.skills_router._scan_skills", return_value=[]) as scan:
            resp = client.get("/skills", headers=_auth(USER_A, "tenant-a"))
        assert resp.status_code == 200
        scan.assert_called_once_with("tenant-a")

    def test_body_tenant_id_is_checked(self, client):
        """쿼리스트링뿐 아니라 JSON 본문의 tenant_id 도 같은 규칙을 받는다."""
        resp = client.request(
            "DELETE",
            "/skills/some-skill",
            json={"tenant_id": "tenant-b", "mode": "local"},
            headers=_auth(USER_A, "tenant-a"),
        )
        assert resp.status_code == 403

    def test_body_tenant_id_allowed_for_own_tenant(self, client):
        """본문 tenant_id 가 내 테넌트면 통과해 핸들러까지 도달한다(스킬 없음 → 404)."""
        resp = client.request(
            "DELETE",
            "/skills/no-such-skill",
            json={"tenant_id": "tenant-a", "mode": "local"},
            headers=_auth(USER_A, "tenant-a"),
        )
        assert resp.status_code == 404

    def test_form_tenant_id_is_checked(self, client):
        resp = client.post(
            "/skills/upload",
            data={"tenant_id": "tenant-b"},
            files={"file": ("x.zip", b"PK\x03\x04", "application/zip")},
            headers=_auth(USER_A, "tenant-a"),
        )
        assert resp.status_code == 403

    def test_git_provider_is_not_reached_for_other_tenant(self, client):
        """가드가 핸들러 앞에서 끊으므로 남의 테넌트 git 자격증명은 조회조차 되지 않는다."""
        with patch("core.api.skills_router._get_provider", return_value=MagicMock()) as get_provider:
            resp = client.get(
                "/skills/my-skill/branches?tenant_id=tenant-b",
                headers=_auth(USER_A, "tenant-a"),
            )
        assert resp.status_code == 403
        get_provider.assert_not_called()


class TestTokenValidation:
    def test_expired_token_is_rejected(self, client):
        token = _token(USER_A, "tenant-a", expires_in=-10)
        resp = client.get("/skills?tenant_id=tenant-a", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    def test_wrong_signature_is_rejected(self, client):
        token = _token(USER_A, "tenant-a", secret="attacker-secret")
        resp = client.get("/skills?tenant_id=tenant-a", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    def test_body_user_jwt_is_accepted_for_backward_compatibility(self, client):
        resp = client.request(
            "DELETE",
            "/skills/no-such-skill",
            json={"tenant_id": "tenant-a", "mode": "local", "user_jwt": _token(USER_A, "tenant-a")},
        )
        assert resp.status_code == 404

    def test_tenant_membership_falls_back_to_users_table(self, client):
        """토큰에 tenant_id 클레임이 없으면 users 테이블 소속으로 판단한다.

        조회는 SDK(processgpt_agent_sdk.tenant_auth)가 자기 Supabase 클라이언트로
        수행한다. 키 선택 순서(SERVICE_ROLE_KEY → SUPABASE_KEY → SUPABASE_ANON_KEY)는
        core.storage.db.get_supabase 와 같으므로 운영 동작은 그대로다.
        """
        sb = MagicMock()
        sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"tenant_id": "tenant-a"}]
        )
        with patch("processgpt_agent_sdk.database.get_db_client", return_value=sb), \
                patch("processgpt_agent_sdk.database.initialize_db"):
            with patch("core.api.skills_router._scan_skills", return_value=[]):
                ok = client.get("/skills?tenant_id=tenant-a", headers=_auth(USER_B, None))
            forbidden = client.get("/skills?tenant_id=tenant-b", headers=_auth(USER_B, None))
        assert ok.status_code == 200
        assert forbidden.status_code == 403


class TestAsymmetricTokens:
    """운영/로컬 Supabase 는 JWT signing keys(ES256) 를 쓴다 — JWKS 경로 검증."""

    @pytest.fixture
    def ec_keys(self):
        from cryptography.hazmat.primitives.asymmetric import ec

        private_key = ec.generate_private_key(ec.SECP256R1())
        return private_key, private_key.public_key()

    def _es256_token(self, private_key, tenant_id: str) -> str:
        return jwt.encode(
            {
                "sub": USER_A,
                "exp": int(time.time()) + 3600,
                "app_metadata": {"tenant_id": tenant_id},
            },
            private_key,
            algorithm="ES256",
            headers={"kid": "test-kid"},
        )

    def _patched_jwk_client(self, public_key):
        signing_key = MagicMock()
        signing_key.key = public_key
        client = MagicMock()
        client.get_signing_key_from_jwt.return_value = signing_key
        return patch.object(tenant_auth, "_get_jwk_client", return_value=client)

    def test_es256_token_is_verified_via_jwks(self, client, ec_keys, monkeypatch):
        # JWKS 경로가 선택되는지 보려면 대칭키 시크릿이 없어야 한다.
        monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
        private_key, public_key = ec_keys
        token = self._es256_token(private_key, "tenant-a")

        with self._patched_jwk_client(public_key):
            with patch("core.api.skills_router._scan_skills", return_value=[]):
                ok = client.get("/skills?tenant_id=tenant-a", headers={"Authorization": f"Bearer {token}"})
            forbidden = client.get("/skills?tenant_id=tenant-b", headers={"Authorization": f"Bearer {token}"})

        assert ok.status_code == 200
        assert forbidden.status_code == 403

    def test_es256_token_signed_by_another_key_is_rejected(self, client, ec_keys, monkeypatch):
        from cryptography.hazmat.primitives.asymmetric import ec

        monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
        private_key, _ = ec_keys
        token = self._es256_token(private_key, "tenant-a")
        other_public_key = ec.generate_private_key(ec.SECP256R1()).public_key()

        with self._patched_jwk_client(other_public_key):
            resp = client.get("/skills?tenant_id=tenant-a", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 401


class TestBuiltinSkills:
    def test_requires_authentication(self, client):
        assert client.get("/skills-builtin").status_code == 401

    def test_allows_any_authenticated_user(self, client):
        with patch("core.api.skills_router._scan_builtin_skills", return_value=[]):
            resp = client.get("/skills-builtin", headers=_auth(USER_A, "tenant-a"))
        assert resp.status_code == 200
