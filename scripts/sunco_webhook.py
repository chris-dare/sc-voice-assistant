"""Create the SunCo custom integration whose webhook delivers bot messages to the gateway (design 5.9).

Not run automatically: it changes the shared SunCo app. Prints the plan by default; --apply creates it.

    uv run python scripts/sunco_webhook.py https://sc-voice-assistant.vercel.app
    uv run python scripts/sunco_webhook.py https://sc-voice-assistant.vercel.app --apply

Then set SUNCO_DELIVERY=webhook and SUNCO_WEBHOOK_SECRET (printed once, on --apply) on the gateway, and
REDIS_URL on both services so every instance hears every webhook.
"""

from __future__ import annotations

import argparse
import sys

import httpx

from app.config import Settings


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("gateway_url")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    s = Settings()
    body = {
        "type": "custom",
        "displayName": "Zigi voice gateway",
        "webhooks": [
            {"target": args.gateway_url.rstrip("/") + "/webhooks/sunco", "triggers": ["conversation:message"], "includeFullUser": False}
        ],
    }
    url = f"{s.sunco_base_url.rstrip('/')}/v2/apps/{s.zendesk_app_id}/integrations"
    print(f"POST {url}\n{body}")
    if not args.apply:
        print("dry run: add --apply to create it")
        return
    resp = httpx.post(url, json=body, auth=(s.zendesk_key_id, s.zendesk_secret), timeout=30)
    if resp.status_code >= 300:
        sys.exit(f"SunCo said HTTP {resp.status_code}: {resp.text[:300]}")
    hook = resp.json()["integration"]["webhooks"][0]
    print(f"integration created. Set SUNCO_WEBHOOK_SECRET to this value now (it is not shown again):\n{hook['secret']}")


if __name__ == "__main__":
    main()
