# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The official MySQL 8.4 client provides the mysqldump used by backups; the repository publishes amd64 packages only.
ADD --chmod=644 --checksum=sha256:a4bcd9f16a53cc763f87b9955dbcdced33c7aa90296b157eb6ceef0f156f4327 https://repo.mysql.com/RPM-GPG-KEY-mysql-2025 /etc/apt/keyrings/mysql.asc
RUN echo "deb [signed-by=/etc/apt/keyrings/mysql.asc] http://repo.mysql.com/apt/debian bookworm mysql-8.4-lts" > /etc/apt/sources.list.d/mysql.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends mysql-community-client \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 rapidwire \
    && useradd --system --uid 10001 --gid rapidwire --home-dir /var/lib/rapidwire --shell /usr/sbin/nologin rapidwire \
    && install -d -o rapidwire -g rapidwire -m 750 /var/lib/rapidwire /var/lib/rapidwire/backups

WORKDIR /app

# Runtime dependencies are installed from pyproject.toml and constraints.txt alone so that code changes do not invalidate this layer.
COPY pyproject.toml constraints.txt ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt -c constraints.txt \
    && rm /tmp/requirements.txt

COPY . .
RUN pip install --no-cache-dir --no-deps -c constraints.txt . \
    && rm -rf build *.egg-info

USER rapidwire
ENTRYPOINT ["rapidwire"]
CMD ["--help"]
