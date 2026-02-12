# Minimal Implementation Roadmap

## Phase 0 — Define a vertical slice

Try to get the loop to work end-to-end for `raw_to_jpg`

**Definition of “working”:**

1. DB can answer “what’s ready?”
2. Orchestrator can claim one unit of work
3. Orchestrator launches stage
4. Stage writes `run_report.json` (+ artifacts) to TMP
5. Orchestrator ingests run_report through `agir_db`
6. DB now shows that batch/stage is no longer ready

Not working on transfers or retries yet. Use a fake place holder for copying locally.

---

## Phase 1 — DB surfaces (smallest possible)

### 1.1 Create the “control plane” lease table (ops)

**Create:** `logs.stage_leases`

* key: `(batch_id, stage)` unique for active-claim purposes
* fields: `lease_id`, `batch_id`, `stage`, `orchestrator_id`, `leased_at`, `expires_at`, `attempt`, `state`

**Done when:** you can enforce “at most one active lease per (batch_id, stage)”.

---

### 1.2 Implement agir_db API function: `claim_stage_lease()`

**Implements Q2** (atomic claim)

* if active + not expired → return `claimed=false`
* if expired or missing → claim and increment attempt

**Done when:** two orchestrators racing can’t both claim.

---

### 1.3 Implement `release_stage_lease()`

**Implements Q3** (idempotent release)

* only owner can release (recommend “soft false” not exception)

**Done when:** orchestrator can always clean up safely.

---

### 1.4 Create minimal run logging table (logs)

**Create:** `logs.stage_runs` (minimal columns only)

* `run_id` (uuid)
* `batch_id`, `stage`
* `attempt`
* `status`
* `exit_code`
* `started_at`, `ended_at`
* `run_report_ref` (path/URI to TMP bundle)
* `output_ref` (per-run directory)

**Done when:** you can record one run and later compute latest status.

---

### 1.5 Create derived readiness view (report)

**Create:** `report.ready_work` (for *one stage only*)
Rules (minimal):

* required inputs exist in `/90daydata`
* batch exists in `source.globus_file_index` for required input state
* latest run is not `success`
* no active lease

Return fields: `batch_id, stage, staging_input_ref, priority, resource_profile_id, config_id`

**Done when:** Q1 returns *some rows* and goes to *zero rows* after success.

---

## Phase 2 — Add Minimal Input Staging (Local Copy Stub)

Here we work on staging without the transfer part to reduce complexity.

---

## 2.1 Add `report.batches_needing_input_staging` (Q8)

**Rules**
* Inputs exist in LTS
* Inputs NOT present in `/90daydata`
* No active input transfer (ignore transfer table for now)

Return:

* `batch_id`
* `stage`
* `src_lts_ref`
* `dst_staging_ref`

For Phase 0, this can just check filesystem presence.

---

## 2.2 Add `request_input_transfer()` (Q9)

For Phase 0:

* Just record intent in `logs.transfer_runs`
* Immediately mark as `completed`
* Physically copy files (local cp/rsync)

No async transfer engine yet.

---

## 2.3 Input Staging Loop (Simple Version)

Loop:

1. Q8 → find missing staging
2. Q9 → copy files locally
3. Mark transfer complete

Now `/90daydata` becomes the runnable tier.

---

## Phase 3 — Orchestrator loop (minimal working daemon) 

### 3.1 Implement polling + claim loop

Pseudo-flow:

1. call `agir_db.get_ready_work(limit=…)` (Q1)
2. for each candidate: call `claim_stage_lease()` (Q2)
3. first `claimed=true` wins → proceed

**Done when:** orchestrator consistently claims only one work unit.

---

## 3.2 Introduce Job Wrapper

Wrapper does:

- Create unique run_id
- Create:
    - `/90daydata/.../stage_runs/<run_id>/`
- Copy:
    - `/90daydata` → `/tmp/<run_id>/inputs`
- Execute stage in /tmp
- Copy:
    - `/tmp/<run_id>/outputs` → `/90daydata/.../stage_runs/<run_id>/`
- Write run_report in that run dir

Stage remains unaware of tiers.
* Build the command for `raw_to_jpg` stage CLI
* Pass resolved config_id as a file path or pointer
* Stage writes outputs + `run_report.json` to TMP

**Done when:** stage runs locally from orchestrator with the same args every time.

---

### 3.3 Ingest run_report via agir_db

Add API endpoint/function:

* `ingest_run_report(run_report_path)` (MVP)

* validates schema lightly
* inserts/updates `logs.stage_runs` terminal status
* Ingests:
  * `status`
  * exit code
  * `outputs_ref` (run directory path)
* stores `run_report_ref`

**Done when:** a completed run appears in DB.

---

### 3.4 Finalize + release

After stage exits:

1. `finalize_stage_run()` (Q7)
2. `release_stage_lease()` (Q3)

**Done when:** rerunning orchestrator doesn’t re-run successful work.

---

## Phase 4 — Minimal hardening

### 4.1 TTL + stale detection

Implement Q4 as **view only**: `report.stale_stage_leases`

**Done when:** you can see expired leases and reclaim them via claim.

---

### 4.2 Idempotency guardrails (important)

* `claim_stage_lease` should be safe to retry
* `ingest_run_report` should be safe to retry (dedupe by `run_id`)
* Wrapper safe if rerun after crash

**Done when:** orchestrator can crash mid-ingest and you can rerun it.

---

>[!NOTE]
> P5 Still needs updating

# Phase 5 — Introduce Real Transfers

Now that system works locally:

---

## 5.1 Implement real `logs.transfer_runs`

Add fields:

* `direction`
* `state`
* `src_ref`
* `dst_ref`

---

## 5.2 Replace local cp in Q9 with real transfer scheduling

Now input staging loop schedules real LTS → 90daydata transfers.

---

## 5.3 Add `report.runs_needing_promotion` (Q5)

Rules:

* run success
* outputs_ref exists
* not yet promoted

---

## 5.4 Add `request_promotion_transfer()` (Q10)

Schedule `/90daydata → LTS` transfer.

---

# Week 1 Checklist

1. `logs.stage_leases` + claim/release
2. `logs.stage_runs` + ingest
3. `report.ready_work`
4. `report.batches_needing_input_staging`
5. Local staging loop (cp only)
6. Wrapper with `/tmp` + per-run output dir

That gives you:

LTS → 90daydata → /tmp → per-run-dir → DB state

Without touching real network transfers.

---