FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends python3 bubblewrap \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "-B"]
