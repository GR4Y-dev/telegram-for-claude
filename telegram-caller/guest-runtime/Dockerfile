FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
LABEL telegram.guest-runtime="1"
# Native Claude 2.1.286 is mounted at runtime; no account files are built into this image.
USER 65532:65532
WORKDIR /guest/work
