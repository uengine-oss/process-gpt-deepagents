"""/chat/stream · /chat/stream/attach 테넌트 접근 제어 테스트.

/skills* 를 잠가도 채팅 경로가 열려 있으면 같은 격리가 뚫린다 — 본문 tenant_id 가
executor → _get_skills(tenant_id) → 컨테이너 스킬 마운트까지 그대로 흘러가기 때문이다.
여기서는 그 경로가 막혔는지, 정상 요청은 그대로 통과하는지를 확인한다.
"""

import time

import jwt
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from processgpt_agent_sdk import tenant_auth
from processgpt_agent_sdk.tenant_auth import ChatTenantGuardMiddleware

pytestmark = pytest.mark.real_tenant_auth

SECRET = "test-jwt-secret-0123456789abcdef-32b"
USER_A = "00000000-0000-0000-0000-00000000000a"


@pytest.fixture(autouse=True)
def jwt_secret(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    tenant_auth.clear_caches()
    yield
    tenant_auth.clear_caches()


def _token(tenant_id: str) -> str:
    return jwt.encode(
        {"sub": USER_A, "exp": int(time.time()) + 3600, "app_metadata": {"tenant_id": tenant_id}},
        SECRET,
        algorithm="HS256",
    )


def _auth(tenant_id: str = "tenant-a") -> dict:
    return {"Authorization": f"Bearer {_token(tenant_id)}"}


@pytest.fixture
def client():
    """SDK 의 /chat/stream 을 흉내내는 에코 핸들러 + 실제 가드 미들웨어."""

    async def echo(request: Request) -> JSONResponse:
        body = await request.json()
        # 하위 핸들러(SDK)가 실제로 보게 되는 tenant_id 를 그대로 돌려준다.
        return JSONResponse({"seen_tenant_id": body.get("tenant_id"), "message": body.get("message")})

    app = Starlette(routes=[Route("/chat/stream", echo, methods=["POST"])])
    app.add_middleware(ChatTenantGuardMiddleware, paths=("/chat/stream",))
    return TestClient(app, raise_server_exceptions=True)


class TestChatStreamGuard:
    def test_no_token_is_rejected(self, client):
        resp = client.post("/chat/stream", json={"message": "hi", "tenant_id": "tenant-a"})
        assert resp.status_code == 401

    def test_other_tenant_is_forbidden(self, client):
        """핵심 회귀 테스트 — 본문 tenant_id 를 바꿔 남의 테넌트 스킬을 로드할 수 없어야 한다."""
        resp = client.post(
            "/chat/stream",
            json={"message": "hi", "tenant_id": "tenant-b"},
            headers=_auth("tenant-a"),
        )
        assert resp.status_code == 403

    def test_own_tenant_passes_through_unchanged(self, client):
        resp = client.post(
            "/chat/stream",
            json={"message": "hi", "tenant_id": "tenant-a"},
            headers=_auth("tenant-a"),
        )
        assert resp.status_code == 200
        assert resp.json() == {"seen_tenant_id": "tenant-a", "message": "hi"}

    def test_missing_tenant_id_is_filled_from_token(self, client):
        """본문에 tenant_id 가 없으면 검증된 값을 채워 하위 핸들러로 넘긴다."""
        resp = client.post("/chat/stream", json={"message": "hi"}, headers=_auth("tenant-a"))
        assert resp.status_code == 200
        assert resp.json()["seen_tenant_id"] == "tenant-a"

    def test_body_user_jwt_is_accepted(self, client):
        """헤더 없이 본문 user_jwt 만 보내던 구버전 클라이언트도 검증된다."""
        resp = client.post(
            "/chat/stream",
            json={"message": "hi", "tenant_id": "tenant-a", "user_jwt": _token("tenant-a")},
        )
        assert resp.status_code == 200
        assert resp.json()["seen_tenant_id"] == "tenant-a"

    def test_body_user_jwt_cannot_claim_other_tenant(self, client):
        resp = client.post(
            "/chat/stream",
            json={"message": "hi", "tenant_id": "tenant-b", "user_jwt": _token("tenant-a")},
        )
        assert resp.status_code == 403

    def test_expired_token_is_rejected(self, client):
        token = jwt.encode(
            {"sub": USER_A, "exp": int(time.time()) - 10, "app_metadata": {"tenant_id": "tenant-a"}},
            SECRET,
            algorithm="HS256",
        )
        resp = client.post(
            "/chat/stream",
            json={"message": "hi", "tenant_id": "tenant-a"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401

    def test_sse_response_streams_through_the_guard(self):
        """가드는 요청 본문만 버퍼링하고 응답(send)은 그대로 통과시켜야 한다 — SSE 가 살아 있는지 확인."""
        from starlette.responses import StreamingResponse

        async def stream(request: Request) -> StreamingResponse:
            body = await request.json()

            async def gen():
                yield f"data: {body.get('tenant_id')}\n\n".encode()
                yield b"data: done\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream")

        app = Starlette(routes=[Route("/chat/stream", stream, methods=["POST"])])
        app.add_middleware(ChatTenantGuardMiddleware, paths=("/chat/stream",))
        with TestClient(app) as c:
            resp = c.post("/chat/stream", json={"message": "hi"}, headers=_auth("tenant-a"))

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.text == "data: tenant-a\n\ndata: done\n\n"

    def test_slow_sse_stream_is_not_cut_short(self):
        """가드가 가짜 http.disconnect 를 흘리면 StreamingResponse 가 즉시 취소된다.

        Starlette 의 StreamingResponse 는 receive() 로 클라이언트 끊김을 감시하다가
        http.disconnect 를 받으면 task group 을 취소한다. 미들웨어가 버퍼링한 본문을
        재생한 뒤 곧바로 disconnect 를 돌려주면, 실제 채팅처럼 await 가 끼는 스트림이
        첫 청크만 나가고 끊긴다(= 프론트에 네트워크 오류).
        """
        import asyncio

        from starlette.responses import StreamingResponse

        async def stream(request: Request) -> StreamingResponse:
            await request.json()

            async def gen():
                for i in range(3):
                    await asyncio.sleep(0.05)
                    yield f"data: {i}\n\n".encode()

            return StreamingResponse(gen(), media_type="text/event-stream")

        app = Starlette(routes=[Route("/chat/stream", stream, methods=["POST"])])
        app.add_middleware(ChatTenantGuardMiddleware, paths=("/chat/stream",))
        with TestClient(app) as c:
            resp = c.post("/chat/stream", json={"message": "hi"}, headers=_auth("tenant-a"))

        assert resp.status_code == 200
        assert resp.text == "data: 0\n\ndata: 1\n\ndata: 2\n\n"

    def test_other_paths_are_untouched(self):
        """가드 경로가 아닌 요청은 본문·헤더 그대로 통과해야 한다."""

        async def echo(request: Request) -> JSONResponse:
            return JSONResponse(await request.json())

        app = Starlette(routes=[Route("/other", echo, methods=["POST"])])
        app.add_middleware(ChatTenantGuardMiddleware, paths=("/chat/stream",))
        with TestClient(app) as c:
            resp = c.post("/other", json={"tenant_id": "tenant-b"})
        assert resp.status_code == 200
        assert resp.json() == {"tenant_id": "tenant-b"}


class TestChatStreamAttachGuard:
    """재접속 라우트는 이제 SDK 가 소유한다. 이 저장소가 기대하는 접근 제어가
    SDK 핸들러에서도 그대로인지 확인한다 — 방 ID 와 테넌트 ID 만 아는 외부인이
    남의 스트림에 붙지 못해야 한다."""

    @pytest.fixture
    def attach_client(self):
        from processgpt_agent_sdk.chat_sse import make_attach_handler

        app = Starlette(routes=[
            Route("/chat/stream/attach", make_attach_handler(require_auth=True), methods=["POST"]),
        ])
        return TestClient(app, raise_server_exceptions=True)

    def test_no_token_is_rejected(self, attach_client):
        resp = attach_client.post("/chat/stream/attach", json={"conversation_id": "c1", "tenant_id": "tenant-a"})
        assert resp.status_code == 401

    def test_other_tenant_is_forbidden(self, attach_client):
        resp = attach_client.post(
            "/chat/stream/attach",
            json={"conversation_id": "c1", "tenant_id": "tenant-b"},
            headers=_auth("tenant-a"),
        )
        assert resp.status_code == 403
