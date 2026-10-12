FROM debian:bookworm-slim AS native
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY launch.c /tmp/launch.c
RUN gcc -O2 -Wall -Wextra -Werror /tmp/launch.c -o /tmp/editor-launch
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends g++ gcc python3 openjdk-17-jdk-headless ca-certificates && rm -rf /var/lib/apt/lists/*
COPY execute.py /opt/editor/execute.py
COPY --from=native /tmp/editor-launch /opt/editor/launch
ENTRYPOINT ["python3", "/opt/editor/execute.py"]
