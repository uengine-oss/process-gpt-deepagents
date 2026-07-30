FROM python:3.13-slim

# 표준 독립 저장소 빌드는 기본값(.)을 그대로 쓰면 되고,
# 모노레포에서 이 서비스만 빌드하는 경우는 컨텍스트를 모노레포 루트로 잡고
# --build-arg SERVICE_DIR=services/deepagents 로 재정의하면 된다.
ARG SERVICE_DIR=.

# 1) Timezone + build deps
ENV TZ=Asia/Seoul
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    tzdata \
    curl \
    gnupg \
    build-essential \
    git \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
 && echo $TZ > /etc/timezone \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# 2) Node.js 20.x + Supabase MCP
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @supabase/mcp-server-supabase@latest \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# 3) App deps
WORKDIR /app
ENV PYTHONPATH=/app

ENV PIP_DEFAULT_TIMEOUT=300
RUN pip install --no-cache-dir uv

COPY ${SERVICE_DIR}/requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

# 4) Copy source
#    - 독립 저장소: SERVICE_DIR=. (기본값), 빌드 컨텍스트 = 이 저장소 루트
#    - 모노레포: SERVICE_DIR=services/deepagents, 빌드 컨텍스트 = 모노레포 루트
COPY ${SERVICE_DIR}/. .

# 5) 오프라인 안전 번들 시스템 스킬 (레포 루트 skills/ submodule들).
#    core/skills/local_system_skills.py가 기동 시 이 경로를
#    {SKILLS_DIRS}/process-gpt-system/ 으로 동기화해 모든 테넌트에 전역 노출한다.
COPY skills /app/system-skills/process-gpt-system

EXPOSE 8888
CMD ["python", "server.py"]
