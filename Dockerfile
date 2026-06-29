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

COPY requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

# 4) Copy source
COPY . .

EXPOSE 8888
CMD ["python", "server.py"]
