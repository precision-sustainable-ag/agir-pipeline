
# Final System Structure

### Transfers tracked in DB:

* `LTS` -> `/90daydata` (Q8/Q9)
* `/90daydata` -> `LTS` (Q5/Q10)

### Job-local copies tracked in run metadata:

* `/90daydata` -> `/tmp`
* `/tmp` -> `/90daydata/.../stage_runs/<run_id>/`

**Notes**
- Leasing prevents concurrent execution.
- Per-run output directory prevents retry corruption.
- Orchestrator remains pure control plane.

---

# SciNet Storage Tiers

We operate across three storage tiers:

1. **LTS** — authoritative long-term storage
2. **/90daydata** — persistent compute staging
3. **job /tmp** — ephemeral scratch (per job, wiped on exit)

### Data movement paths

**Tracked in DB (transfer layer):**

* `LTS` -> `/90daydata` (input staging)
* `/90daydata` -> `LTS` (output promotion)

**Tracked as run metadata (not transfer jobs):**

* `/90daydata` -> `/tmp` (copy-in)
* `/tmp` -> `/90daydata` (copy-out to per-run dir)

---

# Lifecycle Overview

## 1. Input Staging Loop (continuous)

1. Q8 -> find batches needing input staging (`LTS` -> `/90daydata`)
2. Q9 -> record/schedule input transfer

---

## 2. Execution Loop

1. Q1 -> get ready work (only inputs already on `/90daydata`)
2. Q2 -> claim lease
3. Q6 -> record submission
4. Job wrapper (stage runs):

   * copy-in: `/90daydata` -> `/tmp`
   * stage runs
   * copy-out: `/tmp` -> `/90daydata/.../stage_runs/<run_id>/`
5. Q7 -> finalize stage run
6. Q3 -> release lease

---

## 3. Output Promotion Loop (continuous)

1. Q5 -> find runs needing promotion (`/90daydata` -> `LTS`)
2. Q10 -> record/schedule promotion transfer