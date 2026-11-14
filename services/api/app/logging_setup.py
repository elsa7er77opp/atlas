"""
Project Atlas - structured logging
AT-26-L1-002  |  FR-17, FR-18

Identical record shape to the ingestion service. Deliberately so: a single
`docker compose logs` stream carries records from five containers, and a
uniform shape means one filter works across all of them.
"""

import json
import sys
from datetime import datetime, timezone

_COMPONENT = "api"


def log(level: str, message: str, **kw) -> None:
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level,
        "component": _COMPONENT,
        "message": message,
    }
    rec.update(kw)
    stream = sys.stderr if level in ("error", "critical") else sys.stdout
    print(json.dumps(rec, default=str), file=stream, flush=True)
