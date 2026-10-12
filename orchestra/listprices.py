"""Anthropic's public list prices: the default when you have no price file of your own.

Per million tokens, checked on AS_OF against SOURCE. Every figure priced from this table is
labelled "list price, as of AS_OF"; your own price file always wins, and ORCHESTRA_PRICES=off
turns cost off altogether.

Two choices keep it from understating, as orchestra.pricing promises:
* cache writes are priced at the one-hour rate (2x input). Claude Code writes most of its cache
  for an hour (72% of cache writes in the transcripts this was checked against); a five-minute
  write (1.25x input) is overstated.
* Claude Haiku 5.5 is priced at its rate for prompts over 100K tokens; shorter prompts cost a fifth.
Models not listed (older Opus 4 and Sonnet 4.5 and before) stay unpriced and are reported as
such, rather than guessed.
"""

from typing import Dict, Tuple

from orchestra.pricing import PriceTable

AS_OF = "2026-10-12"
SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"

# The cheaper model orchestra.advice suggests for an agent that only read and searched, by family:
# (the alias for an agent's definition, the model id it is priced as).
CHEAPER: Dict[str, Tuple[str, str]] = {"opus": ("sonnet", "claude-sonnet-5-5"),
                                       "sonnet": ("haiku", "claude-haiku-5-5")}


def _p(input_: float, output: float, cache_read: float) -> Dict[str, float]:
    return {"input": input_, "output": output, "cache_read": cache_read, "cache_create": input_ * 2}


# First match wins, so a model's own row comes before its family's.
MODELS = (
    ("claude-fable-5-1*", _p(10.0, 50.0, 0.25)),
    ("claude-mythos-5-1*", _p(10.0, 50.0, 0.25)),
    ("claude-fable-5*", _p(10.0, 50.0, 1.0)),
    ("claude-mythos-5*", _p(10.0, 50.0, 1.0)),
    ("claude-opus-5-5*", _p(4.0, 20.0, 0.20)),
    ("claude-opus-5*", _p(5.0, 25.0, 0.50)),
    ("claude-opus-4-[678]*", _p(5.0, 25.0, 0.50)),
    ("claude-sonnet-5-5*", _p(2.0, 10.0, 0.20)),
    ("claude-sonnet-5*", _p(2.0, 10.0, 0.20)),
    ("claude-sonnet-4-6*", _p(3.0, 15.0, 0.30)),
    ("claude-haiku-5-5*", _p(0.50, 2.50, 0.05)),
    ("claude-haiku-4-5*", _p(1.0, 5.0, 0.10)),
)


def table() -> PriceTable:
    t = PriceTable.parse({"currency": "USD", "models": dict(MODELS)})
    t.source, t.as_of = "list", AS_OF
    return t
