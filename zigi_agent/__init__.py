"""Zigi agent service (design 5.8): an LLM agent, built on Pydantic AI, that talks to the caller and
uses Zigi (MTN's chatbot behind Sunshine Conversations) as a tool. Runs apart from the voice gateway."""

import os as _os

_os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
