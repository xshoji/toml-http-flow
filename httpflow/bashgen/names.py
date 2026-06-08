from __future__ import annotations

import re

def step_function_name(name: str, used: set[str]) -> str:
    """Sanitise a step name into a valid bash function identifier."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not cleaned or cleaned[0].isdigit():
        cleaned = "_" + cleaned
    base = "step_" + cleaned
    out = base
    i = 2
    while out in used:
        out = f"{base}_{i}"
        i += 1
    used.add(out)
    return out



def env_name(prefix: str, name: str) -> str:
    """Return a safe generated bash environment variable name."""
    return f"{prefix}_{re.sub(r'[^A-Za-z0-9_]', '_', name).upper()}"


