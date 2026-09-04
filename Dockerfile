ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE} AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /src
COPY pyproject.toml ./
COPY app ./app
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --upgrade pip \
    && /opt/venv/bin/python -m pip install .

FROM ${PYTHON_IMAGE}

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EXTERNAL_DELIVERY_ENABLED=false \
    BUSINESS_WRITES_ENABLED=false \
    CODESTRA_ENVIRONMENT=container

RUN groupadd --gid 10001 codestra \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin codestra

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["uvicorn"]
CMD ["app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
