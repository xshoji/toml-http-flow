from __future__ import annotations

import re

from .names import env_name
from .shell import dq_preserve_expansion


class PlaceholderRenderer:
    """Render httpflow placeholders into bash-compatible expressions."""

    def __init__(self, captured_vars: set[str]) -> None:
        """Create a renderer aware of captured variable shorthand."""
        self._captured_vars = captured_vars

    def expand(self, value: str) -> str:
        """Expand placeholders for heredocs, headers, and form rows."""
        s = value.replace("${random.UUID_HEX}", "$(uuid_hex)")
        s = s.replace("${random.UUID}", "$(uuid)")
        s = s.replace("${time.DATE_ISO}", "$(time_date_iso)")
        s = s.replace("${time.DATE_YMDHMS}", "$(time_date_ymdhms)")
        s = s.replace("${time.DATE_YMD}", "$(time_date_ymd)")
        s = re.sub(
            r"\$\{env\.([A-Za-z_][A-Za-z0-9_]*)\}",
            lambda m: "${" + m.group(1) + "}",
            s,
        )
        s = re.sub(
            r"\$\{var\.([\w\-]+)\}",
            lambda m: "${" + env_name("VAR", m.group(1)) + "}",
            s,
        )
        if self._captured_vars:
            def _repl_captured(m: "re.Match[str]") -> str:
                name = m.group(1)
                if name in self._captured_vars:
                    return "${" + env_name("VAR", name) + "}"
                return m.group(0)
            s = re.sub(r"\$\{([\w\-]+)\}", _repl_captured, s)
        return s

    def expr(self, value: str) -> str:
        """Return a double-quoted bash expression for assignment contexts."""
        return dq_preserve_expansion(self.expand(value))
