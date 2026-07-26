FROM python:3.13-slim

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

COPY services/deepagents/requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

# 4) Copy source (build context is the repo root — see docker-compose.yml)
COPY services/deepagents/. .

# 5) 오프라인 안전 번들 시스템 스킬 (레포 루트 skills/ submodule들).
#    core/skills/local_system_skills.py가 기동 시 이 경로를
#    {SKILLS_DIRS}/process-gpt-system/ 으로 동기화해 모든 테넌트에 전역 노출한다.
COPY skills /app/system-skills/process-gpt-system

EXPOSE 8888
CMD ["python", "server.py"]
