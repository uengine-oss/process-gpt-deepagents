"""tests/test_skill_bound_tools.py 가 테넌트 MCP 서버로 등록해 stdio 로 띄우는 MCP 서버.

`python skill_bound_mcp_server.py orders` 는 주문 도구 두 개를, `... other` 는 다른 서버의
도구 하나를, `... dup` 은 서비스 도구와 이름이 겹치는 도구를 낸다. 실제 MCP 프로토콜로 도구 목록·호출이 오가는지 보려는 것이라 응답은 고정값이다.
"""
import sys

from mcp.server.fastmcp import FastMCP

kind = sys.argv[1] if len(sys.argv) > 1 else "orders"
server = FastMCP(f"skill-bound-{kind}")

if kind == "orders":

    @server.tool()
    def lookup_order(order_id: str) -> str:
        """주문 번호로 배송 상태를 조회한다."""
        return f"ORDER {order_id}: shipped via MCP"

    @server.tool()
    def cancel_order(order_id: str) -> str:
        """주문을 취소한다."""
        return f"ORDER {order_id}: cancelled via MCP"

elif kind == "dup":

    @server.tool()
    def get_current_user(email: str) -> str:
        """서비스 BPM 도구와 이름이 같은 MCP 도구(채팅 루트가 제외한다)."""
        return "mcp user"

    @server.tool()
    def execute_process(process_definition_id: str) -> str:
        """서비스 BPM 도구와 이름이 같은 MCP 도구(실행 시 서비스 도구가 이긴다)."""
        return "mcp execute"

else:

    @server.tool()
    def ping_other(text: str) -> str:
        """다른 MCP 서버의 도구."""
        return f"pong {text}"


if __name__ == "__main__":
    server.run("stdio")
