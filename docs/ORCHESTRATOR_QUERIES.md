# Orchestrator Query Questions

Before we create tables or write orchestrator code, I want to try to define a some questions the orchestrator is going to ask via `agir_db`.

This is not a SQL design doc. You can think of it as a query contracts where we think about the specific question, inputs, outputs, and any rules that need to be met. We'll use this contract and some pseudo-query code to lay a blueprint for the future tables/views.

---

## Instructions

You are writing query "contracts" by thinking of specific questions the orchestrator will ask:   
**Question → Inputs → Returns → Rules → 5–10 line pseudo-query**

1. One screen per query (≈ 20–25 lines max). If it’s longer, it’s too detailed
2. Returns: 5–8 fields max. If you need more, you’re probably mixing responsibilities.
3. No join logic. Put complexity behind a placeholder view or API call (e.g., report.ready_work).
4. Rules > SQL. If it’s complicated, describe it as a rule bullet.
5. Use placeholder names freely. Tables/views don’t have to exist yet.

These may reference placeholder surfaces like:

* `report.ready_work`
* `report.runs_needing_transfer`
* `ops.stage_leases`
* `agir_db.*` API calls

Those do not need to exist yet. But we'll know we need to implement them in the future.

---

## Template (Copy for Every Query)

````md
## Q#: <Short name>

**Question**  
(One sentence)

**Used by**  
<Orchestrator loop / leasing / transfer scheduling / etc.>

**Inputs**
- ...

**Returns**
- ...

**Rules**
- ...

**Pseudo-query**
```sql
<5–10 lines; may reference views or API calls that don’t exist yet>
```

**Open questions (if any)**
- ...

````

---

## Worked Example (Q1 done correctly)

## Q1: Get ready work

**Question**  
Which `(batch_id, stage)` work units are eligible to run now?

**Used by**  
Orchestrator polling loop

**Inputs**
- `limit` (int)
- `stages` (optional list)
- `min_priority` (optional int)

**Returns**
- `batch_id`
- `stage`
- `priority`
- `resource_profile_id` *(or cpus/mem/time)*
- `config_id`

**Rules**
- Stage enabled
- Required inputs exist
- Dependencies satisfied **and transferred**
- Not already completed successfully
- Not currently leased

**Pseudo-query**
```sql
SELECT batch_id, stage, priority, resource_profile_id, config_id
FROM report.ready_work
WHERE (:stages IS NULL OR stage = ANY(:stages))
ORDER BY priority DESC, batch_date ASC
LIMIT :limit;
```

---

<br>
<br>

# Queries You Must Fill In (Q2–Q6)
These are a minimal first pass, not necessarily the final finished set. If you think of more or disagree with what I have, change and justify. 

## Q2: Claim lease (atomic)

**Question**  
Can this orchestrator atomically claim exclusive execution rights for a specific `(batch_id, stage)`?

**Used by**  
Orchestrator polling loop (immediately after Q1 candidate selection)

**Inputs**
- `batch_id` (text)
- `stage` (text)
- `orchestrator_id` (text)
- `ttl_seconds` (int)
- `attempt` (optional int override)

**Returns**
- `claimed` (bool)
- `lease_id`
- `batch_id`
- `stage`
- `expires_at`
- `attempt`

**Rules**
- Claim must be atomic (single call checks + writes)
- At most one active lease per `(batch_id, stage)`
- Existing non-expired lease blocks claim
- Expired lease may be reclaimed
- `attempt` increments on successful claim/reclaim
- If claim fails, return `claimed=false` and no lease side effects

**Pseudo-query**

```sql
SELECT claimed, lease_id, batch_id, stage, expires_at, attempt
FROM agir_db.claim_stage_lease(
  batch_id := :batch_id,
  stage := :stage,
  orchestrator_id := :orchestrator_id,
  ttl_seconds := :ttl_seconds,
  attempt := :attempt
);
```

**Open questions (if any)**
- Should `attempt` always be managed server-side (recommended)?

---

## Q3: Release lease

**Question**  
How does the orchestrator mark a lease as no longer active when work completes, fails, or is abandoned?

**Used by**  
Post-submission cleanup path and failure handling path

**Inputs**
- `lease_id`
- `orchestrator_id`
- `release_reason` (e.g., `completed`, `submission_failed`, `abandoned`)
- `released_at` (optional; defaults server time)

**Returns**
- `released` (bool)
- `lease_id`
- `released_at`
- `release_reason`

**Rules**
- Only owning `orchestrator_id` can release an active lease
- Release is idempotent (repeat call does not error)
- Releasing an already expired lease is allowed for audit closure
- `release_reason` is required and stored

**Pseudo-query**

```sql
SELECT released, lease_id, released_at, release_reason
FROM agir_db.release_stage_lease(
  lease_id := :lease_id,
  orchestrator_id := :orchestrator_id,
  release_reason := :release_reason,
  released_at := :released_at
);
```

**Open questions (if any)**
- Should non-owner release be hard-fail or soft no-op?

---

## Q4: Find stale leases

**Question**  
Which active leases are stale (expired) and should be reclaimed, retried, or marked abandoned?

**Used by**  
Periodic orchestrator maintenance/cleanup step

**Inputs**
- `as_of_ts` (timestamp; optional, defaults now)
- `limit` (int)

**Returns**
- `lease_id`
- `batch_id`
- `stage`
- `orchestrator_id`
- `expires_at`
- `attempt`

**Rules**
- Stale lease = `state='active'` and `expires_at < as_of_ts`
- Return oldest expirations first
- Read-only discovery query; mutation happens in separate claim/release calls
- Must be safe when multiple orchestrators run cleanup concurrently

**Pseudo-query**

```sql
SELECT lease_id, batch_id, stage, orchestrator_id, expires_at, attempt
FROM report.stale_stage_leases
WHERE expires_at < COALESCE(:as_of_ts, NOW())
ORDER BY expires_at ASC
LIMIT :limit;
```

**Open questions (if any)**
- Should stale detection require a grace window beyond `expires_at`?

---

## Q5: Get runs needing transfer

**Question**  
Which successful stage runs now require artifact transfer job submission?

**Used by**  
Transfer scheduling loop in orchestrator

**Inputs**
- `limit` (int)
- `stages` (optional list)
- `min_priority` (optional int)

**Returns**
- `run_id`
- `batch_id`
- `stage`
- `transfer_priority`
- `transfer_profile_id`
- `artifact_ref`

**Rules**
- Stage run status must be transfer-eligible (e.g., success/partial per policy)
- Transfer must not already be active/completed for this `run_id` + destination
- Destination/promotion policy must be satisfied
- Ordering should prioritize urgent transfers first

**Pseudo-query**

```sql
SELECT run_id, batch_id, stage, transfer_priority, transfer_profile_id, artifact_ref
FROM report.runs_needing_transfer
WHERE (:stages IS NULL OR stage = ANY(:stages))
  AND (:min_priority IS NULL OR transfer_priority >= :min_priority)
ORDER BY transfer_priority DESC, completed_at ASC
LIMIT :limit;
```

**Open questions (if any)**
- Should `EXIT_PARTIAL` (code 1) be transfer-eligible by default?

---

## Q6: Submit stage job (where/how to run)

**Question**  
How does the orchestrator persist an auditable record of stage job submission (backend + resources + run context reference)?

**Used by**  
Stage launch path (after successful lease claim, before or immediately after scheduler submit)

**Inputs**
- `lease_id`
- `run_id`
- `backend` (`local` or `slurm`)
- `resource_profile_id` *(or explicit cpus/mem/time fields)*
- `command_ref` *(or rendered command hash)*
- `submitted_at` (optional; defaults server time)

**Returns**
- `accepted` (bool)
- `submission_id`
- `run_id`
- `lease_id`
- `backend`
- `submitted_at`

**Rules**
- Logging call is audit-focused; it does not execute `sbatch`/`subprocess`
- One logical active submission per `lease_id` + `run_id`
- Duplicate retries must be idempotent (same `submission_id` or deterministic no-op)
- Should capture enough metadata to reconstruct launch decisions later

**Pseudo-query**

```sql
SELECT accepted, submission_id, run_id, lease_id, backend, submitted_at
FROM agir_db.record_stage_submission(
  lease_id := :lease_id,
  run_id := :run_id,
  backend := :backend,
  resource_profile_id := :resource_profile_id,
  command_ref := :command_ref,
  submitted_at := :submitted_at
);
```

*(Note: actual sbatch happens in orchestrator code; this call is just optional logging.)*

---

## Q7: Finalize stage run

**Question**  
How does the orchestrator atomically mark a stage run as finalized (success, partial, or failure) with its exit code and end metadata?

**Used by**  
Stage completion path (after job exits, before lease release and/or transfer scheduling)

**Inputs**
- `run_id` (text)
- `lease_id` (text)
- `status` (`success` | `partial` | `failed`)
- `exit_code` (int)
- `ended_at` (optional; defaults server time)
- `metrics_ref` (optional reference/hash)
- `outputs_ref` (optional artifact reference)

**Returns**
- `accepted` (bool)
- `run_id`
- `status`
- `exit_code`
- `ended_at`
- `attempt`

**Rules**
- Only the owning active `lease_id` may finalize the run
- Finalization is atomic (single call updates run state + closes execution window)
- Idempotent: repeat calls with same inputs return same result, no duplicate side effects
- Once finalized with a terminal state, it cannot transition back to non-terminal
- `attempt` reflects the lease attempt associated with this run
- Finalization does not automatically release the lease (explicit Q3 handles lease lifecycle)

**Pseudo-query**
```sql
SELECT accepted, run_id, status, exit_code, ended_at, attempt
FROM agir_db.finalize_stage_run(
  run_id := :run_id,
  lease_id := :lease_id,
  status := :status,
  exit_code := :exit_code,
  ended_at := :ended_at,
  metrics_ref := :metrics_ref,
  outputs_ref := :outputs_ref
);
```

**Open questions (if any)**
- Should finalization implicitly release the lease (combine with Q3), or remain separate?
- Should certain exit codes (e.g., config/data errors) auto-mark run as non-retryable?

