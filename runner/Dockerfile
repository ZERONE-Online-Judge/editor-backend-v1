FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends g++ gcc python3 openjdk-17-jdk-headless ca-certificates && rm -rf /var/lib/apt/lists/*
COPY execute.py /opt/editor/execute.py
ENTRYPOINT ["python3", "/opt/editor/execute.py"]
