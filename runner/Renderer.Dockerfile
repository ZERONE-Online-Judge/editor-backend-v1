FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 texlive-xetex texlive-latex-recommended texlive-fonts-recommended texlive-latex-extra texlive-lang-korean texlive-lang-chinese fonts-noto-cjk fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
COPY execute.py /opt/editor/execute.py
ENTRYPOINT ["python3", "/opt/editor/execute.py"]
