FROM debian:trixie-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev cppcheck coreutils \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 sandbox \
    && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin sandbox

COPY runner/run-submission.sh /usr/local/bin/run-submission
RUN chmod 0755 /usr/local/bin/run-submission

USER 10001:10001
WORKDIR /tmp
CMD ["sleep", "infinity"]
