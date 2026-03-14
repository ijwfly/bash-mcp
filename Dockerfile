FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

# Layer 1: system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl wget jq git vim nano \
    python3 python3-pip python3-venv \
    nodejs npm \
    htop net-tools iputils-ping dnsutils \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Layer 2: ocli
RUN npm install -g openapi-to-cli

# Layer 3: unprivileged user
RUN useradd -m -s /bin/bash mcpuser

# Layer 4: Python deps (cached separately from app code)
COPY server/requirements.txt /opt/server/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /opt/server/requirements.txt

# Layer 5: app code
COPY server/ /opt/server/
COPY entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh

# Layer 6: working directories
RUN mkdir -p /workspace && chown mcpuser:mcpuser /workspace
RUN mkdir -p /home/mcpuser/.ocli && chown mcpuser:mcpuser /home/mcpuser/.ocli

WORKDIR /workspace
USER mcpuser
EXPOSE 8080
ENTRYPOINT ["/opt/entrypoint.sh"]
