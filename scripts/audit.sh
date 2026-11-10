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
# The CDN can run ahead of npm for a few hours after a release (seen 10 Nov: 1.447.9 served, 1.447.8 on npm).
# Then audit the newest published version and say so; the audit itself always runs.
if ! npm view "posthog-js@${version}" version --prefer-online >/dev/null 2>&1; then
  published=$(npm view posthog-js version --prefer-online)
  echo "warning: posthog-js ${version} is not on npm yet; auditing the newest published version, ${published}"
  version="$published"
fi
printf '{"name":"web-audit","private":true,"dependencies":{"posthog-js":"%s"}}' "$version" > "$tmp/package.json"
(cd "$tmp" && npm install --package-lock-only --ignore-scripts --silent --prefer-online && npm audit --audit-level=low)
