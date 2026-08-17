"""
Pure-function tests for the seam between tools/bake_embeddings.py and
demo/console.html — the one place in the project where a Python build step and a
hand-written page have to agree about content they each hold separately.

Why this file exists: the console looks vectors up by sha256(content)[:16], and
the bake tool used to emit ONLY demo/corpus.py's texts while the console seeds a
corpus of its own. One text of thirty-nine overlapped. Every other lookup missed,
distance() answered its null default of 1, recall order collapsed to insertion
order — and the page relabeled itself as the pinned semantic model while doing
it. That is the fabricated certainty is_semantic exists to prevent, arriving
through a build artifact instead of through a provider.

These tests pin the contract so the two halves cannot drift apart again in
silence. They read the page as text; they do not execute JavaScript (the
browser-side behaviour of all three table states is exercised by hand against
demo/console.html, and the checks below are what make that drift-proof).
"""

from __future__ import annotations
import hashlib, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.bake_embeddings import (
    CONSOLE, CONSOLE_TEXT_COUNT, all_texts, console_texts, corpus_texts, quantize_int8,
)

failures = []

def check(name, fn):
    try:
        fn(); print(f"  ok    {name}")
    except AssertionError as e:
        failures.append(name); print(f"  FAIL  {name}: {e}")
    except Exception as e:
        failures.append(name); print(f"  ERROR {name}: {type(e).__name__}: {e}")

HTML = CONSOLE.read_text(encoding="utf-8")
KEY = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

# --- the bake output must cover the corpus the console actually embeds ---------

def t_console_corpus_extraction_has_the_declared_shape():
    texts = console_texts()
    assert len(texts) == CONSOLE_TEXT_COUNT, f"{len(texts)} texts, expected {CONSOLE_TEXT_COUNT}"
    assert len(set(texts)) == len(texts), "duplicate console text — content-hash keys would collide"
    assert all(len(t) >= 25 for t in texts)

def t_baked_table_covers_every_console_text():
    """The regression. Before the fix this failed 38 times out of 39."""
    baked = {KEY(t) for t in all_texts()}
    missing = [t for t in console_texts() if KEY(t) not in baked]
    assert not missing, f"{len(missing)} console texts absent from the bake: {missing[:2]}"

def t_baked_table_still_covers_every_cluster_text():
    baked = {KEY(t) for t in all_texts()}
    missing = [t for t in corpus_texts() if KEY(t) not in baked]
    assert not missing, f"{len(missing)} cluster-corpus texts absent from the bake"

def t_union_is_deduplicated_not_concatenated():
    texts = all_texts()
    assert len(set(texts)) == len(texts), "all_texts() must not repeat a text"
    assert set(texts) == set(corpus_texts()) | set(console_texts())

# --- the page must actually load what the bake writes -------------------------

def t_console_loads_the_baked_table():
    """README promised the page 'picks it up automatically'; it had no such tag."""
    assert re.search(r'<script\s+src="minilm_vectors\.js"', HTML), \
        "console.html does not load demo/minilm_vectors.js — the bake output would be inert"

def t_console_skips_metadata_keys():
    """__provider is a plain string; atob() throws InvalidCharacterError on it,
    which killed the whole page script at module init."""
    assert 'k.startsWith("__")' in HTML, "metadata keys must be skipped before b64ToInt8"

def t_console_refuses_a_partial_table():
    assert "VECTOR_TABLE_GAP" in HTML, "no coverage guard in console.html"
    assert "missing === 0" in HTML, "the guard must require full coverage, not any coverage"

def t_gap_is_reported_on_the_page_not_just_in_devtools():
    assert "GAP_NOTE" in HTML and '$("provider-note").innerHTML = GAP_NOTE' in HTML, \
        "a refused table must be stated in the page's own provider note"

# --- quantization stays a size decision, never a decision-path one ------------

def t_quantize_is_symmetric_and_bounded():
    assert quantize_int8([0.0] * 384) == quantize_int8([0.0] * 384)
    for value in (1.0, -1.0, 2.0, -2.0):
        raw = quantize_int8([value])
        assert len(raw) == 4, raw       # one byte, base64-padded
    assert quantize_int8([1.0]) == quantize_int8([2.0]), "must clamp at +127"
    assert quantize_int8([-1.0]) == quantize_int8([-2.0]), "must clamp at -127"

def t_no_float_reaches_a_console_verdict():
    """The engine constants must remain BigInt rationals in the page."""
    for constant in ("REINFORCEMENT_ALPHA", "BETA", "ENTER", "EXIT", "CONSENSUS_QUORUM"):
        m = re.search(rf"const {constant}\s*=\s*(\w+)\(", HTML)
        assert m, f"{constant} not found in console.html"
        assert m.group(1) in ("Fr", "F1", "F0"), f"{constant} is not a rational: {m.group(1)}"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("t_")]
    print(f"running {len(tests)} pure-function tests\n")
    for name, fn in tests:
        check(name, fn)
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
