# Analytics Component

[← Back to Index](index.md)

The `analytics` component provides reporting and statistics.

---

## Methods

### `get_pipeline_summary()`

Get summary statistics for entire pipeline.

```python
summary = db.analytics.get_pipeline_summary()
```

**Returns:** `Dict` - Pipeline-wide statistics:
```python
{
    'total_batches': 45,
    'total_images': 15000,
    'stages': {
        'raw_to_dng': {
            'complete': 44,
            'in_progress': 1,
            'pending': 0,
            'completion_rate': 97.8
        }
    }
}
```

---

### `get_batch_statistics()`

Get detailed statistics for a batch.

```python
stats = db.analytics.get_batch_statistics(batch_id='B001')
```

**Parameters:**
- `batch_id` (str): Batch identifier

**Returns:** `Dict` - Batch statistics

**Raises:**
- `BatchNotFoundError`: If doesn't exist

---

### `get_processing_rates()`

Get processing rates and throughput.

```python
rates = db.analytics.get_processing_rates(
    stage='raw_to_jpg',
    start_time='2025-01-15T00:00:00Z',
    end_time='2025-01-15T23:59:59Z'
)
```

**Parameters:**
- `stage` (str, optional): Filter by stage
- `start_time` (str, optional): Start time (ISO 8601)
- `end_time` (str, optional): End time (ISO 8601)

**Returns:** `Dict` - Processing rate metrics

---

### `get_error_summary()`

Get summary of errors and failures.

```python
errors = db.analytics.get_error_summary(
    start_time='2025-01-15T00:00:00Z',
    end_time='2025-01-15T23:59:59Z'
)
```

**Parameters:**
- `start_time` (str, optional): Start time (ISO 8601)
- `end_time` (str, optional): End time (ISO 8601)

**Returns:** `Dict` - Error summary

---

### `export_report()`

Export analytics data to CSV or JSON.

```python
filepath = db.analytics.export_report(
    report_type='pipeline_summary',
    format='csv',
    output_path='/reports/pipeline.csv',
    start_time='2025-01-01T00:00:00Z',
    end_time='2025-01-31T23:59:59Z'
)
```

**Parameters:**
- `report_type` (str): Report type
- `format` (str): Output format ('csv' or 'json')
- `output_path` (str): Where to save
- `start_time` (str, optional): Start time
- `end_time` (str, optional): End time

**Returns:** `str` - Path to generated file

---

[← Back to Index](README.md)
