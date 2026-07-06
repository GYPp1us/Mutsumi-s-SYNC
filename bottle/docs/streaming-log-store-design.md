# Streaming Log Store Design

**Status:** Implemented on v3 mainline  
**Scope:** `src/mutsumi_sync/logging.py`, `src/mutsumi_sync/main.py`, `config.yaml`

## Goal

The application should persist the same diagnostic log stream that operators see in stdout and TUI tools. The store is append-only NDJSON so it can be tailed, replayed, filtered, and consumed later by dashboard views without depending on an in-memory queue.

## Configuration

```yaml
logging:
  stream_store:
    enabled: true
    path: data/logs/mutsumi.ndjson
    max_bytes: 52428800
    backup_count: 5
    keep_ansi: true
```

`path` is resolved relative to the process working directory. In production this points into the shared `data/` symlink, so logs survive release switches.

## Data Format

Each log event is one JSON object on one line:

```json
{
  "schema": "mutsumi.log.v1",
  "ts": "2026-07-06T06:30:00.000000+00:00",
  "level": "INFO",
  "logger": "mutsumi.pipeline",
  "message": "...",
  "ansi": true,
  "module": "pipeline",
  "function": "pipeline",
  "line": 631,
  "process": 1234,
  "thread": "MainThread"
}
```

Multi-line records such as `CONTEXT`, LLM results, and tool logs remain a single NDJSON event. This keeps the storage stream honest and makes replay unambiguous.

## Runtime Flow

`main.setup_logging()` still installs stdout logging for systemd/journald. When `logging.stream_store.enabled` is true, it also installs a `QueueHandler` backed by a `QueueListener` and `RotatingFileHandler`.

This keeps pipeline code on the normal `logging.getLogger("mutsumi.xxx")` path while moving file I/O to a background listener thread. `stop_stream_log_store()` flushes and closes the listener during shutdown and test cleanup.

## Future Dashboard Use

The dashboard can later read the last N NDJSON records at startup and then tail the file for new records. That should complement, not replace, the current in-process queue used for live logs.
