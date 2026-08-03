#!/usr/bin/env python3
"""
Prepare Wikipedia dump articles as per-file UTF-8 text for HSAIDS evaluation.

Usage
-----
# Download the latest dump and extract up to 500k articles as wiki_v1/:
    python prepare_wikipedia.py --output wiki_v1 --limit 500000

# Produce a mutated version simulating a backup revision (10% of articles altered):
    python prepare_wikipedia.py --output wiki_v2 --limit 500000 --mutate 0.10 --source wiki_v1

The script accepts either a local .xml.bz2 dump file via --dump-file or downloads
the latest enwiki articles dump automatically.

Output layout
--------------
wiki_v1/
    00/
        00000.txt
        00001.txt
        ...
    01/
        ...

Each article is stored as a single UTF-8 .txt file.  The two-level shard
directory keeps directory entry counts manageable for large article sets.
"""

from __future__ import annotations

import argparse
import bz2
import hashlib
import os
import random
import re
import sys
import urllib.request
from pathlib import Path
from typing import Iterator, Optional
from xml.etree import ElementTree as ET


# ---------------------------------------------------------------------------
# Dump download
# ---------------------------------------------------------------------------

DUMP_INDEX_URL = (
    "https://dumps.wikimedia.org/enwiki/latest/"
    "enwiki-latest-pages-articles-multistream.xml.bz2"
)


def download_dump(dest: Path, url: str = DUMP_INDEX_URL) -> Path:
    """Stream-download the Wikipedia dump to *dest* if not already present."""
    if dest.exists():
        print(f"[prepare] Using existing dump file: {dest}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"[prepare] Downloading dump from {url}")
    print("[prepare] This may take a long time (~22 GB).  Press Ctrl-C to abort.")

    def _report(block_count, block_size, total_size):
        downloaded = block_count * block_size
        if total_size > 0:
            pct = min(downloaded / total_size * 100, 100)
            bar = int(pct / 2)
            print(
                f"\r  [{'#' * bar}{' ' * (50 - bar)}] {pct:5.1f}%  "
                f"{downloaded / 1e9:.2f} GB",
                end="",
                flush=True,
            )

    urllib.request.urlretrieve(url, dest, reporthook=_report)
    print()
    return dest


# ---------------------------------------------------------------------------
# XML streaming parser
# ---------------------------------------------------------------------------

_NS = "{http://www.mediawiki.org/xml/export-0.11/}"


def _remove_templates(text: str) -> str:
    """Remove {{ ... }} templates in a single O(n) pass using depth tracking."""
    result: list[str] = []
    i = 0
    n = len(text)
    depth = 0
    seg_start = 0
    while i < n - 1:
        if text[i : i + 2] == "{{":
            if depth == 0:
                result.append(text[seg_start:i])
            depth += 1
            i += 2
        elif text[i : i + 2] == "}}":
            if depth > 0:
                depth -= 1
                if depth == 0:
                    seg_start = i + 2
            i += 2
        else:
            i += 1
    if depth == 0:
        result.append(text[seg_start:])
    return "".join(result)


def _strip_markup(text: str) -> str:
    """Very lightweight wikitext cleanup — removes the noisiest markup."""
    text = _remove_templates(text)
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", text)  # [[link|label]]
    text = re.sub(r"\[https?://\S+\s+([^\]]+)\]", r"\1", text)      # [url label]
    text = re.sub(r"={2,}([^=]+)={2,}", r"\1", text)                # ==headings==
    text = re.sub(r"'{2,}", "", text)                                # bold/italic
    text = re.sub(r"<[^>]+>", "", text)                              # HTML tags
    text = re.sub(r"\n{3,}", "\n\n", text)                           # blank lines
    return text.strip()


def iter_articles(dump_path: Path) -> Iterator[tuple[str, str]]:
    """Yield (title, plain_text) from a bz2-compressed Wikipedia XML dump."""
    opener = bz2.open if dump_path.suffix == ".bz2" else open
    with opener(str(dump_path), "rb") as fh:
        inside_page = False
        title = ""
        text_buf: list[str] = []
        in_text = False
        ns = 0

        for event, elem in ET.iterparse(fh, events=("start", "end")):
            tag = elem.tag.replace(_NS, "")

            if event == "start":
                if tag == "page":
                    inside_page = True
                    title = ""
                    text_buf = []
                    ns = 0
                    in_text = False
                elif tag == "text" and inside_page:
                    in_text = True

            elif event == "end":
                if tag == "title" and inside_page:
                    title = (elem.text or "").strip()
                elif tag == "ns" and inside_page:
                    try:
                        ns = int(elem.text or "0")
                    except ValueError:
                        ns = -1
                elif tag == "text" and inside_page:
                    in_text = False
                    if elem.text:
                        text_buf.append(elem.text)
                elif tag == "page":
                    if ns == 0 and title and text_buf:
                        raw = "".join(text_buf)
                        # Skip redirects
                        if not raw.lstrip().upper().startswith("#REDIRECT"):
                            yield title, _strip_markup(raw)
                    inside_page = False
                    elem.clear()


# ---------------------------------------------------------------------------
# Article writer
# ---------------------------------------------------------------------------

def _article_path(output_dir: Path, article_index: int) -> Path:
    shard = f"{article_index // 10000:02d}"
    filename = f"{article_index:06d}.txt"
    return output_dir / shard / filename


def write_articles(
    dump_path: Path,
    output_dir: Path,
    limit: int,
    min_chars: int = 500,
) -> int:
    """Extract up to *limit* articles from *dump_path* into *output_dir*."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for title, text in iter_articles(dump_path):
        if len(text) < min_chars:
            continue
        path = _article_path(output_dir, written)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{title}\n\n{text}\n", encoding="utf-8")
        written += 1
        if written % 10000 == 0:
            print(f"[prepare] {written:,} articles written to {output_dir} …")
        if written >= limit:
            break
    print(f"[prepare] Done. {written:,} articles written to {output_dir}")
    return written


# ---------------------------------------------------------------------------
# Mutation (simulates a backup revision)
# ---------------------------------------------------------------------------

_FILLER = (
    " In recent developments, researchers have revisited this topic extensively."
    " Several alternative perspectives have been proposed in the literature."
    " Further studies are ongoing."
)


def _mutate_text(text: str, rng: random.Random) -> str:
    """Alter ~15% of lines in *text* to simulate an edited article revision."""
    lines = text.splitlines(keepends=True)
    mutated: list[str] = []
    for line in lines:
        if len(line) > 40 and rng.random() < 0.15:
            # Insert filler in the middle of the line
            mid = len(line) // 2
            line = line[:mid] + _FILLER + line[mid:]
        mutated.append(line)
    return "".join(mutated)


def mutate_from_source(
    source_dir: Path,
    output_dir: Path,
    mutate_fraction: float,
    seed: int = 42,
) -> int:
    """Copy articles from *source_dir* to *output_dir*, mutating *mutate_fraction* of them."""
    output_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    all_files = sorted(source_dir.rglob("*.txt"))
    written = 0
    for src_path in all_files:
        text = src_path.read_text(encoding="utf-8")
        if rng.random() < mutate_fraction:
            text = _mutate_text(text, rng)
        rel = src_path.relative_to(source_dir)
        dest = output_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        written += 1
        if written % 10000 == 0:
            print(f"[prepare] {written:,} articles copied/mutated to {output_dir} …")
    print(f"[prepare] Done. {written:,} articles in {output_dir} ({mutate_fraction*100:.0f}% mutated)")
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare Wikipedia dump as per-article text files for HSAIDS evaluation."
    )
    parser.add_argument(
        "--dump-file",
        help="Path to a local enwiki-*-pages-articles*.xml.bz2 file. "
             "If omitted the latest dump is downloaded automatically.",
    )
    parser.add_argument(
        "--dump-cache",
        default="wiki_dump_cache/enwiki-latest-pages-articles.xml.bz2",
        help="Where to save the downloaded dump (default: wiki_dump_cache/…).",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output directory for article text files (e.g. wiki_v1).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500_000,
        help="Maximum number of articles to extract (default: 500000).",
    )
    parser.add_argument(
        "--min-chars",
        type=int,
        default=500,
        help="Skip articles shorter than this many characters (default: 500).",
    )
    parser.add_argument(
        "--mutate",
        type=float,
        default=0.0,
        help="Fraction of articles to mutate [0.0–1.0]. Requires --source.",
    )
    parser.add_argument(
        "--source",
        help="Source article directory for mutation mode (e.g. wiki_v1). "
             "Required when --mutate > 0.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for mutation (default: 42).",
    )
    args = parser.parse_args()

    output_dir = Path(args.output)

    if args.mutate > 0:
        if not args.source:
            parser.error("--source is required when --mutate > 0")
        source_dir = Path(args.source)
        if not source_dir.is_dir():
            parser.error(f"--source directory does not exist: {source_dir}")
        mutate_from_source(
            source_dir=source_dir,
            output_dir=output_dir,
            mutate_fraction=args.mutate,
            seed=args.seed,
        )
        return

    # Normal extraction mode
    if args.dump_file:
        dump_path = Path(args.dump_file)
        if not dump_path.exists():
            sys.exit(f"Dump file not found: {dump_path}")
    else:
        dump_path = download_dump(Path(args.dump_cache))

    write_articles(
        dump_path=dump_path,
        output_dir=output_dir,
        limit=args.limit,
        min_chars=args.min_chars,
    )


if __name__ == "__main__":
    main()
