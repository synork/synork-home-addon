#!/usr/bin/with-contenv bashio
# Synork Home — LIVE entrypoint (hot-pulled from synork-home-addon repo).
#
# The container's baked /run.sh is a thin trampoline: it pulls this repo
# into /data/synork/app and exec's THIS file. So changes here take effect
# on the next addon restart with no container rebuild required.

# --------------------------------------------------------------------------- #
# Read configuration from HA Supervisor
# --------------------------------------------------------------------------- #
RELAY_URL=$(bashio::config 'relay_url')
RELAY_API_URL=$(bashio::config 'relay_api_url')
DEVICE_ID=$(bashio::config 'device_id')
DEVICE_SECRET=$(bashio::config 'device_secret')
LANGUAGE=$(bashio::config 'language')
LOG_LEVEL=$(bashio::config 'log_level')
MOCK_HARDWARE=$(bashio::config 'mock_hardware')
LOCAL_STT=$(bashio::config 'local_stt')
CLOUD_STT=$(bashio::config 'cloud_stt')
WAKE_WORD=$(bashio::config 'wake_word')
TTS_PROVIDER=$(bashio::config 'tts_provider')
CARTESIA_VOICE_ID=$(bashio::config 'cartesia_voice_id')
ARLO_ENABLED=$(bashio::config 'arlo_enabled')
ARLO_LANGUAGE=$(bashio::config 'arlo_language')
WAKE_WORD_THRESHOLD=$(bashio::config 'wake_word_threshold')
WHISPER_MODEL=$(bashio::config 'whisper_model')
ASSISTANT_PIPELINE=$(bashio::config 'assistant_pipeline')
FRONTEND_PATCHER=$(bashio::config 'frontend_patcher')
SATELLITE_PORT=$(bashio::config 'satellite_port')
AUDIO_DEVICE=$(bashio::config 'audio_device')
MODE=$(bashio::config 'mode')

bashio::log.info "Synork Home v0.2.10 — live entrypoint"
bashio::log.info "Mode: ${MODE} | Language: ${LANGUAGE} | Log level: ${LOG_LEVEL}"
if [ -n "${DEVICE_ID}" ]; then
    bashio::log.info "Device ID: ${DEVICE_ID}"
else
    bashio::log.warning "Device ID empty — addon not paired yet (use the wizard)"
fi
bashio::log.info "Relay URL: ${RELAY_URL}"
bashio::log.info "Update channel: ${SYNORK_UPDATE_CHANNEL:-stable}"

# --------------------------------------------------------------------------- #
# Arlo voice assistant — env + first-boot setup
# --------------------------------------------------------------------------- #
# Cloud calls (TTS, brain) are proxied through the Synork relay using the
# device's short-lived WS session_token as Bearer auth. The relay holds the
# upstream provider keys server-side — no API keys ever live on the device.
# Until the relay connects the runtime falls back to local providers.
export CARTESIA_VOICE_ID="${CARTESIA_VOICE_ID}"
export ARLO_LANGUAGE="${ARLO_LANGUAGE:-${LANGUAGE}}"
export WAKE_WORD_THRESHOLD="${WAKE_WORD_THRESHOLD:-0.55}"
export WHISPER_MODEL="${WHISPER_MODEL:-large-v3-turbo}"

if [ "${ARLO_ENABLED}" = "true" ]; then
    bashio::log.info "─── Arlo voice assistant ───"
    bashio::log.info "  Wake word:     ${WAKE_WORD} (threshold ${WAKE_WORD_THRESHOLD})"
    bashio::log.info "  STT model:     ${WHISPER_MODEL}"
    bashio::log.info "  TTS provider:  ${TTS_PROVIDER}"
    bashio::log.info "  Language:      ${ARLO_LANGUAGE}"
    bashio::log.info "  Audio device:  ${AUDIO_DEVICE:-auto}"
    bashio::log.info "  Cloud calls:   proxied via Synork relay (no keys on device)"

    # Idempotent first-boot setup: download wake-word model into Synork dir
    # and into Wyoming openWakeWord's /share/openwakeword/, fetch Cartesia
    # voice metadata, pre-download Whisper. Looks up the live source first
    # (hot-pulled tree), then falls back to baked-in /app baseline.
    FIRST_BOOT=""
    for cand in \
        "${SYNORK_LIVE_DIR:-/data/synork/app}/synork_home/src/assistant/first_boot.py" \
        /app/assistant/first_boot.py; do
        if [ -f "${cand}" ]; then
            FIRST_BOOT="${cand}"
            break
        fi
    done
    if [ -n "${FIRST_BOOT}" ]; then
        bashio::log.info "Running first-boot setup: ${FIRST_BOOT}"
        python3 "${FIRST_BOOT}" || \
            bashio::log.warning "first-boot reported errors — continuing"
    else
        bashio::log.warning "first_boot.py not found — skipping wake-word setup"
    fi
else
    bashio::log.info "Arlo voice assistant: disabled by config"
fi

# --------------------------------------------------------------------------- #
# Start the addon (via the bootloader, which exec's the live main.py)
# --------------------------------------------------------------------------- #
exec python3 /opt/bootloader.py \
    --relay-url "${RELAY_URL}" \
    --relay-api-url "${RELAY_API_URL}" \
    --device-id "${DEVICE_ID}" \
    --device-secret "${DEVICE_SECRET}" \
    --language "${LANGUAGE}" \
    --log-level "${LOG_LEVEL}" \
    --mock-hardware "${MOCK_HARDWARE}" \
    --local-stt "${LOCAL_STT}" \
    --cloud-stt "${CLOUD_STT}" \
    --wake-word "${WAKE_WORD}" \
    --tts-provider "${TTS_PROVIDER}" \
    --cartesia-voice-id "${CARTESIA_VOICE_ID}" \
    --assistant-pipeline "${ASSISTANT_PIPELINE}" \
    --frontend-patcher "${FRONTEND_PATCHER}" \
    --satellite-port "${SATELLITE_PORT}" \
    --audio-device "${AUDIO_DEVICE:-auto}" \
    --mode "${MODE}"
