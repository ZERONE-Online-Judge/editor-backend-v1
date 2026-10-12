FROM debian:bookworm-slim AS native
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY launch.c /tmp/launch.c
RUN gcc -O2 -Wall -Wextra -Werror /tmp/launch.c -o /tmp/editor-launch
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 texlive-xetex texlive-latex-recommended texlive-fonts-recommended texlive-latex-extra texlive-lang-korean texlive-lang-chinese fonts-noto-cjk fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
COPY --chmod=0644 execute.py /opt/editor/execute.py
COPY --from=native --chmod=0755 /tmp/editor-launch /opt/editor/launch
ENTRYPOINT ["python3", "/opt/editor/execute.py"]
