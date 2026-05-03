#!/usr/bin/with-contenv bashio
# Synork Home Add-on Entrypoint
# Reads options from HA Supervisor, sets up environment, starts the addon.

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
MODE=$(bashio::config 'mode')

# HA Supervisor token is injected by Supervisor automatically
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN}"
export HA_BASE_URL="http://supervisor/core"

bashio::log.info "Starting Synork Home add-on v0.1.0"
bashio::log.info "Device ID: ${DEVICE_ID}"
bashio::log.info "Relay URL: ${RELAY_URL}"
bashio::log.info "Language: ${LANGUAGE}"
bashio::log.info "Log level: ${LOG_LEVEL}"

# Live update channel (stable|beta|dev) — read from addon options if present.
export SYNORK_UPDATE_CHANNEL="$(bashio::config 'update_channel' 'stable')"
export SYNORK_REPO_URL="$(bashio::config 'update_repo_url' 'https://github.com/synork/synork-home-addon.git')"
if bashio::config.true 'disable_autoupdate'; then
    export SYNORK_DISABLE_AUTOUPDATE=1
fi
bashio::log.info "Update channel: ${SYNORK_UPDATE_CHANNEL}"

# --------------------------------------------------------------------------- #
# Arlo voice assistant — config validation + boot summary
# --------------------------------------------------------------------------- #
# Export Arlo-specific env so the bootloader / app can read it.
# NOTE: Cloud credentials (Cartesia, OpenRouter, Arlo internal) are NOT
# user-facing — they are supplied by the Synork relay over the device
# WS session, authenticated via device_id/device_secret. The runtime
# falls back to local providers (Piper TTS, mock brain) if the relay
# hasn't pushed credentials yet.
export CARTESIA_VOICE_ID="${CARTESIA_VOICE_ID}"
export ARLO_LANGUAGE="${ARLO_LANGUAGE:-${LANGUAGE}}"
export WAKE_WORD_THRESHOLD="${WAKE_WORD_THRESHOLD:-0.55}"
export WHISPER_MODEL="${WHISPER_MODEL:-large-v3-turbo}"

if [ "${ARLO_ENABLED}" = "true" ]; then
    bashio::log.info "─── Arlo voice assistant ───"
    bashio::log.info "  Wake word:      ${WAKE_WORD} (threshold ${WAKE_WORD_THRESHOLD})"
    bashio::log.info "  STT model:      ${WHISPER_MODEL}"
    bashio::log.info "  TTS provider:   ${TTS_PROVIDER}"
    bashio::log.info "  Language:       ${ARLO_LANGUAGE}"
    bashio::log.info "  Cloud creds:    supplied by relay (no API keys here)"

    # First-boot model + voice setup. Idempotent.
    if [ -x /app/assistant/first_boot.py ] || [ -f /app/assistant/first_boot.py ]; then
        bashio::log.info "Running Arlo first-boot setup…"
        python3 /app/assistant/first_boot.py || \
            bashio::log.warning "Arlo first-boot setup reported errors — continuing."
    fi
else
    bashio::log.info "Arlo voice assistant: disabled by config"
fi

# --------------------------------------------------------------------------- #
# Start the addon (via the bootloader, which fetches the latest app code)
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
    --mode "${MODE}"
