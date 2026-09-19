"""
Plumbing shared by both products -- desktop/ (Live2D companion at your PC)
and call/ (phone calls). Only low-level pieces live here: the Groq client,
speech-to-text, text-to-speech, and long-term memory.

Deliberately NOT shared: personas, prompts, openers, timing, settings.
Those are each product's own concern and live in its own package.

Memory IS shared on purpose -- it's the same "her" in both places, so
something you tell her on the laptop can come up on a call.
"""
from pathlib import Path

# phase1/ -- where .env, memory.json, device_token.json etc. live. Both
# products resolve data files against this rather than their own folder,
# so the data stayed put when the code was split.
ROOT = Path(__file__).resolve().parent.parent
