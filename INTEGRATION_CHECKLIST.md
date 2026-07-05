# STIGMERGY — Integration test checklist (against the real cluster)

The project's test discipline: pure decision functions get adversarial
unit tests with no database; transactional paths get integration tests
against the real 3-node cluster. This file is the ledger of the second
half — behaviors flagged in audits that CANNOT be honestly tested
without CockroachDB, so they do not silently fall through the gap.

Each item states what must be pinned, and which audit finding demands it.

## ops.recruitment

- [ ] **Cooldown guard rejects a second migration (REC-015, Invariant 6).**
      Migrate a memory, immediately resolve a second consensus toward a
      third region: expect CooldownActive, memory unmoved, signals still
      PENDING, no MEMORY_MIGRATED event sealed. Then advance past the
      window (short cooldown in test) and confirm the second migration
      succeeds with from_region = the first target.
- [ ] **Concurrent resolutions cannot double-migrate.** Two transactions
      resolving the same memory toward different targets: FOR UPDATE on
      the memory row serializes them; the loser must see either
      CooldownActive or an empty live set (signals consumed), never a
      second migration inside the window.
- [ ] **Decay expression executes on CockroachDB (REC-001).** The
      FLOAT8-cast expression must run without "unsupported binary
      operator"; a signal near its floor must flip from live to dead as
      wall-clock time passes, with zero writes to signal_strength.
- [ ] **Sweep chunking (REC-016, REC-012).** Insert limit+k expired
      signals; one call expires exactly `limit` (oldest expires_at
      first), seals one event with `limit` ids; looping drains to 0.
      Verify EXPIRED rows carry resolved_at but NULL resolved_by_region
      (schema state-coherence CHECK).
- [ ] **Nonexistent memory → LookupError, not CooldownActive (REC-004).**
- [ ] **Nonexistent target region → LookupError before any write;
      RETIRED region → RegionUnavailable (REC-011).** Confirm nothing
      changed and no event sealed in both cases.
- [ ] **Same-region target → ValueError, cooldown NOT burned (REC-005).**
- [ ] **emit_signal FK translation (REC-013).** Nonexistent memory_id
      and nonexistent origin_region both surface as LookupError naming
      the ids, transaction cleanly aborted.
- [ ] **MEMORY_MIGRATED payload forensics (REC-003).** After A→B,
      the sealed payload carries from_region=A, target_region=B,
      old_tier, new_tier=REGIONAL, and only target-origin signal ids in
      accepted_signal_ids; non-target live signals remain PENDING.

## ops.orphans

- [ ] **Staleness expression on CockroachDB.** greatest() + coalesce()
      over the four activity timestamps runs and orders correctly;
      a memory with NULL in every nullable timestamp orphans based on
      created_at alone.
- [ ] **Activity of any kind protects.** Reinforce (not recall) a memory,
      sweep with a window that would otherwise catch it: not orphaned.
      Same for a fresh migration (last_migrated_at).
- [ ] **Rediscovery self-protects.** Orphan a memory, recall it
      (REDISCOVERED, tier -> SHORT_TERM), sweep immediately: not
      re-orphaned — last_accessed_at is fresh.
- [ ] **GLOBAL and REINFORCED are untouchable under defaults.** Stale
      GLOBAL memory and stale REINFORCED memory both survive the sweep;
      the REINFORCED one orphans only when eligible_states says so
      explicitly.
- [ ] **Chunking drains stalest-first.** limit+k stale memories: one call
      orphans exactly `limit` (oldest last_activity first), one
      MEMORIES_ORPHANED event with `limit` (memory_id, from_tier) pairs;
      looping drains to empty; from_tier values are the pre-sweep tiers.
- [ ] **Sweep/recall race is serializable, not corrupting.** Concurrent
      sweep and recall on the same memory: one of the transactions
      retries (40001) or blocks on the lock; the surviving order is
      either recall-then-protected or orphan-then-REDISCOVERED-later —
      never a lost update or an unaudited transition.
- [ ] **state axis untouched.** A FORGOTTEN memory that orphans is
      ORPHANED + FORGOTTEN after the sweep — tier moved, state did not.

## ops.controller

- [ ] **First observation creates the row.** observe() on an unknown
      agent_id inserts (ROAMING, NULL region, ema from INITIAL_STATE);
      no audit event unless the very first observation already crosses
      enter (possible with high beta — verify the event seals if so).
- [ ] **Concurrent first-observations.** Two transactions observing the
      same new agent_id: one commits, the other surfaces as 40001 or
      StateWriteConflict — never two rows, never a lost observation.
- [ ] **Transition audit policy.** A long run of in-band observations
      writes MANY row updates and ZERO audit events; the eventual
      ROAMING->DWELLING crossing seals exactly one AGENT_MODE_CHANGED
      with trigger, region, quantized ema/observation.
- [ ] **Entering DWELLING without candidate_region → ValueError, no
      write, no event.** Nonexistent candidate_region → LookupError
      (FK 23503 translated), transaction cleanly aborted.
- [ ] **updated_at is owned by ON UPDATE now().** After an observe()
      UPDATE, updated_at moved without the statement naming it; confirm
      no code path ever writes it directly (schema header caveat 1).
- [ ] **Decimal round trip is exact.** ema written at scale 10, read
      back as Decimal, Fraction(Decimal) reproduces the quantized value
      bit for bit across repeated observe() calls.
- [ ] **Exit path clears the region.** DWELLING->ROAMING (both via exit
      threshold and via stagnation) sets current_region NULL and the
      payload names the trigger that fired.

## lambdas (against the cluster + a real webhook sink)

- [ ] **Changefeed round trip.** CREATE CHANGEFEED on recruitment_signals
      into the resolver's function URL; emit_signal from an agent; the
      handler receives the insert, attempts resolution toward the
      signal's origin, and the summary classifies the outcome.
- [ ] **Redelivery is a no-op.** Re-POST a batch whose resolution already
      migrated the memory: attempts run, live set is empty, summary says
      not_reached, nothing changed, no duplicate MEMORY_MIGRATED.
- [ ] **Echo suppression.** The handler's own ACCEPTED updates flow back
      through the feed and are ignored (non-PENDING), not re-attempted —
      no feedback loop.
- [ ] **Poison message does not stall the feed.** A malformed entry ACKs
      200 with the malformation named in the body; subsequent batches
      keep flowing.
- [ ] **Unexpected failure redelivers.** Kill the DB mid-batch: 500,
      sink retries, attempts succeed on the retry, no double effects.
- [ ] **Cron budget.** Seed a backlog > MAX_CHUNKS * CHUNK_SIZE expired
      signals: one invocation reports drained=false with exact totals;
      repeated invocations converge to drained=true.
- [ ] **Cold start fails fast.** Deploy without STIGMERGY_NODE_ID: the
      first invocation errors with our message, before touching the DB.
- [ ] **Warm connection reuse.** Consecutive invocations reuse the
      module-global connection; a dropped connection re-establishes
      instead of erroring the invocation.

## ops.memories (carried over)

- [ ] Rediscovery: recall touching N ORPHANED memories seals N
      REDISCOVERED events in the same transaction.
- [ ] verify_provider refuses a mixed-provider database.

## audit / merkle (carried over)

- [ ] append_event race on one node_id: concurrent writers → one wins,
      one retries via 40001 or ChainWriteConflict; UNIQUE(node_id,
      prev_hash) never violated.
- [ ] create_snapshot race: loser surfaces as constraint error on
      UNIQUE(snapshot_seq), never a forked ledger.
- [ ] verify_chain and verify_ledger pass end-to-end after a realistic
      mixed workload (store/recall/reinforce/emit/resolve/sweep).
