# Transfer Management Component

[← Back to Index](index.md)

The `transfers` component manages JUNO transfer operations.

---

## Methods

### `create()`

Create a new transfer request.

```python
transfer_id = db.transfers.create(
    batch_id='B001',
    source_path='/local/data/raw/B001',
    destination_path='/juno/archive/raw/B001',
    transfer_type='upload',
    priority='high',
    metadata={'size_gb': 15.5}
)
```

**Parameters:**
- `batch_id` (str): Associated batch
- `source_path` (str): Source path
- `destination_path` (str): Destination path
- `transfer_type` (str): Type ('upload', 'download', 'move')
- `priority` (str, optional): Priority ('low', 'normal', 'high'). Default: 'normal'
- `metadata` (dict, optional): Additional metadata

**Returns:** `str` - Transfer ID

**Raises:**
- `BatchNotFoundError`: If batch doesn't exist
- `ValidationError`: If parameters invalid

---

### `start()`

Mark transfer as started.

```python
db.transfers.start(
    transfer_id='uuid-string',
    globus_task_id='globus-task-123',
    estimated_duration=3600
)
```

**Parameters:**
- `transfer_id` (str): Transfer identifier
- `globus_task_id` (str, optional): Globus task ID
- `estimated_duration` (int, optional): Estimated seconds

**Returns:** `None`

**Raises:**
- `TransferNotFoundError`: If doesn't exist
- `TransferAlreadyInProgressError`: If already started

---

### `complete()`

Mark transfer as completed.

```python
db.transfers.complete(
    transfer_id='uuid-string',
    success=True,
    files_transferred=150,
    bytes_transferred=15500000000
)
```

**Parameters:**
- `transfer_id` (str): Transfer identifier
- `success` (bool): Whether succeeded
- `files_transferred` (int, optional): Files count
- `bytes_transferred` (int, optional): Bytes count
- `error_message` (str, optional): Error if failed

**Returns:** `None`

**Raises:**
- `TransferNotFoundError`: If doesn't exist

---

### `get_status()`

Get status of a transfer.

```python
status = db.transfers.get_status(transfer_id='uuid-string')
```

**Parameters:**
- `transfer_id` (str): Transfer identifier

**Returns:** `Dict` - Transfer status

**Raises:**
- `TransferNotFoundError`: If doesn't exist

---

### `list_pending()`

Get all pending transfers.

```python
transfers = db.transfers.list_pending(
    priority='high',
    limit=20
)
```

**Parameters:**
- `priority` (str, optional): Filter by priority
- `limit` (int, optional): Maximum to return. Default: 20

**Returns:** `List[Dict]` - Pending transfers

---

### `cancel()`

Cancel a pending/in-progress transfer.

```python
db.transfers.cancel(
    transfer_id='uuid-string',
    reason='User requested'
)
```

**Parameters:**
- `transfer_id` (str): Transfer identifier
- `reason` (str, optional): Cancellation reason

**Returns:** `None`

**Raises:**
- `TransferNotFoundError`: If doesn't exist

---

[← Back to Index](index.md)
