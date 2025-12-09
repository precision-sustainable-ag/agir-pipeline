# Event Logging Component

[← Back to Index](index.md)

The `events` component logs processing events for auditing and debugging.

---

## Methods

### `log()`

Log a single processing event.

```python
event_id = db.events.log(
    event_type='processing_started',
    batch_id='B001',
    stage='raw_to_jpg',
    severity='info',
    message='Started RAW to JPG conversion',
    metadata={'worker_id': 'worker-001'}
)
```

**Parameters:**
- `event_type` (str): Event type category
- `batch_id` (str, optional): Associated batch
- `stage` (str, optional): Associated stage
- `image_id` (str, optional): Associated image
- `severity` (str): Level ('debug', 'info', 'warning', 'error', 'critical')
- `message` (str): Event description
- `metadata` (dict, optional): Additional data

**Returns:** `str` - Event ID

**Raises:**
- `ValidationError`: If required fields missing

---

### `log_bulk()`

Log multiple events efficiently.

```python
count = db.events.log_bulk(events_data)
```

**Parameters:**
- `events_data` (List[Dict]): List of event dictionaries

**Returns:** `int` - Number of events logged

**Raises:**
- `ValidationError`: If any event invalid

**Example:**
```python
events = [
    {
        'event_type': 'image_processed',
        'batch_id': 'B001',
        'image_id': 'MD_1683434234',
        'stage': 'raw_to_jpg',
        'severity': 'info',
        'message': 'Image converted'
    },
    # ... more events
]
count = db.events.log_bulk(events)
```

---

### `get()`

Get a specific event by ID.

```python
event = db.events.get(event_id='uuid-string')
```

**Parameters:**
- `event_id` (str): Event identifier

**Returns:** `Dict` - Event details

**Raises:**
- `NotFoundError`: If event doesn't exist

---

### `search()`

Search events by criteria.

```python
events = db.events.search(
    event_type='conversion_error',
    batch_id='B001',
    stage='raw_to_jpg',
    severity='error',
    start_time='2025-01-15T00:00:00Z',
    end_time='2025-01-15T23:59:59Z',
    limit=100
)
```

**Parameters:**
- `event_type` (str, optional): Filter by type
- `batch_id` (str, optional): Filter by batch
- `stage` (str, optional): Filter by stage
- `image_id` (str, optional): Filter by image
- `severity` (str, optional): Filter by severity
- `start_time` (str, optional): Start time (ISO 8601)
- `end_time` (str, optional): End time (ISO 8601)
- `limit` (int, optional): Maximum to return. Default: 100

**Returns:** `List[Dict]` - Matching events ordered by timestamp

**Raises:**
- `ValidationError`: If parameters invalid

**Example:**
```python
# Find errors in last 24 hours
yesterday = (datetime.now() - timedelta(days=1)).isoformat()
errors = db.events.search(
    severity='error',
    start_time=yesterday
)
```

---

### `get_recent()`

Get most recent events.

```python
events = db.events.get_recent(limit=50)
```

**Parameters:**
- `limit` (int, optional): Maximum to return. Default: 50

**Returns:** `List[Dict]` - Recent events (descending timestamp)

**Raises:**
- `QueryError`: If query fails

---

## Usage Patterns

### Log Processing Events

```python
with AgirDB() as db:
    # Start event
    db.events.log(
        event_type='batch_processing_started',
        batch_id='B001',
        stage='raw_to_jpg',
        severity='info',
        message='Starting batch processing'
    )
    
    # Process...
    
    # Complete event
    db.events.log(
        event_type='batch_processing_complete',
        batch_id='B001',
        stage='raw_to_jpg',
        severity='info',
        message='Completed successfully',
        metadata={'files_processed': 150, 'duration': 342.5}
    )
```

### Error Tracking

```python
with AgirDB() as db:
    try:
        process_image(image_id)
    except Exception as e:
        db.events.log(
            event_type='image_processing_error',
            batch_id='B001',
            image_id=image_id,
            stage='raw_to_jpg',
            severity='error',
            message=str(e),
            metadata={'traceback': traceback.format_exc()}
        )
```

### Audit Trail

```python
with AgirDB() as db:
    # Get all events for a batch
    events = db.events.search(batch_id='B001')
    
    print(f"Audit trail for B001:")
    for event in events:
        print(f"{event['created_at']}: {event['message']}")
```

---

## See Also

- [Stage Status](stage-status.md) - Processing status tracking
- [Troubleshooting](troubleshooting.md) - Using logs for debugging

[← Back to Index](index.md)
