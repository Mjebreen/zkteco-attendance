#!/usr/bin/env python3
"""Docker healthcheck for the collector: the heartbeat file must be fresh.

The collector touches HEARTBEAT_FILE every scheduler tick (at least every ~10 s),
so a stale file means the process is wedged, not merely that the device is offline.
"""

import os
import sys
import time

path = os.getenv("HEARTBEAT_FILE", "/tmp/collector-heartbeat")
max_age = int(os.getenv("HEARTBEAT_MAX_AGE_SECONDS", "180"))
try:
    age = time.time() - os.path.getmtime(path)
except OSError:
    print("heartbeat file missing")
    sys.exit(1)
if age > max_age:
    print(f"heartbeat stale: {age:.0f}s")
    sys.exit(1)
print(f"ok ({age:.0f}s)")
