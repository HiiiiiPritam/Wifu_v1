"""
Mood detection for her fillers -- no server or API needed:

    python -m call.test_mood
"""
import sys

from call.mood import classify

# (what you said, acceptable moods)
CASES = [
    ("My dog died this morning.", {"very_negative"}),
    ("Ugh, my boss yelled at me again.", {"negative", "very_negative"}),
    ("I miss you", {"negative"}),  # -> "Aww..."
    ("The exam got cancelled.", {"negative"}),
    ("I am really tired and stressed", {"negative", "very_negative"}),
    # Good news with no emotion words -- VADER alone scores these 0.00.
    ("I got the job!!", {"excited"}),
    ("Guess what, I bought a guitar!", {"excited"}),
    ("I am so so happy today", {"excited"}),
    ("I love you so much", {"excited"}),
    ("Not bad, actually.", {"positive"}),
    ("I just got back from work.", {"neutral"}),
    # Acknowledgments: VADER calls "okay" happy; she must not say "Oh, nice!"
    ("Okay.", {"neutral"}),
    ("Yeah, sure.", {"neutral"}),
    ("What do you think about that?", {"question"}),
    ("how was your day", {"question"}),
    ("Why are you being so mean to me?", {"question", "negative"}),
    # Mixed news: a gentle "Oh..." or nothing, never "Ooh!".
    ("I failed my exam but I passed the other one.", {"negative", None}),
]


def main() -> None:
    failed = 0
    for text, acceptable in CASES:
        got = classify(text)
        ok = got in acceptable
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {str(got):14} {text}")
    print("\nALL PASSED" if not failed else f"\n{failed} FAILED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
