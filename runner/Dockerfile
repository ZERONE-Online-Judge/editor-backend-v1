FROM eclipse-temurin:21.0.4_7-jdk-jammy AS icpc-jdk
FROM debian:bookworm-slim AS native
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY launch.c /tmp/launch.c
RUN gcc -O2 -Wall -Wextra -Werror /tmp/launch.c -o /tmp/editor-launch
FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends g++ gcc openjdk-17-jdk-headless ca-certificates && ln -sf /usr/local/bin/python3.13 /usr/bin/python3 && rm -rf /var/lib/apt/lists/*
COPY --from=icpc-jdk /opt/java/openjdk /usr/local/lib/icpc-jdk
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip bzip2 libffi8 libncursesw6 libtinfo6 && rm -rf /var/lib/apt/lists/*
COPY install-icpc-runtimes.sh /tmp/install-icpc-runtimes.sh
RUN sh /tmp/install-icpc-runtimes.sh && rm /tmp/install-icpc-runtimes.sh

COPY --chmod=0644 execute.py /opt/editor/execute.py
COPY --from=native --chmod=0755 /tmp/editor-launch /opt/editor/launch
ENTRYPOINT ["python3", "/opt/editor/execute.py"]

LABEL zoj.icpc_languages="v1"
