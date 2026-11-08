#!/usr/bin/env bash
# Dependency audit, run before every deploy. Fails on any known vulnerability.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== Python (uv audit, gateway and agent service share uv.lock)"
uv audit --preview-features audit-command

echo "== Web client"
# No package manager or build step: the only third-party code is PostHog's loader, fetched at runtime.
# Audit the posthog-js version the CDN serves today.
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
version=$(curl -fsS https://eu-assets.i.posthog.com/static/array.js | grep -oE '"1\.[0-9]+\.[0-9]+"' | head -1 | tr -d '"')
echo "posthog-js served: ${version}"
printf '{"name":"web-audit","private":true,"dependencies":{"posthog-js":"%s"}}' "$version" > "$tmp/package.json"
(cd "$tmp" && npm install --package-lock-only --ignore-scripts --silent && npm audit --audit-level=low)
