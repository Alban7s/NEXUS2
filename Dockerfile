# NEXUS backend + dashboard
FROM python:3.12-slim

ARG INSTALL_TRIVY=false
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

# git: needed for GitOps-mode deploys. Image builds talk to the Docker daemon
# through its socket (see docker-compose.yml), so no docker CLI is needed here.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Optional: bake Trivy into the image (docker compose build --build-arg INSTALL_TRIVY=true).
RUN if [ "$INSTALL_TRIVY" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends wget gnupg lsb-release \
      && wget -qO- https://aquasecurity.github.io/trivy-repo/deb/public.key | gpg --dearmor -o /usr/share/keyrings/trivy.gpg \
      && echo "deb [signed-by=/usr/share/keyrings/trivy.gpg] https://aquasecurity.github.io/trivy-repo/deb generic main" > /etc/apt/sources.list.d/trivy.list \
      && apt-get update && apt-get install -y --no-install-recommends trivy \
      && rm -rf /var/lib/apt/lists/*; \
    fi

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend
COPY assets ./assets

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"

CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
