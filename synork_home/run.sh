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

# --------------------------------------------------------------------------- #
# Start the addon
# --------------------------------------------------------------------------- #
exec python3 /app/main.py \
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
    --assistant-pipeline "${ASSISTANT_PIPELINE}" \
    --frontend-patcher "${FRONTEND_PATCHER}" \
    --satellite-port "${SATELLITE_PORT}" \
    --mode "${MODE}"
