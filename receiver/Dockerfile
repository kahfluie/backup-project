FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/ssh/ssh_host_* \
    && groupmod --gid 1000 backup \
    && usermod --uid 1000 --gid backup --home /uploads --shell /bin/sh --password '*' backup
WORKDIR /app
COPY backupkit/ ./backupkit/
COPY receiver/sshd_config ./receiver/sshd_config
EXPOSE 22
ENTRYPOINT ["python", "-m", "backupkit.receiver"]
