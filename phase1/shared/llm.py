"""Groq setup shared by both products."""
import os
import sys

from dotenv import load_dotenv
from groq import AsyncGroq, Groq

from shared import ROOT

LLM_MODEL = "openai/gpt-oss-20b"  # fast + free-tier friendly on Groq (desktop)

# The end-of-conversation memory update isn't time-critical but has to be
# accurate and return clean JSON. The 20b model mixed up who said what and
# once returned JSON that couldn't be parsed, losing a whole call's worth
# of memories.
MEMORY_MODEL = "openai/gpt-oss-120b"

# gpt-oss is a reasoning model: without this it spends the whole token
# budget "thinking" and returns an empty reply.
REASONING_EFFORT = "low"


def api_key() -> str:
    load_dotenv(ROOT / ".env")
    key = os.getenv("GROQ_API_KEY")
    if not key:
        sys.exit(
            "Missing GROQ_API_KEY. Copy .env.example to .env and add your "
            "free key from https://console.groq.com"
        )
    return key


def sync_client() -> Groq:
    return Groq(api_key=api_key())


def async_client() -> AsyncGroq:
    return AsyncGroq(api_key=api_key())
