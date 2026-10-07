# Shared compile+execute image for one submission's container.
# Includes gcc deliberately -- compile and run happen in the same
# sandbox instance, so the toolchain has to be present here. The
# container is network-isolated, read-only, capability-dropped, and
# seccomp-filtered regardless, so a submission shelling out to gcc
# again at runtime gains it nothing it couldn't already attempt.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* \
    && useradd -M -u 10001 -s /usr/sbin/nologin sandboxuser

USER sandboxuser

# No ENTRYPOINT running a specific binary -- this container stays
# alive (see sleep infinity in docker.py's `docker run` call) and is
# driven entirely via `docker exec`, once for compile, once for run.