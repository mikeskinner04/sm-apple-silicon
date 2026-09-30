# Runs Signal Hound's Linux aarch64 SM API to produce reference filter taps.
# Apple Silicon runs this natively; no emulation.
#
#   docker build --platform linux/arm64 -t sm-ref reference
#   docker run --rm --platform linux/arm64 -v "$PWD":/work sm-ref \
#       /work/libsm_api.so.2.3.9 > ref_filters.json
#
# The .so is not copied into the image; mount your own copy from the SDK.

FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev libusb-1.0-0 \
 && rm -rf /var/lib/apt/lists/*
COPY ref_filters.c /src/
RUN gcc -O2 -Wall -Wextra -o /usr/local/bin/ref_filters /src/ref_filters.c -ldl
ENTRYPOINT ["/usr/local/bin/ref_filters"]
