#!/usr/bin/env bash
# Deploy the voice gateway and web client (Vercel project sc-voice-assistant). Needs VERCEL_TOKEN.
set -euo pipefail
cd "$(dirname "$0")/.."
scripts/audit.sh
npx --yes vercel@latest deploy --prod --yes --token "$VERCEL_TOKEN"
