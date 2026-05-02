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
CARTESIA_API_KEY=$(bashio::config 'cartesia_api_key')
CARTESIA_VOICE_ID=$(bashio::config 'cartesia_voice_id')
OPENROUTER_API_KEY=$(bashio::config 'openrouter_api_key')
ARLO_ENABLED=$(bashio::config 'arlo_enabled')
ARLO_INTERNAL_SECRET=$(bashio::config 'arlo_internal_secret')
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
export CARTESIA_API_KEY="${CARTESIA_API_KEY}"
export CARTESIA_VOICE_ID="${CARTESIA_VOICE_ID}"
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY}"
export ARLO_INTERNAL_SECRET="${ARLO_INTERNAL_SECRET}"
export ARLO_LANGUAGE="${ARLO_LANGUAGE:-${LANGUAGE}}"
export WAKE_WORD_THRESHOLD="${WAKE_WORD_THRESHOLD:-0.55}"
export WHISPER_MODEL="${WHISPER_MODEL:-large-v3-turbo}"

if [ "${ARLO_ENABLED}" = "true" ]; then
    bashio::log.info "─── Arlo voice assistant ───"
    bashio::log.info "  Wake word:      ${WAKE_WORD} (threshold ${WAKE_WORD_THRESHOLD})"
    bashio::log.info "  STT model:      ${WHISPER_MODEL}"
    bashio::log.info "  TTS provider:   ${TTS_PROVIDER}"
    bashio::log.info "  Language:       ${ARLO_LANGUAGE}"

    # Soft validation — warn but don't abort. Voice features fail closed
    # at runtime if a key is missing; the rest of the addon still starts.
    if [ -z "${OPENROUTER_API_KEY}" ]; then
        bashio::log.warning "OpenRouter API key not set — Arlo brain will be unavailable."
    fi
    if [ "${TTS_PROVIDER}" = "cartesia" ] || [ "${TTS_PROVIDER}" = "auto" ]; then
        if [ -z "${CARTESIA_API_KEY}" ]; then
            bashio::log.warning "Cartesia API key not set — TTS will fall back or be silent."
        fi
    fi

    # First-boot model + voice setup. Idempotent — script exits fast if everything
    # is already cached at /data/synork/models.
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
    --cartesia-api-key "${CARTESIA_API_KEY}" \
    --cartesia-voice-id "${CARTESIA_VOICE_ID}" \
    --assistant-pipeline "${ASSISTANT_PIPELINE}" \
    --frontend-patcher "${FRONTEND_PATCHER}" \
    --satellite-port "${SATELLITE_PORT}" \
    --mode "${MODE}"
