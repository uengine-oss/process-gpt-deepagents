# Application layout: repo root on disk = WORKDIR (/app).
# - `deepagents_server.py` / `deepagents_executor.py`: entry modules (import `app.*` package).
# - `app/`: Python package (sandbox, skills, agent, executor, …).
FROM python:3.13-slim

# 1) Timezone + build deps
ENV TZ=Asia/Seoul
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    tzdata \
    curl \
    gnupg \
    build-essential \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
 && echo $TZ > /etc/timezone \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# 2) Node.js 20.x + Supabase MCP (parity with existing action servers)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @supabase/mcp-server-supabase@latest \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# 3) App deps — repo root must be on sys.path so `app` package and root entry modules resolve.
WORKDIR /app
ENV PYTHONPATH=/app

# Install uv (requested) then install deps into system env
ENV PIP_DEFAULT_TIMEOUT=300
RUN pip install --no-cache-dir uv

COPY requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

# 4) Copy source
COPY . .

EXPOSE 8888
CMD ["python", "deepagents_server.py"]
