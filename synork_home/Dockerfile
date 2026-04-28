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

COPY src/ /app/

# Install run.sh as an s6 longrun service (s6-overlay v3 pattern)
# This is required by recent HA base images — using `CMD ["/run.sh"]`
# triggers `s6-overlay-suexec: fatal: can only run as pid 1` because
# with-contenv/bashio expect to be invoked from inside the s6 service tree.
COPY run.sh /etc/services.d/synork/run
RUN chmod a+x /etc/services.d/synork/run \
    && printf '#!/command/execlineb -S0\nimportas -i CODE 1\nimportas -i SIGNAL 2\ns6-svscanctl -t /var/run/s6/services\n' \
       > /etc/services.d/synork/finish \
    && chmod a+x /etc/services.d/synork/finish

# Persistent data directory (HA maps /data for addon storage)
# /data/synork/ holds wizard sidecar (hub_config.json) and assistant pairing
# config — both written by the addon at runtime, not bundled in the image.
RUN mkdir -p /data/config /data/cache /data/frontend /data/synork

# --------------------------------------------------------------------------- #
# Entrypoint — leave it to the parent image (/init from s6-overlay).
# Do NOT set CMD — s6-overlay v3 will discover and run our service in
# /etc/services.d/synork/ automatically.
# --------------------------------------------------------------------------- #
