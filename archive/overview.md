# Design Document for the AgIR Pipeline

## 1. Intro

This defines the design for AgIR BBot imagery. The goal is to replace the current SemiF-Preprocessing system with something that is more modular, simple, scalable, and auditable in a way that allows us to identify gaps and monitor the current status.

---

## 2. Problem Summary

The existing AgIR pipeline (SemiF-Preprocessing):

* Is not sufficently unified or modular
* Relies on scripts loosely coordinated by hydra
* Lacks any type of state tracking and only uses file-based logging
* Writes outputs to JSON files that are then curated into a databases later
* Is difficult to deploy across different environments (only on SUNNY now)
* Will be difficult to scale well as datasets, BBots, or projects increase

* It is difficult to determine:
    * What data exists and where
    * What processing has been attempted or failed
    * What remains to be processed
    * Which outputs are authoritative
    * Is the data consistent

---

## 3. Design Goals

Redesigned aims to:

* Be more modular
* Be **portable across environments** (HPC, SciNet, SUNNY, Lightning, on-prem)
* Establish **PostgreSQL as a keystone component** for:

  * Pipeline state tracking
  * Storage of derived data products
* Maintain a **clear separation** between:

  * Image processing logic
  * Database and orchestration logic
* Enable **auditable execution**:

  * Inputs, outputs, stages, failures
* Support **automated discovery** of:

  * New batches
  * Incomplete or failed processing
* Standardize **dependency and configuration management**
* Scale with:

  * Increasing numbers of BBots
  * New locations
  * High data quality requirements

---

## 4. Scope and Non-Goals

### In Scope

* Batch-based image processing
* Detection, segmentation, and cutout generation
* Pipeline state tracking and provenance
* Operational monitoring and readiness detection

### Out of Scope

* Model training or development
* Real-time processing
* Experimental design or phenotyping targets
* User-facing GUIs or visualization tools
* Camera configuration and calibration
* Legacy system migration
* Color matrix development

---

## 5. Architectural Principles

### Modular Pipeline Stages

Each pipeline stage is an **independent executable unit** with a clearly defined contract:

* Explicit inputs
* Explicit outputs
* Deterministic behavior
* Idempotent and resumable execution

Stages can be tested, deployed, and replaced independently.

### Clear Separation of Concerns

* Image processing stages **do not**:

  * Query the database
  * Decide what runs next
* Database APIs:

  * Track state
  * Ingest outputs
  * Enforce consistency
* Orchestration logic:

  * Determines what work runs and when

### Database as Source of Truth

The PostgreSQL database is the authoritative system for:

* File inventory
* Pipeline state
* Data products
* Provenance metadata

Lifecycle tiers follow a clear progression:

```
source (bronze) → processed (silver) → release (gold)
```

---

## 6. High-Level Architecture

The pipeline follows a **stage-based architecture** coordinated by an orchestration layer and backed by a centralized database.

(See existing Mermaid diagram for visual flow.)

**Conceptual flow:**

1. Orchestrator queries readiness/backlog from the database
2. Orchestrator launches an eligible processing stage
3. Stage:

   * Reads inputs from staging storage
   * Writes artifacts to staging storage
   * Produces execution reports
4. Database API ingests outputs and updates state
5. Database becomes the updated source of truth for downstream stages

---

## 7. Pipeline Stages

Examples of pipeline stages include:

* `raw_to_jpg`
* `jpg_to_det`
* `det_to_seg`
* `seg_to_cutouts`

### Stage Characteristics

* Standalone CLI (optionally containerized)
* Operates at batch or image level
* Produces:

  * Physical artifacts (images, masks, cutouts)
  * `run_report.json` (required)
  * `manifest.json` (optional, per-image detail)

### Stage Responsibilities

* Perform a single, narrowly scoped task
* Report execution results
* Exit with standardized exit codes

### Stage Limitations

Stages **must not**:

* Query the database
* Determine pipeline readiness
* Manage retries or scheduling

---

## 8. Database Responsibilities (`agir-db`)

The database layer provides:

* Global file inventory (via Globus indexing)
* Pipeline execution tracking
* Readiness and backlog queries
* Ingestion of derived data products
* Provenance tracking (models, configs, versions)
* Support for curated dataset releases

The database answers key operational questions:

* What data exists and where?
* What processing has been attempted?
* What succeeded or failed?
* What remains to be done?
* What constitutes an official release?

---

## 9. Orchestration Layer

### Role

The orchestration layer:

* Queries readiness/backlog tables
* Launches pipeline stages
* Handles retries and resumes using exit codes

### Implementation Strategy

* **Initial:** Slurm + Python/Bash scripts
* **Future:** Evaluate workflow engines (Airflow, Nextflow, Snakemake)

**Guiding principle:** orchestration is intentionally swappable.
Stage CLIs and database APIs remain stable regardless of orchestration choice.

---

## 10. Configuration and Dependency Management

* All dependencies (models, configs, color matrices) are:

  * Versioned
  * Stored in a centralized dependency store
* A dedicated **registry schema**:

  * Catalogs dependencies
  * Records versions, checksums, and identifiers
  * Manages selection and provenance
* Configuration files define **behavior**
* Registry defines **identity and traceability**

Dependencies are stored in **read-only, versioned directory structures** with stable URIs—never scattered across user directories.
