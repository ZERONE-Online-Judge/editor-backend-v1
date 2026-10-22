FROM eclipse-temurin:21-jdk-noble@sha256:107215954359f1ab0be058a247d7c64ed6e69c71c1682d471d38ef751ec8e964 AS zoj-jdk
FROM python:3.11.17-slim-trixie@sha256:cce12316475d7d56d605f76e1d811d0dc4e7227f2d4afd29951521264f504ff8 AS cpython311
FROM python:3.13.16-slim-trixie@sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f AS native
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY launch.c /tmp/launch.c
RUN gcc -O2 -Wall -Wextra -Werror /tmp/launch.c -o /tmp/editor-launch
FROM python:3.13.16-slim-trixie@sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f
RUN apt-get update && apt-get install -y --no-install-recommends g++ gcc ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=zoj-jdk /opt/java/openjdk /usr/local/lib/zoj-jdk
COPY --from=cpython311 /usr/local /usr/local/lib/zoj-python311
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip bzip2 libffi8 libncursesw6 libtinfo6 && rm -rf /var/lib/apt/lists/*
COPY install-zoj-runtimes.sh /tmp/install-zoj-runtimes.sh
RUN sh /tmp/install-zoj-runtimes.sh && rm /tmp/install-zoj-runtimes.sh

RUN ln -sf /usr/local/bin/python3.11 /usr/local/bin/python3
COPY --chmod=0644 execute.py /opt/editor/execute.py
COPY --from=native --chmod=0755 /tmp/editor-launch /opt/editor/launch
ENTRYPOINT ["python3.13", "/opt/editor/execute.py"]

LABEL zoj.runtime_policy="v1"
LABEL zoj.icpc_languages="v1"
