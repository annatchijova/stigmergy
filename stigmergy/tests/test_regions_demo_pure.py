"""
Pure-function tests for ops.regions (boundary validators, lineage
discipline) and demo.corpus (data integrity — the demo's convergence
claim depends on the corpus being what it says it is).
"""

from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo.corpus import MISPLACED, QUERIES, THEMES, all_regions, region_id_for, seed_items
from ops.regions import _REGION_ID_PATTERN, create_region

failures = []

def check(name, fn):
    try:
        fn(); print(f"  ok    {name}")
    except AssertionError as e:
        failures.append(name); print(f"  FAIL  {name}: {e}")
    except Exception as e:
        failures.append(name); print(f"  ERROR {name}: {type(e).__name__}: {e}")

def expect_raises(exc_type, fn):
    try: fn()
    except exc_type: return
    except Exception as e:
        raise AssertionError(f"expected {exc_type.__name__}, got {type(e).__name__}: {e}")
    raise AssertionError(f"expected {exc_type.__name__}, nothing raised")

KW = dict(node_id="n", locality="demo", reason="test")

# --- create_region boundaries (all fire before any cursor use) -----------------

def t_region_id_required_and_shaped():
    expect_raises(ValueError, lambda: create_region(None, region_id="  ", **KW))
    expect_raises(ValueError, lambda: create_region(None, region_id="x" * 65, **KW))
    expect_raises(ValueError, lambda: create_region(None, region_id="has space", **KW))
    expect_raises(ValueError, lambda: create_region(None, region_id="-leading", **KW))

def t_locality_required():
    expect_raises(ValueError, lambda: create_region(
        None, node_id="n", region_id="r1", locality="  ", reason="test"))

def t_generation_type_and_range():
    expect_raises(TypeError, lambda: create_region(
        None, region_id="r1", generation=True, **KW))
    expect_raises(TypeError, lambda: create_region(
        None, region_id="r1", generation=2.0, **KW))
    expect_raises(ValueError, lambda: create_region(
        None, region_id="r1", generation=0, **KW))

def t_lineage_root_means_gen1_no_parent():
    # Invariant 4 discipline: the two arrive together or not at all.
    expect_raises(ValueError, lambda: create_region(
        None, region_id="r1", generation=2, **KW))                    # gen>1, no parent
    expect_raises(ValueError, lambda: create_region(
        None, region_id="r1", parent_region="p", generation=1, **KW)) # parent, gen 1

def t_parent_must_be_nonempty_when_given():
    expect_raises(ValueError, lambda: create_region(
        None, region_id="r1", parent_region="  ", generation=2, **KW))

# --- corpus integrity ------------------------------------------------------------

def t_every_theme_has_texts_and_queries():
    assert set(THEMES) == set(QUERIES)
    for theme, texts in THEMES.items():
        assert texts and all(t.strip() for t in texts), theme
        assert QUERIES[theme] and all(q.strip() for q in QUERIES[theme]), theme

def t_all_texts_globally_unique():
    texts = [t for ts in THEMES.values() for t in ts] + [t for _, _, t in MISPLACED]
    assert len(texts) == len(set(texts)), "duplicate corpus text"

def t_misplaced_are_actually_misplaced_and_themes_valid():
    for home, seeded_into, _text in MISPLACED:
        assert home in THEMES and seeded_into in THEMES
        assert home != seeded_into, "a 'misplaced' memory seeded at home is not misplaced"

def t_every_theme_has_convergence_targets():
    # the demo's claim covers all three regions, both directions
    assert {h for h, _, _ in MISPLACED} == set(THEMES)
    assert {s for _, s, _ in MISPLACED} == set(THEMES)

def t_seed_items_cover_everything_once():
    items = seed_items()
    expected = sum(len(ts) for ts in THEMES.values()) + len(MISPLACED)
    assert len(items) == expected
    assert len({text for _, text in items}) == expected

def t_misplaced_seed_into_wrong_region():
    items = dict((text, rid) for rid, text in seed_items())
    for home, seeded_into, text in MISPLACED:
        assert items[text] == region_id_for(seeded_into) != region_id_for(home)

def t_region_ids_pass_the_regions_module_validation():
    for rid in all_regions():
        assert _REGION_ID_PATTERN.match(rid) and len(rid) <= 64, rid

def t_unknown_theme_rejected():
    expect_raises(ValueError, lambda: region_id_for("alchemy"))


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("t_")]
    print(f"running {len(tests)} pure-function tests\n")
    for name, fn in tests:
        check(name, fn)
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
