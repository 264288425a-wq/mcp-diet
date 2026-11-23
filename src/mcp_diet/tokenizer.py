"""Token counting utilities with optional tiktoken acceleration and stdlib fallback."""

from __future__ import annotations

import json
import math
import re
from typing import Any, Protocol

_TOKEN_CHUNK_RE = re.compile(
    r"""
    [A-Za-z]+(?:_[A-Za-z]+)*   # words and snake_case identifiers
    |[0-9]+                    # numbers
    |[\u4e00-\u9fff]           # CJK characters (~1-2 tokens each)
    |[^\sA-Za-z0-9\u4e00-\u9fff]+  # JSON/code punctuation runs
    """,
    re.VERBOSE,
)

# Approximate input token pricing (USD per 1M tokens) for quick cost estimation.
MODEL_INPUT_PRICE_PER_1M: dict[str, float] = {
    "gpt-5": 1.25,
    "gpt-4.1": 2.00,
    "gpt-4o": 2.50,
    "gpt-4o-mini": 0.15,
    "claude-sonnet": 3.00,
}


class TokenCounter(Protocol):
    """Protocol implemented by all token counters."""

    @property
    def name(self) -> str:
        """Human-readable tokenizer identifier."""
        ...

    def count_text(self, text: str) -> int:
        """Count tokens in a raw string."""
        ...

    def count_json(self, data: Any) -> int:
        """Count tokens in a JSON-serializable object."""
        ...


class HeuristicTokenCounter:
    """Zero-dependency calibrated BPE-style estimator for JSON schemas and English prose.

    Tuned against OpenAI o200k_base behavior on MCP tool definitions:
    - Short words and common JSON keys count as ~1 token.
    - Long identifiers split roughly every ~4.5 chars or on underscores/camelCase.
    - Punctuation runs compress slightly (e.g. `": "` or `"}, {"` merge in BPE).
    - CJK characters count as ~1.4 tokens on average.
    """

    @property
    def name(self) -> str:
        return "heuristic-bpe"

    def count_text(self, text: str) -> int:
        if not text:
            return 0

        total = 0.0
        for match in _TOKEN_CHUNK_RE.finditer(text):
            chunk = match.group(0)
            first = chunk[0]
            if "\u4e00" <= first <= "\u9fff":
                total += 1.4
            elif first.isdigit():
                total += max(1.0, math.ceil(len(chunk) / 3.0))
            elif first.isalpha():
                # Split snake_case / camelCase segments subtly like BPE merges
                parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)", chunk)
                if not parts:
                    total += max(1.0, math.ceil(len(chunk) / 4.5))
                else:
                    for part in parts:
                        total += 1.0 if len(part) <= 6 else math.ceil(len(part) / 4.5)
                    # Underscore separators add small overhead
                    total += chunk.count("_") * 0.4
            else:
                # Punctuation / JSON brackets
                total += max(0.65, len(chunk) * 0.65)

        # Account lightly for multi-newline formatting
        total += text.count("\n") * 0.35
        return max(1, round(total))

    def count_json(self, data: Any) -> int:
        serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return self.count_text(serialized)


class TiktokenCounter:
    """Exact OpenAI BPE token counter backed by `tiktoken` (optional extra)."""

    def __init__(self, encoding_name: str = "o200k_base") -> None:
        import tiktoken  # type: ignore[import-not-found]

        self._encoding_name = encoding_name
        self._encoding = tiktoken.get_encoding(encoding_name)

    @property
    def name(self) -> str:
        return f"tiktoken:{self._encoding_name}"

    def count_text(self, text: str) -> int:
        if not text:
            return 0
        return len(self._encoding.encode(text, disallowed_special=()))

    def count_json(self, data: Any) -> int:
        serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return self.count_text(serialized)


def get_token_counter(prefer_tiktoken: bool = True, encoding: str = "o200k_base") -> TokenCounter:
    """Return a TiktokenCounter when installed and requested, else HeuristicTokenCounter."""
    if prefer_tiktoken:
        try:
            return TiktokenCounter(encoding_name=encoding)
        except Exception:
            pass
    return HeuristicTokenCounter()


def estimate_cost_per_1k_turns(tokens: int, model: str = "gpt-5") -> float:
    """Estimate USD cost of sending `tokens` of tool menu overhead across 1,000 LLM turns."""
    price = MODEL_INPUT_PRICE_PER_1M.get(model, 1.25)
    return (tokens * 1000 / 1_000_000) * price
