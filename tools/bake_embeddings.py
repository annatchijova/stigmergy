"""
Bake the pinned provider's embeddings into the browser console.

demo/console.html runs the real decision arithmetic but has no Python and no
model. Rather than let it invent a similarity function and narrate convergence
it did not earn, this script precomputes the SAME embeddings the cluster demo
uses — sentence-transformers all-MiniLM-L6-v2, 384 dimensions, normalized —
for every corpus memory and every demo query, and writes them to
demo/minilm_vectors.js as int8-quantized base64.

Quantization is a SIZE decision, not a decision-path decision: these vectors
only ever produce recall ORDER. Nothing quantized here reaches a verdict —
every verdict in the console is exact BigInt rational arithmetic, and the
audit hashes cover canonical decimal strings, never a float.

Keys are sha256(content)[:16] so console.html can look a vector up by the same
content hash it already seals into MEMORY_STORED.

    pip install sentence-transformers
    python -m tools.bake_embeddings

Re-run this whenever demo/corpus.py changes. If it is never run, console.html
falls back to a labeled lexical provider and says so on the page — the same
is_semantic discipline demo/run_demo.py applies to its own report.
"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import re

from demo.corpus import MISPLACED, QUERIES, THEMES

DEMO_DIR = pathlib.Path(__file__).resolve().parent.parent / "demo"
OUT = DEMO_DIR / "minilm_vectors.js"
CONSOLE = DEMO_DIR / "console.html"
MODEL = "all-MiniLM-L6-v2"

# The console seeds its OWN corpus (see the comment above `const THEMES` in
# console.html: demo/corpus.py is written for the semantic provider and is
# deliberately hard lexically). Both corpora therefore have to be baked, or the
# table covers a corpus the page never embeds and every lookup misses. The page
# refuses a table that does not cover it, so a mismatch is loud — but it is
# cheaper to bake correctly than to be told off by the page.
CONSOLE_TEXT_COUNT = 39  # 3 themes x 8 + 6 misplaced + 3 x 3 queries


def corpus_texts() -> list[str]:
    texts: list[str] = []
    for theme in THEMES:
        texts.extend(THEMES[theme])
    texts.extend(text for _home, _into, text in MISPLACED)
    for theme in QUERIES:
        texts.extend(QUERIES[theme])
    return texts


def console_texts() -> list[str]:
    """The console's inline corpus, read out of the page that owns it.

    console.html has to run from `file://` with nothing installed, so it cannot
    fetch a shared JSON corpus — the texts live inline in the page and this is
    the seam where the two meet. Extraction is strict on purpose: a corpus edit
    that changes the shape fails the bake instead of quietly baking a subset.
    """
    html = CONSOLE.read_text(encoding="utf-8")
    try:
        start = html.index("const THEMES = {")
        end = html.index("const THEME_LIST")
    except ValueError as exc:  # pragma: no cover - structural guard
        raise SystemExit(
            f"Could not locate the corpus block in {CONSOLE.name}: {exc}. "
            "If the page was restructured, update console_texts()."
        ) from exc

    # Corpus entries are the only long double-quoted literals in that block.
    texts = [
        raw.encode().decode("unicode_escape")
        for raw in re.findall(r'"([^"\\\n]{25,})"', html[start:end])
    ]
    if len(texts) != CONSOLE_TEXT_COUNT:
        raise SystemExit(
            f"Extracted {len(texts)} texts from {CONSOLE.name}, expected "
            f"{CONSOLE_TEXT_COUNT}. Baking a partial table is exactly the failure "
            "the console now refuses; fix the extraction or CONSOLE_TEXT_COUNT."
        )
    return texts


def all_texts() -> list[str]:
    """Cluster corpus + console corpus, de-duplicated, order preserved."""
    seen: dict[str, None] = {}
    for text in corpus_texts() + console_texts():
        seen.setdefault(text, None)
    return list(seen)


def quantize_int8(vector) -> str:
    """Symmetric int8 over a unit-normalized vector, base64 for transport."""
    out = bytearray()
    for value in vector:
        q = int(round(float(value) * 127))
        q = max(-127, min(127, q))
        out.append(q & 0xFF)
    return base64.b64encode(bytes(out)).decode("ascii")


def main() -> None:
    from sentence_transformers import SentenceTransformer

    texts = all_texts()
    if len(set(texts)) != len(texts):
        raise SystemExit("Duplicate text in the corpus — content-hash keys would collide.")

    model = SentenceTransformer(MODEL)
    vectors = model.encode(texts, normalize_embeddings=True)
    if vectors.shape[1] != 384:
        raise SystemExit(f"Expected 384 dimensions, got {vectors.shape[1]} — "
                         "the console and schema.sql both pin 384.")

    table = {"__provider": MODEL, "__dim": 384}
    for text, vector in zip(texts, vectors):
        table[hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]] = quantize_int8(vector)

    OUT.write_text(
        "/* Generated by tools/bake_embeddings.py — do not edit by hand.\n"
        f"   Provider: {MODEL}, 384 dimensions, normalized, int8-quantized.\n"
        f"   {len(texts)} texts keyed by sha256(content)[:16]. */\n"
        "window.MINILM_VECTORS = " + json.dumps(table, indent=0, sort_keys=True) + ";\n",
        encoding="utf-8",
    )
    print(f"[bake] {len(texts)} vectors from {MODEL} -> {OUT.relative_to(OUT.parents[1])} "
          f"({OUT.stat().st_size // 1024} KiB)")
    print(f"[bake] covers demo/corpus.py ({len(corpus_texts())} texts) and "
          f"demo/console.html ({len(console_texts())} texts)")


if __name__ == "__main__":
    main()
