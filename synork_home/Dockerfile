ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8
ENV PYTHONUNBUFFERED=1

# --------------------------------------------------------------------------- #
# System dependencies
# --------------------------------------------------------------------------- #
RUN apk add --no-cache \
    python3 \
    py3-pip \
    # Audio stack (ALSA for mic/speaker access)
    alsa-utils \
    alsa-lib \
    portaudio-dev \
    # Bluetooth (for BLE provisioning v1.5 + device detection)
    bluez \
    bluez-libs \
    # WiFi AP fallback (hostapd + dnsmasq for captive portal)
    hostapd \
    dnsmasq \
    # mDNS / DNS-SD
    avahi \
    avahi-compat-libdns_sd \
    dbus \
    # USB serial for radio dongles
    libusb \
    # Audio: ffmpeg (decoding from various sources) + libsndfile (whisper, soundfile)
    ffmpeg \
    libsndfile \
    # git: needed by the bootloader to fetch live app code (auto-update)
    git \
    ca-certificates \
    # Thread/OTBR dependencies (Session 18)
    # Included in all images; OTBR only starts when Thread radio detected.
    # ipset + iptables: firewall rules for Thread border routing
    # radvd: IPv6 router advertisement for Thread mesh-local prefix
    ipset \
    iptables \
    radvd \
    # Build deps for native Python modules (cleaned up below)
    gcc \
    musl-dev \
    python3-dev \
    libffi-dev \
    portaudio-dev \
    && rm -rf /var/cache/apk/*

# tayga (NAT64 translation for Thread → IPv4) only ships in Alpine ≥3.19;
# the HA base images we use are pinned to Alpine 3.18, so pull tayga from
# the edge community repo. Non-fatal — Thread border routing still works
# without NAT64, just without IPv4 reachability for Thread-only devices.
RUN apk add --no-cache \
        --repository=https://dl-cdn.alpinelinux.org/alpine/edge/community \
        tayga \
    || echo "WARN: tayga unavailable, NAT64 disabled"

# --------------------------------------------------------------------------- #
# Python dependencies
# --------------------------------------------------------------------------- #
COPY addon/requirements.txt /tmp/
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Clean up build dependencies to reduce image size
RUN apk del gcc musl-dev python3-dev libffi-dev 2>/dev/null || true

# --------------------------------------------------------------------------- #
# Application code
# --------------------------------------------------------------------------- #
# Copy shared protocol/schema types (contract source of truth)
# Build context is synork-home/, so paths are relative to that.
COPY shared/ /app/shared/

COPY addon/src/ /app/

# Bootloader: pulls latest app code from the public repo on every container
# start (Synork live update system A). The /app tree above is kept as a
# baked-in baseline used only when the network is unavailable.
COPY addon/bootloader.py /opt/bootloader.py
RUN chmod a+x /opt/bootloader.py

# Entrypoint script. The addon's config.yaml sets `init: false` which
# disables Docker's default init wrapper so the parent image's `/init`
# (s6-overlay v3) becomes PID 1, which is required for s6-overlay-suexec
# and `with-contenv bashio` to function.
COPY addon/run.sh /
RUN chmod a+x /run.sh

# Persistent data directory (HA maps /data for addon storage)
# /data/synork/ holds wizard sidecar (hub_config.json) and assistant pairing
# config — both written by the addon at runtime, not bundled in the image.
RUN mkdir -p /data/config /data/cache /data/frontend /data/synork

CMD [ "/run.sh" ]
