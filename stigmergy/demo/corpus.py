"""
STIGMERGY demo — corpus. Pure data, no dependencies, pinned by tests.

Three themed regions, eight memories each, plus MISPLACED: memories
whose content belongs to one theme but are deliberately seeded into
another theme's region. The demo's measurable convergence claim is that
agents — coordinating only through shared state — discover the
misplaced ones and recruitment consensus migrates them home.

That claim is only meaningful under a SEMANTIC provider (MiniLM). Under
the deterministic provider the demo still exercises every mechanism
(storage, recall, reinforcement, signals, migration, audit), but the
report must not narrate convergence — is_semantic gates that narration,
which is the Failure philosophy applied to the demo itself.
"""

from __future__ import annotations

THEMES: dict[str, list[str]] = {
    "cooking": [
        "Caramelizing onions takes forty minutes, not five; low heat and patience.",
        "A splash of pasta water emulsifies the sauce so it clings to the noodles.",
        "Resting a steak after searing lets the juices redistribute through the meat.",
        "Toasting spices in a dry pan wakes up their aromatic oils before grinding.",
        "Sourdough starter doubles fastest around 26 degrees; cold slows fermentation.",
        "Deglazing the pan with wine lifts the browned fond into the sauce.",
        "Blanching green vegetables then shocking them in ice water fixes the color.",
        "Salting eggplant draws out moisture so it fries instead of steaming.",
    ],
    "astronomy": [
        "Betelgeuse is a red supergiant expected to end as a supernova.",
        "Tidal locking keeps one lunar hemisphere permanently facing Earth.",
        "The Andromeda galaxy will merge with the Milky Way in four billion years.",
        "Neutron stars pack a solar mass into a sphere the size of a city.",
        "Jupiter's Great Red Spot is a storm older than telescopic astronomy.",
        "Redshift of distant galaxies is the fingerprint of cosmic expansion.",
        "Europa hides a saltwater ocean beneath kilometers of surface ice.",
        "A pulsar sweeps its radio beam past Earth like a lighthouse.",
    ],
    "databases": [
        "Serializable isolation makes concurrent transactions behave as if sequential.",
        "A write-ahead log turns crashes into recoverable events, not data loss.",
        "B-tree indexes trade write amplification for logarithmic point lookups.",
        "MVCC lets readers proceed without blocking writers by keeping versions.",
        "Consensus protocols like Raft keep replicas agreeing across failures.",
        "A covering index answers the query without touching the base table.",
        "Foreign keys make referential integrity the database's job, not the app's.",
        "Vector indexes approximate nearest neighbors to make similarity search cheap.",
    ],
}

# (home_theme, seeded_into_theme, text) — the convergence targets.
MISPLACED: list[tuple[str, str, str]] = [
    ("cooking", "astronomy",
     "Browning butter until the milk solids toast adds a nutty depth to sauces."),
    ("cooking", "databases",
     "Kneading develops gluten; the windowpane test says when the dough is ready."),
    ("astronomy", "cooking",
     "Saturn's rings are mostly water ice, shepherded by tiny embedded moons."),
    ("astronomy", "databases",
     "The cosmic microwave background is the afterglow of the early universe."),
    ("databases", "cooking",
     "Changefeeds push row changes to consumers instead of making them poll."),
    ("databases", "astronomy",
     "Hash joins build a table in memory and probe it with the larger input."),
]

QUERIES: dict[str, list[str]] = {
    "cooking": [
        "how do I build flavor when cooking vegetables and meat",
        "techniques for better sauces and doughs in the kitchen",
        "why does resting and temperature control matter in cooking",
    ],
    "astronomy": [
        "what do we know about stars, moons and galaxies",
        "strange compact objects and signals in deep space",
        "how planets and their satellites behave",
    ],
    "databases": [
        "how do databases stay correct under concurrency and crashes",
        "indexing strategies and what they cost",
        "how distributed databases replicate and agree",
    ],
}


def region_id_for(theme: str) -> str:
    if theme not in THEMES:
        raise ValueError(f"unknown theme {theme!r}.")
    return f"region-{theme}"


def all_regions() -> list[str]:
    return [region_id_for(t) for t in THEMES]


def seed_items() -> list[tuple[str, str]]:
    """Every (region_id, text) to store — themed memories in their home
    region, misplaced ones in the wrong one. Order is deterministic."""
    items: list[tuple[str, str]] = []
    for theme in THEMES:
        for text in THEMES[theme]:
            items.append((region_id_for(theme), text))
    for _home, seeded_into, text in MISPLACED:
        items.append((region_id_for(seeded_into), text))
    return items
