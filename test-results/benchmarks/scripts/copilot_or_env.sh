#!/usr/bin/env bash
# copilot_or_env.sh — Source this file to set copilot-or environment variables
#
# Usage:
#   source benchmarks/scripts/copilot_or_env.sh
#
# Required: The following environment variables must be set BEFORE sourcing,
# or they will be read from your shell alias configuration.
#
# If you use the copilot-or alias, export these variables first:
#   export COPILOT_PROVIDER_BASE_URL="https://openrouter.ai/api/v1"
#   export COPILOT_PROVIDER_TYPE="openai"
#   export COPILOT_PROVIDER_API_KEY="<your-openrouter-api-key>"
#   export COPILOT_MODEL="nvidia/nemotron-3-super-120b-a12b:free"
#   export COPILOT_PROVIDER_MAX_PROMPT_TOKENS="32768"
#   export COPILOT_PROVIDER_MAX_OUTPUT_TOKENS="4096"

set -euo pipefail

# Try to extract from zshrc alias if not already set
if [[ -z "${COPILOT_PROVIDER_BASE_URL:-}" ]]; then
  if grep -q "copilot-or" ~/.zshrc 2>/dev/null; then
    echo "[copilot-or-env] Extracting environment from ~/.zshrc alias..."
    eval "$(grep -A 10 'alias copilot-or' ~/.zshrc | grep -oP '(?:COPILOT_[A-Z_]+)="[^"]*"' | sed 's/^/export /')" 2>/dev/null || true
  fi
fi

# Validate required variables
_missing=()
for var in COPILOT_PROVIDER_BASE_URL COPILOT_PROVIDER_TYPE COPILOT_PROVIDER_API_KEY COPILOT_MODEL; do
  if [[ -z "${!var:-}" ]]; then
    _missing+=("$var")
  fi
done

if [[ ${#_missing[@]} -gt 0 ]]; then
  echo "[copilot-or-env] ERROR: Missing required environment variables: ${_missing[*]}"
  echo "[copilot-or-env] Set them manually or ensure your ~/.zshrc copilot-or alias is configured."
  return 1 2>/dev/null || exit 1
fi

echo "[copilot-or-env] Environment configured:"
echo "  COPILOT_PROVIDER_BASE_URL = ${COPILOT_PROVIDER_BASE_URL}"
echo "  COPILOT_PROVIDER_TYPE     = ${COPILOT_PROVIDER_TYPE}"
echo "  COPILOT_MODEL             = ${COPILOT_MODEL}"
echo "  COPILOT_PROVIDER_API_KEY  = ****${COPILOT_PROVIDER_API_KEY: -4}"

export COPILOT_PROVIDER_BASE_URL
export COPILOT_PROVIDER_TYPE
export COPILOT_PROVIDER_API_KEY
export COPILOT_MODEL
export COPILOT_PROVIDER_MAX_PROMPT_TOKENS="${COPILOT_PROVIDER_MAX_PROMPT_TOKENS:-32768}"
export COPILOT_PROVIDER_MAX_OUTPUT_TOKENS="${COPILOT_PROVIDER_MAX_OUTPUT_TOKENS:-4096}"
