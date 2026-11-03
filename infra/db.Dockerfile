FROM postgres:17@sha256:31ba9c94a03ada9988f2bb493a22a75eef3a185884533e5cdbcd498ea835b08f
# Vendor fixes; gosu is only used when started as root, and this image never is.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/* \
    && rm /usr/local/bin/gosu
USER postgres
