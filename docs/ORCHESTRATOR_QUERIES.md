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
**Used by**
**Inputs** 
**Returns**
**Rules** 
**Pseudo-query**

```sql
```

---

## Q3: Release lease

**Question** 
**Used by** 
**Inputs** 
**Returns**
**Rules** 
**Pseudo-query**

```sql
```

---

## Q4: Find stale leases

**Question** 
**Used by** 
**Inputs** 
**Returns**
**Rules** 
**Pseudo-query**

```sql
```

---

## Q5: Get runs needing transfer

**Question**
**Used by**
**Inputs**
**Returns**
**Rules** 
**Pseudo-query**

```sql
```

---

## Q6: Submit stage job (where/how to run)

**Question**
**Used by** 
**Inputs** 
**Returns** 
**Rules** 
**Pseudo-query**

```sql
```

*(Note: actual sbatch happens in orchestrator code; this call is just optional logging.)*

---
