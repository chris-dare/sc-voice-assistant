#!/usr/bin/env bash
# Deploy the Zigi agent service (zigi_agent/) as its own Vercel project, sc-voice-agent.
# Needs VERCEL_TOKEN. The project's env vars (OPENROUTER_API_KEY, ZENDESK_*, LOGFIRE_TOKEN,
# AGENT_SERVICE_TOKEN, ENVIRONMENT) are set on Vercel, not here.
set -euo pipefail
cd "$(dirname "$0")/.."
scripts/audit.sh

stage=$(mktemp -d); trap 'rm -rf "$stage"' EXIT
cp -r app zigi_agent uv.lock .python-version pyproject.toml "$stage/"
find "$stage" -name __pycache__ -prune -exec rm -rf {} +
printf 'from zigi_agent.service import app  # Vercel entrypoint for the agent service\n\n__all__ = ["app"]\n' > "$stage/index.py"
printf '{\n  "$schema": "https://openapi.vercel.sh/vercel.json",\n  "regions": ["lhr1"],\n  "functions": { "index.py": { "maxDuration": 120 } }\n}\n' > "$stage/vercel.json"
printf '.venv\n__pycache__\n.vercel\n.env*\n' > "$stage/.vercelignore"
mkdir -p "$stage/.vercel"
printf '{"projectId":"prj_CAblapTtEsyPTWFMTkz0QiDAWYW9","orgId":"team_cvWj5Ypdb1ztf2raeoxgxJzS","projectName":"sc-voice-agent"}' > "$stage/.vercel/project.json"
(cd "$stage" && npx --yes vercel@latest deploy --prod --yes --token "$VERCEL_TOKEN")
