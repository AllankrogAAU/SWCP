# Minimal runtime image for executing pre-compiled student C binaries.
# Compilation happens OUTSIDE this container, on the host/build service —
# this image never sees source code or a compiler, only a binary.
FROM debian:trixie-slim

# Remove package manager metadata and anything not needed at runtime,
# so a shell escape has as little to work with as possible.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* \
    && useradd -M -u 10001 -s /usr/sbin/nologin sandboxuser

# The binary is bind-mounted in at container start (read-only), not baked
# into the image — see runner.py. /sandbox/work is the only writable path.
RUN mkdir -p /sandbox/work && chown sandboxuser:sandboxuser /sandbox/work

USER sandboxuser
WORKDIR /sandbox

ENTRYPOINT ["/sandbox/bin/prog"]
