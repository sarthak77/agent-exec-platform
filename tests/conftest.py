"""Pytest bootstrap for the platform integration tests.

The tests act as a *client* of the running services, so they need the generated
protobuf/gRPC stubs (the ``aep.*`` package). Rather than vendor another copy, we
reuse the stubs already generated for agent_execution_service by putting that
service's ``src`` directory on ``sys.path``. This keeps the client's message
definitions byte-for-byte identical to what the AES server serialises.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent
_AES_SRC = _REPO_ROOT / "services" / "agent_execution_service" / "src"

# Prepend so `import aep.agent_execution.v1...` resolves to the generated stubs,
# and so sibling test modules can `import harness` regardless of pytest's
# import mode.
for _path in (str(_AES_SRC), str(_TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
