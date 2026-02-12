# Orchestrator Query Questions

Before we create tables or write orchestrator code, I want to try to define a some questions the orchestrator is going to ask via `agir_db`.

This is not a SQL design doc. You can think of it as a query contracts where we think about the specific question, inputs, outputs, and any rules that need to be met. We'll use this contract and some pseudo-query code to lay a blueprint for the future tables/views.

---

# Query Questions

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
- `staging_input_ref`  <-- path on `/90daydata`

**Rules**
- Stage enabled
- Required inputs already exist on `/90daydata`
- Dependencies satisfied **and transferred**
- Not already completed successfully
- Not currently leased
- Retry policy allows execution

**Pseudo-query**

```sql
SELECT batch_id, stage, priority, resource_profile_id, config_id, staging_input_ref
FROM report.ready_work
WHERE (:stages IS NULL OR stage = ANY(:stages))
  AND (:min_priority IS NULL OR priority >= :min_priority)
ORDER BY priority DESC, batch_date ASC
LIMIT :limit;
```

---

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
- `attempt`

**Returns**
- `claimed` (bool)
- `lease_id`
- `batch_id`
- `stage`
- `expires_at`
- `attempt`
- `job_workdir_policy`(e.g., use_tmp=true, tmp_root=/tmp/agir)

**Rules**
- Claim must be atomic (single call checks + writes)
- At most one active lease per `(batch_id, stage)`
- Existing non-expired lease blocks claim
- Expired lease may be reclaimed
- `attempt` increments on successful claim/reclaim
- If claim fails, return `claimed=false` and no lease side effects
- Inputs are already in 90daydata (not just LTS)

**Pseudo-query**

```sql
SELECT claimed, lease_id, batch_id, stage, expires_at, attempt, job_workdir_policy
FROM agir_db.claim_stage_lease(
  batch_id := :batch_id,
  stage := :stage,
  orchestrator_id := :orchestrator_id,
  ttl_seconds := :ttl_seconds,
  attempt := :attempt,
);
```

---

## Q3: Release lease

**Question**  
How does the orchestrator mark a lease as no longer active when work completes, fails, or is abandoned?

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

```sql
SELECT released, lease_id, released_at, release_reason
FROM agir_db.release_stage_lease(
  lease_id := :lease_id,
  orchestrator_id := :orchestrator_id,
  release_reason := :release_reason,
  released_at := :released_at
);
```

**Open questions/notes (if any)**
- Should non-owner release be hard-fail or soft no-op?
- Orchestrator provides the inputs for `agir_db` for writing to db

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
- `leased_by`

**Rules**
- Stale lease = `state='active'` and `expires_at < as_of_ts`
- Return oldest expirations first
- Read-only discovery query; mutation happens in separate claim/release calls via `agir_db`
- Must be safe when multiple orchestrators run cleanup concurrently

**Pseudo-query**

```sql
SELECT lease_id, batch_id, stage, orchestrator_id, expires_at, attempt, leased_by
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
Which successful stage runs need artifact promotion from `/90daydata` to LTS?

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
- `src_run_dir`
- `dst_lts_ref`

**Rules**
- Stage run status must be transfer-eligible (e.g., success/partial per policy)
- Transfer must not already be active/completed for this `run_id` + destination
- Destination/promotion policy must be satisfied
- Ordering should prioritize urgent transfers first
- Outputs exist in `/90daydata/.../stage_runs/<run_id>/`

```sql
SELECT run_id, batch_id, stage, transfer_priority, transfer_profile_id, artifact_ref, src_run_dir, dst_lts_ref
FROM report.runs_needing_promotion
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
- `batch_id`
- `stage`
- `run_id`
- `backend` (`local` or `slurm`)
- `resource_profile_id` *(or explicit cpus/mem/time fields)*
- `command_ref` *(or rendered command hash)*
- `submitted_at` (optional; defaults server time)
- `config_id`

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
  submitted_at := :submitted_at,
  config_id := config_id
);
```

*(Note: actual sbatch happens in orchestrator code; this call is just optional logging.)*

**Open questions/notes (if any)**
- submits call to stages

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
- `copied_out` (bool)

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
- ~~Should finalization implicitly release the lease (combine with Q3), or remain separate?~~
- Finalization should not release lease automatically. Let's keep this separate from Q3.
- Should certain exit codes (e.g., config/data errors) auto-mark run as non-retryable?

---

# Q8: Find batches needing input staging (LTS -> /90daydata)

**Question**
Which batches require input transfer from LTS to `/90daydata`?

**Inputs**
- `limit` (int, required)
- `stages` (optional list[text])
- `min_priority` (optional int)
- `as_of_ts` (optional timestamptz; defaults now)

**Returns**

- `batch_id`
- `stage`
- `transfer_profile_id`
- `src_lts_ref`
- `dst_staging_ref`
- `priority`

**Rules**
- Inputs exist in LTS
- Inputs do NOT exist on /90daydata
- No active transfer already in progress for (batch_id, stage)
- Retry policy allows staging
- Ordered by priority DESC, batch_date ASC

```sql
SELECT *
FROM report.batches_needing_input_staging
ORDER BY priority DESC, batch_date ASC
LIMIT :limit;
```

---

# Q9: Record input transfer request (LTS -> /90daydata)

**Question**
Can we atomically record (and dedupe) an input staging transfer request for a (batch_id, stage) from LTS to `/90daydata`?

**Used by**
Input staging loop (after selecting candidates from Q8)

**Inputs**
- `batch_id`
- `stage`
- `transfer_profile_id`
- `src_lts_ref`
- `dst_staging_ref`
- `priortity`
- `request_ts`

**Returns**
- `transfer_id`
- `accepted`
- `state`
- `requested_at`

**Rules**
- Atomic: one call does “check + create” without races.
- Idempotent: repeated calls for same effective request do not create duplicates.
- Deduping: an existing active transfer request for the same (direction='input_stage', batch_id, stage, dst_staging_ref) prevents creating a new one.
- Already completed behavior: if inputs are already staged (or transfer already marked completed), return state='already_completed' (still accepted=true).
- No execution side effects: this is just recording/scheduling. Another component can perform the actual Globus transfer.

```sql
SELECT accepted, transfer_id, state, requested_at
FROM agir_db.request_input_transfer(
  batch_id := :batch_id,
  stage := :stage,
  transfer_profile_id := :transfer_profile_id,
  src_lts_ref := :src_lts_ref,
  dst_staging_ref := :dst_staging_ref,
  requested_by := :requested_by,
  priority := :priority,
  dedupe_key := :dedupe_key,
  request_ts := :request_ts
);
```

---

# Q10: Record promotion transfer request (/90daydata -> LTS)

**Question**
Record/schedule output promotion transfer.

**Question**
Can we atomically record (and dedupe) an output promotion transfer request for a completed run_id, moving artifacts from /90daydata to LTS?

**Used by**
Output promotion loop (after selecting candidates from Q5)

**Inputs**
- `run_id` (text, required)
- `transfer_profile_id` (text, required)
- `src_run_dir` (text, required)
- `dst_lts_ref` (text, required)
- `requested_by` (text, optional)
- `priority` (int, optional)
- `request_ts` (timestamptz, optional; default now)
- `expected_manifest_ref` (text, optional)  

**Returns**
- `accepted` (bool)
- `transfer_id` (text)
- `state` (text)
- `requested_at` (timestamptz)

**Rules**
- Atomic + idempotent.
- No duplicates: only one active promotion transfer per (run_id, dst_lts_ref) (or per dedupe_key).
- If a promotion transfer is already active -> return state='already_active'.
- If promotion already completed (run outputs already in LTS per logs/processed pointers) -> return state='already_completed'.
- Promotion requests are only valid if the run is in a terminal transfer-eligible state (usually success, optionally partial by policy).
- This call records intent only; it does not perform the transfer.

```sql
SELECT *
FROM agir_db.request_promotion_transfer(
  run_id := :run_id,
  transfer_profile_id := :transfer_profile_id,
  src_run_dir := :src_run_dir,
  dst_lts_ref := :dst_lts_ref
);
```