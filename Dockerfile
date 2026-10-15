FROM python:3.13-slim
COPY --from=docker:28-cli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && groupadd -g 1000 editor && useradd -m -u 1000 -g editor editor
COPY app ./app
COPY replay ./replay
COPY runner ./runner
COPY vendor ./vendor
COPY migrations ./migrations
COPY alembic.ini .
# A private deployment checkout uses umask 077. Keep root-owned application
# files readable by the unprivileged runtime regardless of checkout modes.
RUN chmod -R a+rX /app
USER 1000:1000
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
