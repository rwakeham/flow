# Pinned to the multi-platform index digest (amd64, arm64, arm/v7 ...), so what was built,
# tested and scanned is what deploys. Dependabot (.github/dependabot.yml) proposes new digests;
# Python stays on 3.12.
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

# psycopg2-binary and the other dependencies ship as wheels: no compiler or
# libpq headers are needed, and the deploy's health check runs from the host.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# pip is only needed to install; it is removed in the same layer (with ensurepip's bundled
# copy), so the running image carries no package installer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip/_bundled

# The app runs as an unprivileged user. /backups is its only writable path: a new
# backupdata volume copies this ownership; docker-compose.yml's backups-owner
# step fixes an existing volume created by an older, root-run image.
RUN groupadd --system --gid 10001 flow \
    && useradd --system --uid 10001 --gid flow --home-dir /app --no-create-home --shell /usr/sbin/nologin flow \
    && mkdir -p /backups && chown flow:flow /backups

COPY . .

USER flow

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
