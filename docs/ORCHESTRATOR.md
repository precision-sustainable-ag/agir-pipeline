# AgIR Orchestrator Design Document

## 1. Overview

The AgIR Orchestrator is the control-plane component responsible for coordinating
batch-based image processing stages in the AgIR pipeline. It determines *what*
work should run, *when* it should run, and *where* it should run, while ensuring
that execution is exclusive, auditable, resumable, and scalable across multiple
compute environments.

The orchestrator does **not** perform image processing, data transfer, or direct
database mutation. Instead, it interacts exclusively with the agir-db API to
claim work, launch stages, and submit run results for ingestion.

---

## 2. Design Goals

- Modular and minimal control-plane logic
- Database-backed exclusivity and resumability
- Clear separation between orchestration, execution, and ingestion
- Support for multiple execution backends (local, Slurm)
- Fully auditable execution history
- Crash-safe and retryable by design

---

## 3. Non-Goals

The orchestrator explicitly does **not**:

- Transfer data between storage systems
- Perform inventory discovery or checksum validation
- Query or update pipeline state tables directly
- Perform image processing or model inference
- Decide dependency versions or configurations
- Provide user-facing interfaces or dashboards

---

## 4. High-Level Architecture

```text
┌──────────────────────────┐
│       Orchestrator       │
│   (Python control plane) │
└──────────┬───────────────┘
           │
           ▼
┌──────────────────────────┐
│        agir-db API       │
│ (readiness, leases,      │
│  ingestion, provenance)  │
└──────────┬───────────────┘
           │
           ▼
┌──────────────────────────┐
│        Stage CLIs        │
│ (raw_to_jpg, det, seg)   │
└──────────────────────────┘
```

The orchestrator communicates only with agir-db and stage executables.
All authoritative pipeline state is maintained by agir-db.

---

## 5. Unit of Work

The primary unit of work is a **stage execution for a batch**:

(batch_id, stage)

Optionally, this may be extended in the future to:

(batch_id, stage, shard_id)

Sharding logic is resolved by agir-db and expressed via the run context.

---

## 6. Orchestrator Responsibilities

### 6.1 Core Responsibilities

- Query agir-db for eligible work (readiness/backlog)
- Claim exclusive leases for work items
- Launch stage executables with resolved run contexts
- Monitor execution and capture runtime metadata
- Validate presence of required output artifacts
- Submit run bundles for ingestion
- Apply retry and backoff policies
- Emit structured logs and metrics

### 6.2 Explicit Limitations

The orchestrator must not:

- Perform file transfers
- Validate scientific correctness of outputs
- Inspect raw file inventories
- Mutate pipeline state tables directly

---

## 7. Module Structure

The orchestrator is organized into the following modules:

```text
orchestrator/
├── core.py # Main control loop
├── polling.py # Readiness/backlog interface
├── leasing.py # Lease/claim management
├── submission.py # Stage launch backends
├── ingestion.py # Run bundle validation + submit
├── retry.py # Retry and backoff policy
└── init.py # Public Orchestrator export
```

---

## 8. Module Responsibilities

### 8.1 `core.py`

- Owns the main orchestration loop
- Wires together polling, leasing, execution, ingestion, and retry
- Manages worker lifecycle and shutdown handling
- Emits structured logs with run context identifiers

Public API:
- `class Orchestrator`
- `Orchestrator.run_forever()`

---

### 8.2 `polling.py`

- Queries agir-db for eligible work items
- Applies stage filters, priorities, and limits
- Returns *candidates*, not claimed work

Interface:
- `get_candidates(stages, limit) -> List[WorkCandidate]`

---

### 8.3 `leasing.py`

- Performs atomic lease claims
- Manages lease heartbeats and release
- Guarantees exclusive execution per work item

Interface:
- `claim(candidate, owner_id, ttl) -> Optional[LeasedWork]`
- `heartbeat(lease_id)`
- `release(lease_id)`

---

### 8.4 `submission.py`

- Launches stage executables
- Supports multiple execution backends
  - Local (subprocess)
  - Slurm (future)
- Captures exit code, runtime, and execution metadata

Interface:
- `launch(leased_work) -> ExecutionResult`

---

### 8.5 `ingestion.py`

- Validates presence and structure of:
  - `run_report.json`
  - `manifest.json`
- Packages run bundle for ingestion
- Submits results to agir-db

Interface:
- `validate(leased_work) -> ValidatedBundle`
- `submit(leased_work, execution_result, bundle)`

---

### 8.6 `retry.py`

- Encapsulates retry and backoff policy
- Maps exit codes and failures to actions
- Does not perform retries itself

Interface:
- `decide(leased_work, execution_result|exception) -> RetryDecision`
- `backoff_seconds(attempt)`

---

## 9. Run Context Contract

Each stage is launched with a resolved `run_context.json` containing:

- Identifiers: `lease_id`, `run_id`, `batch_id`, `stage`, `attempt`
- Input paths or URIs
- Output directories and report paths
- Dependency identifiers and checksums
- Stage parameters
- Suggested execution resources

Stages must:
- Read only from the run context
- Write outputs only to specified locations
- Produce `run_report.json` and `manifest.json`
- Exit with standardized exit codes

---

## 10. Execution Flow

For each work item:

1. Poll agir-db for candidates
2. Atomically claim a lease
3. Launch stage with run context
4. Wait for completion
5. Validate run outputs
6. Submit run bundle for ingestion
7. Release lease or apply retry policy

All pipeline state transitions occur inside agir-db during ingestion.

---

## 11. Retry and Failure Handling

Exit codes are categorized to guide retry decisions:

- `0`: Success
- `10–19`: Input/data errors (no retry)
- `20–29`: Dependency/config errors (no retry)
- `30–39`: Transient infra errors (retry)
- `40–49`: Resource errors (retry with adjustments)
- `50+`: Unknown errors (limited retries)

Retry policy is configurable and enforced centrally.

### Canonical Exit Codes (Required)

- **0 — `EXIT_SUCCESS`**  
  All images were processed successfully.

- **1 — `EXIT_PARTIAL`**  
  Some images failed while others succeeded.  
  Partial failures must be recorded in `manifest.json`.

- **2 — `EXIT_FAILURE`**  
  All images failed during stage execution.

- **3 — `EXIT_CONFIG_ERROR`**  
  Stage setup or configuration error (invalid args, missing config, unresolved dependencies).

---

## 12. Concurrency and Safety

- Multiple orchestrators may run concurrently
- Leases ensure exclusive execution
- Lease TTL and heartbeats protect against crashes
- Idempotent stages ensure safe re-execution

---

## 13. Observability and Auditability

- Structured logs include: `batch_id`, `stage`, `lease_id`, `run_id`, `attempt`
- Metrics may include backlog size, throughput, failure rates, runtimes
- Full execution history is preserved in agir-db

---

## 14. Setup and Deployment

### 14.1 Prerequisites

- agir-db API deployed and reachable
- Postgres cluster configured
- Stage CLIs available on execution nodes
- Scratch and output storage mounted
- Slurm configured (if using Slurm backend)

### 14.2 Startup

- Configure orchestrator via versioned config
- Assign unique `owner_id` per orchestrator instance
- Start orchestrator as a long-running service or batch job

---

## 15. MVP Completion Criteria

The orchestrator is considered functional when:

- Work is claimed exclusively and executed once
- Crashes do not cause permanent job loss
- Failed stages are retried or quarantined correctly
- All runs are auditable via agir-db
- No pipeline state is inferred from filesystem state

---

## 16. Future Extensions

- Sharded work units
- Dynamic resource selection
- Multiple execution backends
- Priority-based scheduling
- Workflow engine integration (Airflow, Nextflow)

---

## 17. Summary

The AgIR Orchestrator is intentionally minimal: a reliable, auditable, and
stateless control-plane process. By delegating authority to agir-db and enforcing
strict stage contracts, it enables scalable and maintainable pipeline execution
across environments without entangling execution logic, state, or data movement.
