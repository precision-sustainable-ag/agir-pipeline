# AgirDB Documentation Index

Complete documentation for AgirDB organized into modular pages.

## Main Documentation

📘 **[README.md](README.md)** - Main index with quick start and API overview

## Core API Components

🔌 **[core-connection.md](core-connection.md)** - Connection management and transactions  
🔍 **[pipeline-gaps.md](pipeline-gaps.md)** - Pipeline gap analysis (work discovery)  
⚙️ **[stage-status.md](stage-status.md)** - Stage status tracking (prevent duplicate work)  
🖼️ **[image-metadata.md](image-metadata.md)** - Image metadata management  
📦 **[batch-metadata.md](batch-metadata.md)** - Batch metadata management  
📝 **[event-logging.md](event-logging.md)** - Event logging for auditing  
📂 **[inventory-sync.md](inventory-sync.md)** - File inventory synchronization  
🔄 **[transfer-management.md](transfer-management.md)** - JUNO transfer operations  
📊 **[analytics.md](analytics.md)** - Reporting and statistics  
🔄 **[migration.md](migration.md)** - SQLite data import

## Guides and References

🎯 **[orchestration.md](orchestration.md)** - Complete workflow examples  
✨ **[best-practices.md](best-practices.md)** - Production-ready patterns  
⚠️ **[exceptions.md](exceptions.md)** - Exception handling reference  
🔧 **[troubleshooting.md](troubleshooting.md)** - Common issues and solutions  
🗄️ **[schema.md](schema.md)** - Database schema reference  
⚡ **[installation.md](installation.md)** - Installation and setup guide

---

## Quick Navigation

### By Task

**Getting Started:**
1. [Installation Guide](installation.md)
2. [Quick Start](README.md#quick-start)
3. [First Workflow](orchestration.md)

**Processing Workflows:**
- [Basic RAW→JPG Pipeline](orchestration.md#example-1-basic-rawjpg-processing-pipeline)
- [Multi-Stage Pipeline](orchestration.md#example-2-multi-stage-pipeline)
- [Parallel Processing](orchestration.md#example-6-parallel-processing)
- [Integration with svs-raw-api](orchestration.md#example-7-integration-with-svs-raw-api)

**Monitoring & Analytics:**
- [Generate Reports](orchestration.md#example-3-monitoring-and-analytics)
- [Error Recovery](orchestration.md#example-5-error-recovery)
- [Analytics API](analytics.md)

**Problem Solving:**
- [Exception Reference](exceptions.md)
- [Troubleshooting Guide](troubleshooting.md)
- [Best Practices](best-practices.md)

### By Concept

**Work Discovery:**
- [Pipeline Gaps Component](pipeline-gaps.md) - How to find work that needs doing
- [Gap Analysis Patterns](pipeline-gaps.md#usage-patterns)

**Preventing Duplicate Work:**
- [Stage Status Component](stage-status.md) - How to claim and track work
- [Lock Management](stage-status.md#usage-patterns)

**Metadata Management:**
- [Image Metadata](image-metadata.md) - Individual image records
- [Batch Metadata](batch-metadata.md) - Batch-level records
- [Using Metadata Fields](best-practices.md#8-use-metadata-fields-for-extensibility)

---

## File Structure

```
docs/
├── README.md                    # Main index page
├── INDEX.md                     # This file
│
├── Core Components/
│   ├── core-connection.md       # Connection & transactions
│   ├── pipeline-gaps.md         # Work discovery
│   ├── stage-status.md          # Status tracking
│   ├── image-metadata.md        # Image records
│   ├── batch-metadata.md        # Batch records
│   ├── event-logging.md         # Event logging
│   ├── inventory-sync.md        # File inventory
│   ├── transfer-management.md   # Transfers
│   ├── analytics.md             # Analytics
│   └── migration.md             # SQLite import
│
└── Guides & Reference/
    ├── orchestration.md         # Complete workflows
    ├── best-practices.md        # Production patterns
    ├── exceptions.md            # Error handling
    ├── troubleshooting.md       # Problem solving
    ├── schema.md                # Database schema
    └── installation.md          # Setup guide
```

---

## Documentation Features

✅ **Modular Structure** - Each component has its own page  
✅ **One-Line Summaries** - Quick reference in main index  
✅ **Cross-References** - Links between related pages  
✅ **Complete Examples** - Real-world workflow patterns  
✅ **Production-Ready** - Best practices and error handling  
✅ **Searchable** - Clear sections and navigation  

---

## Total API Methods: ~35

- **Connection:** 6 methods
- **Pipeline Gaps:** 5 methods
- **Stage Status:** 6 methods
- **Image Metadata:** 7 methods
- **Batch Metadata:** 5 methods
- **Event Logging:** 5 methods
- **Inventory Sync:** 4 methods
- **Transfers:** 6 methods
- **Analytics:** 5 methods
- **Migration:** 2 methods

---

Start with [README.md](README.md) for the main documentation entry point.
