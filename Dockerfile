# Digest fijado de python:3.11-slim (re-verificar/actualizar con:
#   docker pull python:3.11-slim && docker inspect --format='{{index .RepoDigests 0}}' python:3.11-slim)
FROM python:3.11-slim@sha256:9c900dea9e8fb7e16277c179b555cc72d29a352dbc33cff48ad5a0412fd5bfc7

WORKDIR /app

COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

COPY app ./app
COPY pyproject.toml .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
