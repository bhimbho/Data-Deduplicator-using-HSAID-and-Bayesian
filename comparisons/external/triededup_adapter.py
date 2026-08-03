#!/usr/bin/env python3
"""
Adapter that runs the vendored TrieDedup algorithm (vendor/triededup/Python/lib)
over our chunk hash digests instead of DNA reads.

Scope note (see docs/COMPARISON_PLAN.md): TrieDedup's core contribution is
tolerating ambiguous 'N' bases during exact-sequence comparison. SHA-256 hex
digests have no equivalent of a low-quality/ambiguous base, so running
TrieDedup here exercises only its trie-matching speed and memory footprint on
a large set of fixed-length strings -- not its ambiguous-base handling. This
is a fair speed/memory comparison, not a claim that TrieDedup was designed
for this use case.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Dict, List

VENDOR_ROOT = Path(__file__).resolve().parent.parent.parent / "vendor" / "triededup" / "Python"
if str(VENDOR_ROOT) not in sys.path:
    sys.path.insert(0, str(VENDOR_ROOT))

from lib.restrictedDict import restrictedListDict  # noqa: E402
from lib.trie import collapseSeq as trie_collapseSeq  # noqa: E402
from lib.pairwise import collapseSeq as pairwise_collapseSeq  # noqa: E402

HEX_ALPHABET = "0123456789abcdef"
# TrieDedup requires at least one "ambiguous" symbol; our hashes never contain
# it, so max_missing=0 means no digest is ever treated as fuzzy-matchable.
_PLACEHOLDER_AMBIGUOUS = "N"

_alphabet_registered = False


def _ensure_alphabet_registered() -> None:
    global _alphabet_registered
    if _alphabet_registered:
        return
    restrictedListDict.addAllowedKeys(HEX_ALPHABET + _PLACEHOLDER_AMBIGUOUS)
    _alphabet_registered = True


def dedup_trie(chunk_hashes: List[str]) -> Dict[str, float]:
    """Run TrieDedup's trie-based exact matcher over a list of hex digest strings."""
    _ensure_alphabet_registered()
    start = time.perf_counter()
    uniq_idx_vec, time_spent = trie_collapseSeq(
        chunk_hashes,
        allowed_symbols=HEX_ALPHABET + _PLACEHOLDER_AMBIGUOUS,
        ambiguous_symbols=_PLACEHOLDER_AMBIGUOUS,
        is_input_sorted=True,  # no ambiguous chars present, so any order is "sorted by N-count"
        max_missing=0,
    )
    wall_elapsed = time.perf_counter() - start
    return {
        "method": "triededup_trie",
        "input_count": len(chunk_hashes),
        "unique_count": len(uniq_idx_vec),
        "duplicate_count": len(chunk_hashes) - len(uniq_idx_vec),
        "reported_time_s": time_spent,
        "wall_time_s": wall_elapsed,
    }


def dedup_pairwise(chunk_hashes: List[str]) -> Dict[str, float]:
    """Run TrieDedup's O(n^2) pairwise comparison baseline (their own slow path)."""
    start = time.perf_counter()
    uniq_idx_vec, time_spent = pairwise_collapseSeq(
        chunk_hashes,
        max_missing=0,
    )
    wall_elapsed = time.perf_counter() - start
    return {
        "method": "triededup_pairwise",
        "input_count": len(chunk_hashes),
        "unique_count": len(uniq_idx_vec),
        "duplicate_count": len(chunk_hashes) - len(uniq_idx_vec),
        "reported_time_s": time_spent,
        "wall_time_s": wall_elapsed,
    }
