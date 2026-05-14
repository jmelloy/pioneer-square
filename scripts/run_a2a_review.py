#!/usr/bin/env python3
"""Live test: call review_pr on the code-review-agent at agent.meyers.life.

The scripts/ folder is not in the Docker image, so copy it in first:
    docker cp scripts/run_a2a_review.py pioneer-square-backend-1:/app/

Then run (PRIVATE_KEY_PEM is required):
    docker exec -e PRIVATE_KEY_PEM="$(cat key.pem)" pioneer-square-backend-1 \
        python3 /app/run_a2a_review.py

Optional overrides:
    docker exec \
        -e PRIVATE_KEY_PEM="$(cat key.pem)" \
        -e REVIEWER_AGENT_URL=https://agent.meyers.life \
        -e PR_URL=https://github.com/org/repo/pull/1 \
        -e CALLER_DOMAIN=dnsid.pioneer-square.melloy.life \
        pioneer-square-backend-1 python3 /app/run_a2a_review.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from foreman.a2a_client import A2AClient  # noqa: E402

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")

AGENT_CARD_URL = (
    os.environ.get("REVIEWER_AGENT_URL", "https://agent.meyers.life").rstrip("/")
    + "/.well-known/agent.json"
)

PR_URL = os.environ.get("PR_URL", "https://github.com/Identity-Digital/dnsid/pull/861")
CALLER_DOMAIN = os.environ.get("CALLER_DOMAIN", "dnsid.pioneer-square.melloy.life")
PRIVATE_KEY_PEM = os.environ.get("PRIVATE_KEY_PEM", "")


async def main() -> int:
    if not PRIVATE_KEY_PEM:
        print("ERROR: set PRIVATE_KEY_PEM env var to the Ed25519 private key PEM", file=sys.stderr)
        return 1

    client = A2AClient(AGENT_CARD_URL)

    print(f"Fetching agent card from {AGENT_CARD_URL} ...")
    card = await client.fetch_agent_card()
    print(f"  name:     {card.get('name')}")
    print(f"  skills:   {[s.get('id') for s in card.get('skills', [])]}")
    print(f"  security: {card.get('security')}")

    print(f"\nCalling review_pr on {PR_URL} ...")
    print(f"  caller_domain: {CALLER_DOMAIN}")
    result = await client.review_pr(
        PR_URL,
        caller_domain=CALLER_DOMAIN,
        private_key_pem=PRIVATE_KEY_PEM,
    )
    print("\n=== Result ===")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
