-- =============================================================================
-- STIGMERGY — Core Schema (Phase 1)
-- =============================================================================
-- Requires CockroachDB v25.2+ with vector indexing enabled:
--   SET CLUSTER SETTING feature.vector_index.enabled = true;
--
-- Design invariants enforced or supported by this schema (see ARCHITECTURE.md):
--   - Invariant 1: regions are logical prefix partitions, not physical nodes.
--   - Invariant 2: no column here is ever written by an LLM decision.
--   - Invariant 3: every state-changing write must accompany an audit_chain
--     row in the same transaction, with an explicit event_type AND reason.
--     audit_chain.reason is NOT NULL — an unreasoned audit event cannot exist.
--   - Invariant 4: memory_regions carries lineage (generation, parent_region).
--   - Invariant 5: audit_chain is per-node; merkle_snapshots is the only
--     cross-node ledger, and it is itself chained (parent_snapshot) and
--     structurally prevented from forking (see UNIQUE constraints below).
--   - Invariant 6: migration cooldown enforced via last_migrated_at guard
--     in application-layer UPDATE statements (see ops/migration.sql pattern).
--   - Invariant 8: memories are never deleted. FORGOTTEN and ORPHANED are
--     states, not row removals. recruitment_signals.EXPIRED rows are
--     historical state, not garbage — never attach a row-level TTL policy
--     to this table. Doing so would be a silent deletion that violates the
--     spirit of Invariant 8 even though its letter only names memories.
--
-- FOREIGN KEYS AND DELETION — deliberate absence of ON DELETE actions:
--   No FK in this schema declares ON DELETE CASCADE or SET NULL. This is
--   intentional, not an oversight. Under Invariant 8 nothing is ever
--   deleted, so the default RESTRICT behavior is the correct one: an
--   accidental DELETE against a referenced row fails loudly instead of
--   silently cascading destruction through the memory field.
--
-- DECIMAL PRECISION — canonical quantization scale:
--   All [0,1]-bounded values use DECIMAL(11,10). This is not cosmetic.
--   audit_chain.entry_hash is computed over payloads containing these
--   values; a canonical scale is required for hash determinism across
--   nodes. The application MUST quantize (Fraction -> Decimal, scale 10,
--   deterministic rounding mode documented in one place) BEFORE insert.
--   Relying on the database to round implicitly is forbidden — implicit
--   rounding is non-determinism smuggled in through the driver.
--
-- ON UPDATE now() — supported natively by CockroachDB (v21.2+), verified
--   against v26.2 docs. Two caveats, documented so nobody trips on them:
--   (1) the expression does NOT fire if a statement writes that column
--       directly — never set updated_at manually;
--   (2) an ON UPDATE expression cannot coexist with a FOREIGN KEY on the
--       same column (not our case; kept here as a constraint on future
--       schema changes).
--
-- CANONICAL JSON DISCIPLINE (payload_json, node_chain_heads):
--   Every JSONB value that participates in a hash MUST be serialized
--   canonically before hashing: keys sorted ascending, no insignificant
--   whitespace, UTF-8. entry_hash and merkle_root are computed over the
--   canonical serialization, never over whatever the driver happened to
--   send. Same discipline as VIGÍA's canonical_json.
--
-- EMBEDDING MODEL — VECTOR(384) is bound to ONE model:
--   Phase 1 pins a single embedding model (documented as a constant in
--   the application config and in KNOWN_LIMITATIONS.md). Mixing vectors
--   from different models in the same column is semantically meaningless
--   even when dimensions coincide. Changing models is a migration event,
--   not a config flip.
-- =============================================================================

CREATE DATABASE IF NOT EXISTS stigmergy;
USE stigmergy;

-- -----------------------------------------------------------------------------
-- memory_regions — logical partitions of the shared memory field.
-- Physical CockroachDB nodes are a distinct concept (see `locality`),
-- never conflated with region identity.
-- -----------------------------------------------------------------------------
CREATE TABLE memory_regions (
    region_id             STRING PRIMARY KEY,
    locality               STRING NOT NULL,       -- maps to actual CockroachDB region/zone
    centroid                VECTOR(384),
    generation              INT8 NOT NULL DEFAULT 1 CHECK (generation >= 1),
    parent_region           STRING REFERENCES memory_regions(region_id),
    status                  STRING NOT NULL DEFAULT 'ACTIVE'
                                 CHECK (status IN ('ACTIVE','SPLITTING','MERGING','RETIRED')),
    variance                DECIMAL(11,10),
    entropy                 DECIMAL(11,10),
    bridge_score             DECIMAL(11,10),
    last_signature_update    TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- agent_nodes / node_region_capabilities — authority for distributed writers.
--
-- An audit ``node_id`` is not an identity credential.  Every mutable operation
-- must bind it to CockroachDB's authenticated ``current_user`` and, when it
-- acts for a region, to one explicit live capability.  This stops a valid
-- ledger from faithfully recording an unauthorized claim over another region.
--
-- Bootstrap authority_administrators out of band with CockroachDB RBAC.  In
-- production, give each agent or Lambda role a distinct least-privilege DB
-- principal; never share one application principal across independently
-- trusted nodes.
-- -----------------------------------------------------------------------------
CREATE TABLE authority_administrators (
    db_principal          STRING PRIMARY KEY,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agent_nodes (
    node_id               STRING PRIMARY KEY,
    db_principal          STRING NOT NULL UNIQUE,
    status                STRING NOT NULL DEFAULT 'ACTIVE'
                              CHECK (status IN ('ACTIVE', 'REVOKED')),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at            TIMESTAMPTZ,
    revoke_reason         STRING,
    CHECK (
        (status = 'ACTIVE'  AND revoked_at IS NULL AND revoke_reason IS NULL)
     OR (status = 'REVOKED' AND revoked_at IS NOT NULL AND revoke_reason IS NOT NULL)
    )
);

CREATE TABLE node_region_capabilities (
    node_id               STRING NOT NULL REFERENCES agent_nodes(node_id),
    region_id             STRING NOT NULL REFERENCES memory_regions(region_id),
    capability            STRING NOT NULL CHECK (capability IN (
                              'STORE', 'REINFORCE', 'SIGNAL', 'RESOLVE',
                              'OBSERVE', 'REGION_ADMIN'
                          )),
    status                STRING NOT NULL DEFAULT 'ACTIVE'
                              CHECK (status IN ('ACTIVE', 'REVOKED')),
    granted_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at            TIMESTAMPTZ,
    revoke_reason         STRING,
    PRIMARY KEY (node_id, region_id, capability),
    CHECK (
        (status = 'ACTIVE'  AND revoked_at IS NULL AND revoke_reason IS NULL)
     OR (status = 'REVOKED' AND revoked_at IS NOT NULL AND revoke_reason IS NOT NULL)
    )
);

-- -----------------------------------------------------------------------------
-- memories — the episodic memory store.
-- last_accessed_at / last_reinforced_at / last_migrated_at are written
-- explicitly by the application inside the same transaction as their
-- corresponding audit event (Invariant 3). They deliberately do NOT use
-- ON UPDATE: each one must change only when its specific semantic event
-- occurs, not whenever any column of the row is touched.
-- -----------------------------------------------------------------------------
CREATE TABLE memories (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    region_id             STRING NOT NULL REFERENCES memory_regions(region_id),
    embedding             VECTOR(384) NOT NULL,
    content               STRING NOT NULL,
    state                 STRING NOT NULL DEFAULT 'NEUTRAL'
                               CHECK (state IN ('REINFORCED','NEUTRAL','FORGOTTEN')),
    tier                  STRING NOT NULL DEFAULT 'SHORT_TERM'
                               CHECK (tier IN ('SHORT_TERM','REGIONAL','GLOBAL','ORPHANED')),
    confidence             DECIMAL(11,10) NOT NULL DEFAULT 0.5
                               CHECK (confidence >= 0 AND confidence <= 1),
    recall_count           INT8 NOT NULL DEFAULT 0 CHECK (recall_count >= 0),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_accessed_at        TIMESTAMPTZ,
    last_reinforced_at       TIMESTAMPTZ,
    last_migrated_at         TIMESTAMPTZ,
    VECTOR INDEX (region_id, embedding)
);

-- -----------------------------------------------------------------------------
-- cell_links — CHECK (memory_id_a < memory_id_b) canonicalizes pair order,
-- eliminating inverted duplicates and self-links in one constraint.
--
-- Domain decision: a pair of memories holds exactly ONE relationship at a
-- time. RESONANT and INHIBITORY cannot coexist for the same pair — that
-- would encode two memories simultaneously reinforcing and contradicting
-- each other, which is not a coherent state. A pair's link_type can
-- TRANSITION over time (new evidence can turn resonance into conflict),
-- but that is an UPDATE on the existing row, never a second INSERT.
-- UNIQUE is therefore on the pair alone, not on (pair, link_type).
-- -----------------------------------------------------------------------------
CREATE TABLE cell_links (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id_a          UUID NOT NULL REFERENCES memories(id),
    memory_id_b          UUID NOT NULL REFERENCES memories(id),
    link_type            STRING NOT NULL CHECK (link_type IN ('RESONANT','INHIBITORY')),
    resonance_weight      DECIMAL(11,10) NOT NULL DEFAULT 0.3
                               CHECK (resonance_weight >= 0 AND resonance_weight <= 1),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now() ON UPDATE now(),
    CHECK (memory_id_a < memory_id_b),
    UNIQUE (memory_id_a, memory_id_b),
    INDEX (memory_id_b)
);

-- -----------------------------------------------------------------------------
-- recruitment_signals — write-only stigmergic state, never a message bus.
-- NEVER attach a row-level TTL to this table — see Invariant 8 note above.
--
-- DECAY IS COMPUTED, NEVER STORED. There is no trigger, no cron rewrite,
-- no background job mutating signal_strength. Effective strength is
-- derived at READ time as:
--     signal_strength * exp(-decay_rate * seconds_since(created_at))
-- Because created_at is immutable and the formula is pure, two agents
-- reading at different instants see different effective strengths by
-- design (pheromones evaporate) — and there is no write, hence no race,
-- no accumulation, no drift. The stored columns are initial conditions,
-- not running state. (Raised independently by two external audits; if a
-- third reader asks, the comment has failed and should be promoted to
-- ARCHITECTURE.md.)
-- -----------------------------------------------------------------------------
CREATE TABLE recruitment_signals (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    origin_region          STRING NOT NULL REFERENCES memory_regions(region_id),
    memory_id              UUID NOT NULL REFERENCES memories(id),
    reason                 STRING NOT NULL
                               CHECK (reason IN ('HIGH_RESONANCE','NOVEL_PATTERN','REINFORCEMENT','CONFLICT')),
    vigor                  DECIMAL(11,10) NOT NULL
                               CHECK (vigor >= 0 AND vigor <= 1),
    signal_strength         DECIMAL(11,10) NOT NULL DEFAULT 1.0
                               CHECK (signal_strength >= 0 AND signal_strength <= 1),
    decay_rate              DECIMAL(11,10) NOT NULL DEFAULT 0.15
                               CHECK (decay_rate > 0),
    status                  STRING NOT NULL DEFAULT 'PENDING'
                               CHECK (status IN ('PENDING','ACCEPTED','REJECTED','EXPIRED')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at               TIMESTAMPTZ NOT NULL
                               CHECK (expires_at > created_at),
    resolved_at              TIMESTAMPTZ,
    resolved_by_region        STRING REFERENCES memory_regions(region_id),
    -- State coherence: a signal's resolution fields must match its status.
    -- EXPIRED deliberately gets resolved_at (the sweep's timestamp, for
    -- audit) but never resolved_by_region — nobody accepted it, it decayed.
    CHECK (
        (status = 'PENDING'  AND resolved_at IS NULL     AND resolved_by_region IS NULL)
     OR (status = 'EXPIRED'  AND resolved_at IS NOT NULL AND resolved_by_region IS NULL)
     OR (status IN ('ACCEPTED','REJECTED')
                              AND resolved_at IS NOT NULL AND resolved_by_region IS NOT NULL)
    ),
    -- ONE LIVE VOTE PER REGION (anti-flood). Consensus counts each live
    -- signal as a voter (quorum * count of live signals), so "half the
    -- live field points HERE" must mean half the REGIONS, not half the
    -- ROWS — otherwise a single region could fabricate consensus by
    -- emitting many signals for one memory. This PARTIAL unique index
    -- caps a region at one PENDING signal per memory; it is deliberately
    -- partial on status='PENDING' so history is untouched (Invariant 8:
    -- EXPIRED/ACCEPTED rows never collide, and a region may re-recruit a
    -- memory once its previous signal has resolved or evaporated).
    -- NOTE: this hardens self-flooding only. Binding origin_region to the
    -- emitting node's identity — so an actor cannot emit under a region it
    -- does not own — is deferred to the identity phase (KNOWN_LIMITATIONS).
    UNIQUE INDEX one_live_signal_per_region (memory_id, origin_region)
        WHERE status = 'PENDING',
    INDEX (status, expires_at),
    INDEX (memory_id)
);

-- -----------------------------------------------------------------------------
-- agent_search_state — roaming/dwelling controller with hysteresis.
-- -----------------------------------------------------------------------------
CREATE TABLE agent_search_state (
    agent_id            STRING PRIMARY KEY,
    mode                 STRING NOT NULL DEFAULT 'ROAMING' CHECK (mode IN ('ROAMING','DWELLING')),
    current_region        STRING REFERENCES memory_regions(region_id),
    confidence_ema         DECIMAL(11,10) NOT NULL DEFAULT 0
                               CHECK (confidence_ema >= 0 AND confidence_ema <= 1),
    ema_prev               DECIMAL(11,10) NOT NULL DEFAULT 0
                               CHECK (ema_prev >= 0 AND ema_prev <= 1),
    stagnation_count        INT8 NOT NULL DEFAULT 0 CHECK (stagnation_count >= 0),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now() ON UPDATE now()
);

-- -----------------------------------------------------------------------------
-- audit_chain — per-node hash chain. reason is NOT NULL: an audit event
-- without a stated reason cannot exist. UNIQUE(node_id, prev_hash) makes
-- an intra-node fork impossible at the schema level, not just detectable
-- after the fact.
--
-- payload_json MUST be canonical JSON (keys sorted ascending, no
-- insignificant whitespace) — entry_hash is computed over the canonical
-- serialization. A payload that hashes differently on two nodes because
-- of key ordering is a forensic integrity failure, not a formatting
-- nitpick.
--
-- INDEX (node_id, event_type) supports forensic reconstruction queries
-- ("all MIGRATION events on node X") without a full chain scan.
-- -----------------------------------------------------------------------------
CREATE TABLE audit_chain (
    node_id             STRING NOT NULL,
    seq                  INT8 NOT NULL CHECK (seq >= 0),
    prev_hash             STRING NOT NULL,
    entry_hash             STRING NOT NULL,
    event_type            STRING NOT NULL,
    reason                STRING NOT NULL,
    payload_json           JSONB NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id, seq),
    UNIQUE (node_id, prev_hash),
    INDEX (node_id, event_type)
);

-- -----------------------------------------------------------------------------
-- merkle_snapshots — chained ledger of ledgers.
-- UNIQUE(parent_snapshot) forces linearity for non-genesis snapshots.
-- UNIQUE(snapshot_seq) additionally closes the genesis gap: CockroachDB
-- treats every NULL as distinct under UNIQUE, so two genesis rows with
-- parent_snapshot = NULL would not collide on that constraint alone.
-- The application MUST assign seq = 0 deterministically to genesis;
-- CHECK (snapshot_seq >= 0) makes negative sequences impossible.
--
-- node_chain_heads MUST be canonical JSON with node_id keys sorted
-- ascending before hashing — merkle_root and ledger_hash are computed
-- over that canonical form. Same discipline as payload_json above.
-- -----------------------------------------------------------------------------
CREATE TABLE merkle_snapshots (
    snapshot_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_snapshot        UUID REFERENCES merkle_snapshots(snapshot_id),
    snapshot_seq           INT8 NOT NULL CHECK (snapshot_seq >= 0),
    node_chain_heads        JSONB NOT NULL,
    merkle_root             STRING NOT NULL,
    ledger_hash             STRING NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (parent_snapshot),
    UNIQUE (snapshot_seq)
);
