from __future__ import annotations

def sq(s: str) -> str:
    """Single-quote a string for bash (handles embedded ')."""
    return "'" + s.replace("'", "'\"'\"'") + "'"



def dq_preserve_expansion(s: str) -> str:
    """Double-quote a string for bash while preserving shell expansion."""
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'



def dq_literal(s: str) -> str:
    """Double-quote a string for bash, escaping all shell-special chars."""
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('`', '\\`') + '"'



def default_assignment(name: str, value: str) -> str:
    """Emit a bash line that sets a default for an env-style variable."""
    return f'if [ -z "${{{name}:-}}" ]; then {name}={sq(value)}; fi'



def mask_key_pattern(keys: str) -> str:
    """Return bash sed regex where each hyphen part's first letter is case-insensitive."""
    parts: list[str] = []
    for key in keys.split("|"):
        if not key:
            continue
        key_parts: list[str] = []
        for key_part in key.split("-"):
            if not key_part:
                key_parts.append(key_part)
                continue
            first = key_part[0]
            rest = key_part[1:]
            lower = first.lower()
            upper = first.upper()
            if lower != upper:
                key_parts.append(f"[{lower}{upper}]{rest}")
            else:
                key_parts.append(key_part)
        parts.append("-".join(key_parts))
    return "|".join(parts)

