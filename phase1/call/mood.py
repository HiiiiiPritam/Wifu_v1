"""
Reads the mood of what you just said, so her "hmm..." filler matches it --
"Oh no..." for bad news, "Ooh!" for good news -- instead of a random one
that can land "wow!" on something sad.

VADER does the scoring: a rule-based sentiment model made for short,
casual English. It runs in well under a millisecond with no API call, so
it costs nothing against the latency the filler exists to hide.

Two gaps in VADER are patched with small rules, both found by probing it:
- News with no emotion words scores 0.00 -- "I got the job!!", "Guess
  what, I bought a guitar!" -- so an exclamation on an otherwise neutral
  sentence, or "guess what", counts as excited.
- Questions get a thinking sound rather than an emotional one.

When the signal is genuinely mixed ("I failed one exam but passed the
other") it returns None and she plays no filler at all: silence is better
than the wrong reaction.
"""
import re

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_analyzer = SentimentIntensityAnalyzer()

_QUESTION_START = re.compile(
    r"^(what|how|why|when|where|who|which|do you|did you|are you|can you|could you|"
    r"would you|will you|have you|should|is it|isn't it|was it)\b",
    re.IGNORECASE,
)

_ACKNOWLEDGMENTS = {
    "ok", "okay", "k", "yeah", "yea", "yes", "yep", "yup", "sure", "fine",
    "alright", "right", "cool", "hmm", "hm", "mhm", "uh", "um", "huh", "so",
    "well", "oh", "ah", "got", "it", "i", "see", "good",
}

# compound score thresholds (VADER's range is -1..1)
VERY_NEGATIVE = -0.5
NEGATIVE = -0.1
POSITIVE = 0.1
EXCITED = 0.5
# Both halves this strong at once = mixed news: no filler.
MIXED = 0.2


def classify(text: str) -> str | None:
    """"excited", "positive", "neutral", "question", "negative",
    "very_negative", or None (mixed -- play nothing)."""
    t = text.strip()
    if not t:
        return None
    scores = _analyzer.polarity_scores(t)
    c = scores["compound"]
    if scores["pos"] >= MIXED and scores["neg"] >= MIXED:
        return None
    # "Okay." / "Yeah, sure." score as positive -- "Yeah, sure." even
    # strongly -- because VADER counts those words as happy. They're just
    # acknowledgments, and "Ooh!" in reply to a plain "okay" is exactly the
    # mismatch this module exists to prevent.
    words = set(re.findall(r"[a-z']+", t.lower()))
    if words and words <= _ACKNOWLEDGMENTS:
        return "neutral"
    if c <= VERY_NEGATIVE:
        return "very_negative"
    if c <= NEGATIVE:
        return "negative"
    if c >= EXCITED:
        return "excited"
    if c >= POSITIVE:
        return "positive"
    # Neutral by VADER's lexicon -- look for the cues it misses.
    lower = t.lower()
    if t.endswith("?") or _QUESTION_START.match(lower):
        return "question"
    if "guess what" in lower or t.endswith("!"):
        return "excited"
    return "neutral"
