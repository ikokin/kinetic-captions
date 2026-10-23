#!/usr/bin/env python3
"""Optional: let a model pick the emphasis words — one batched call per video.

The deterministic scorer in kinetic.py hears only HOW something was said
(how far a word was stretched beyond its syllable count). A model understands
WHAT was said. This module is a drop-in upgrade: on any failure it returns
None and kinetic.py falls back to the scorer.

Enable with KIN_EMPHASIS=llm. Model via KIN_MODEL (default claude-opus-5).
Credentials come from ANTHROPIC_API_KEY or an `ant auth login` profile.
"""
import os
from typing import List, Optional

MODEL = os.environ.get("KIN_MODEL", "claude-opus-5")
EFFORT = os.environ.get("KIN_EFFORT", "low")   # the task is simple; stay cheap

SYSTEM = """You place emphasis in short-form video captions.

You receive spoken phrases split into numbered words. For each phrase return
the indices of the 1-2 words that CARRY THE MEANING — the ones a viewer
scrolling without sound must catch.

Rules:
- Pick content words: nouns, numbers, names, strong verbs and adjectives.
  Never pick prepositions, conjunctions, particles, pronouns or fillers.
- A multi-word proper name ("App Store", "YouTube Shorts") is returned whole
  or not at all.
- If a phrase carries nothing substantial (a connective, an interjection),
  return an empty list. Empty beats an arbitrary word.
- Judge the phrase as a whole, not each word in isolation."""


def pick(phrases: List[List[str]], timeout: float = 90.0) -> Optional[List[List[int]]]:
    """phrases: a list of phrases, each a list of words.

    Returns word indices (0-based) per phrase, or None when the model could
    not be reached — in which case the caller uses its own scorer.
    """
    if not phrases:
        return []
    try:
        import anthropic
        from pydantic import BaseModel
    except ImportError:
        return None

    class Phrase(BaseModel):
        phrase: int
        words: List[int]

    class Picks(BaseModel):
        picks: List[Phrase]

    body = "\n".join(
        f"[{pi}] " + "  ".join(f"{wi}:{w}" for wi, w in enumerate(words))
        for pi, words in enumerate(phrases))

    try:
        client = anthropic.Anthropic(timeout=timeout, max_retries=3)
        resp = client.messages.parse(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            output_config={"effort": EFFORT},
            messages=[{"role": "user",
                       "content": f"Phrases:\n{body}\n\nReturn the emphasis for each."}],
            output_format=Picks,
        )
    except Exception as e:                      # network, key, rate limit, anything
        print(f"[emphasis] model unavailable ({type(e).__name__}), using the scorer")
        return None

    parsed = getattr(resp, "parsed_output", None)
    if parsed is None:
        return None
    out: List[List[int]] = [[] for _ in phrases]
    for item in parsed.picks:
        if 0 <= item.phrase < len(phrases):
            n = len(phrases[item.phrase])
            out[item.phrase] = sorted({w for w in item.words if 0 <= w < n})[:3]

    u = getattr(resp, "usage", None)
    if u:
        print(f"[emphasis] {MODEL}: {u.input_tokens} in / {u.output_tokens} out tokens")
    return out
