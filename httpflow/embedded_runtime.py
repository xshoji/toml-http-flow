"""Runtime helpers embedded into generated scripts."""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

# Path segments inside ${...} may contain letters, digits, underscores and
# hyphens; dots act as the path separator.
PATTERN = re.compile(r"\$(?:\$|\{([\w.\-]+)\})")
PATH_TOKEN = re.compile(r"([^\.\[\]]+)|\[(\d+)\]")
_UNTIL_OPS = [
    (re.compile(r"=="), "=="),
    (re.compile(r"!="), "!="),
    (re.compile(r"\s+in\s+"), "in"),
    (re.compile(r"~"), "~"),
]
_UNTIL_REGEX_RHS = re.compile(r"^/(.*)/([a-zA-Z]*)$")
_UNTIL_LIST_RHS = re.compile(r"^\[(.*)\]$")
_MASK_PLACEHOLDER = "***"
_MASK_DEFAULTS = frozenset({
    "authorization", "proxyauthorization", "cookie", "setcookie",
    "xapikey", "xauthtoken", "xaccesstoken",
    "xcsrftoken", "xxsrftoken",
    "xsessiontoken", "xsessionid", "xsecretkey",
    "password", "passwd", "pwd",
    "secret", "clientsecret",
    "token", "accesstoken", "refreshtoken", "idtoken",
    "authtoken", "sessiontoken",
    "apikey", "privatekey",
    "auth", "session", "sessionid",
    "creditcard", "cardnumber", "cvv", "cvc", "pin", "ssn",
})


class TemplateError(KeyError):
    """Raised when a referenced template variable is not found."""


def _lookup(store: dict, parts: list[str]) -> Any:
    if len(parts) == 2 and parts[0] == "env":
        try:
            return os.environ[parts[1]]
        except KeyError as exc:
            raise TemplateError(".".join(parts)) from exc
    if parts == ["random", "UUID"]:
        return uuid.uuid4()
    if parts == ["random", "UUID_HEX"]:
        return uuid.uuid4().hex
    if len(parts) == 2 and parts[0] == "var":
        try:
            return store["vars"][parts[1]]
        except KeyError as exc:
            raise TemplateError(".".join(parts)) from exc
    if len(parts) == 1 and parts[0] in store.get("vars", {}):
        return store["vars"][parts[0]]
    cur: Any = store
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            raise TemplateError(".".join(parts))
        cur = cur[p]
    return cur


def render(text: str, store: dict) -> str:
    """Render ``${path.to.value}`` references in ``text`` using ``store``."""
    def repl(m: re.Match) -> str:
        if m.group(0) == "$$":
            return "$"
        path = m.group(1)
        return str(_lookup(store, path.split(".")))
    return PATTERN.sub(repl, text)


def render_mapping(mapping: dict[str, str], store: dict) -> dict[str, str]:
    """Render every key and value in a string-to-string mapping."""
    return {render(k, store): render(v, store) for k, v in mapping.items()}


def poll_until(
    name: str,
    attempt_fn,
    condition: str,
    interval: float,
    max_attempts: int,
    store: dict[str, Any],
    quiet: bool,
    out=sys.stdout,
) -> None:
    """Re-run ``attempt_fn`` until ``eval_until(condition, store)`` becomes true."""
    for attempt in range(1, max_attempts + 1):
        attempt_fn()
        if eval_until(condition, store):
            if not quiet:
                print(f"    * until satisfied on attempt {attempt}", file=out)
            return
        if attempt < max_attempts:
            if not quiet:
                print(
                    f"    * until not satisfied (attempt {attempt}/{max_attempts}), "
                    f"retrying in {interval}s",
                    file=out,
                )
            time.sleep(interval)
    raise RuntimeError(
        f"step {name!r}: until condition not satisfied "
        f"after {max_attempts} attempts: {condition!r}"
    )


def extract(body: Any, path: str) -> Any:
    """Extract a value from a parsed JSON body using a dotted/indexed path."""
    cur: Any = body
    for name, idx in PATH_TOKEN.findall(path):
        if name:
            if not isinstance(cur, dict) or name not in cur:
                raise KeyError(f"path not found: {path}")
            cur = cur[name]
        else:
            i = int(idx)
            if not isinstance(cur, list) or i >= len(cur):
                raise IndexError(f"index out of range: {path}")
            cur = cur[i]
    return cur


def do_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body_bytes: bytes | None,
    timeout: float | None = None,
) -> tuple[int, str, dict[str, str], str, Any | None]:
    """Send an HTTP request and return status, reason, headers, text, and JSON body."""
    req = urllib.request.Request(url=url, data=body_bytes, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status, reason = resp.status, resp.reason
            resp_headers = dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        body = e.read() if e.fp is not None else b""
        raise RuntimeError(
            f"HTTP {e.code} from {method} {url}: {body.decode('utf-8', errors='replace')}"
        ) from e
    text = raw.decode("utf-8", errors="replace")
    try:
        body_json = json.loads(text) if text else None
    except json.JSONDecodeError:
        body_json = None
    return status, reason, resp_headers, text, body_json


def _until_flags(spec: str) -> int:
    flags = 0
    for ch in spec:
        if ch == "i":
            flags |= re.IGNORECASE
        elif ch == "m":
            flags |= re.MULTILINE
        elif ch == "s":
            flags |= re.DOTALL
        else:
            raise ValueError(f"until condition: unknown regex flag {ch!r}")
    return flags


def eval_until(condition: str, store: dict[str, Any]) -> bool:
    """Evaluate an until-condition string against the variable store."""
    best = None
    for pat, op in _UNTIL_OPS:
        m = pat.search(condition)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.end(), op)
    if best is None:
        raise ValueError(
            f"until condition: no operator (==, !=, ~, in) found in {condition!r}"
        )
    start, end, op = best
    lhs = render(condition[:start], store).strip()
    rhs = render(condition[end:], store).strip()

    if op == "==":
        return lhs == rhs
    if op == "!=":
        return lhs != rhs
    if op == "~":
        m = _UNTIL_REGEX_RHS.match(rhs)
        if m is None:
            raise ValueError(f"until condition: '~' RHS must be /pattern/[flags], got {rhs!r}")
        return re.search(m.group(1), lhs, _until_flags(m.group(2))) is not None
    if op == "in":
        m = _UNTIL_LIST_RHS.match(rhs)
        if m is None:
            raise ValueError(f"until condition: 'in' RHS must be [A, B, C], got {rhs!r}")
        items = [x.strip() for x in m.group(1).split(",") if x.strip() != ""]
        return lhs in items
    raise AssertionError(f"unreachable: unknown operator {op!r}")


def _mask_norm(name: str) -> str:
    return name.lower().replace("_", "").replace("-", "").replace(" ", "")


def _mask_targets() -> set[str]:
    base = set(_MASK_DEFAULTS)
    raw = os.environ.get("HTTPFLOW_MASK_EXTRA", "")
    base |= {_mask_norm(item) for item in raw.split(",") if item.strip()}
    return base


def _mask_obj(obj: Any, targets: set[str]) -> Any:
    if isinstance(obj, dict):
        return {
            k: (_MASK_PLACEHOLDER if isinstance(k, str) and _mask_norm(k) in targets
                else _mask_obj(v, targets))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_mask_obj(item, targets) for item in obj]
    return obj


def mask(text: str, disabled: bool = False) -> str:
    """Best-effort masking for a raw string."""
    if disabled or not text:
        return text
    targets = _mask_targets()
    try:
        return json.dumps(_mask_obj(json.loads(text), targets), ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        pass
    if "=" in text and "\n" not in text and " " not in text:
        try:
            pairs = urllib.parse.parse_qsl(text, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            return text
        masked = [(k, _MASK_PLACEHOLDER if _mask_norm(k) in targets else v) for k, v in pairs]
        return urllib.parse.urlencode(masked, safe="*")
    return text


def mask_url(url: str, disabled: bool = False) -> str:
    """Replace query-parameter values for sensitive keys in ``url``."""
    if disabled:
        return url
    parsed = urllib.parse.urlsplit(url)
    if not parsed.query:
        return url
    targets = _mask_targets()
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    masked = [(k, _MASK_PLACEHOLDER if _mask_norm(k) in targets else v) for k, v in pairs]
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(masked, safe="*")))


def mask_value(name: str, value: Any, disabled: bool = False) -> Any:
    """Return ``value`` masked when ``name`` matches a sensitive key."""
    if disabled:
        return value
    if _mask_norm(name) in _mask_targets():
        return _MASK_PLACEHOLDER
    return value


def build_repeat_iterations(
    repeat_vars: dict[str, list[str]] | None,
    required: set[str],
) -> list[dict[str, str]]:
    """Validate repeat variables and expand them into per-iteration dicts."""
    repeat_vars = dict(repeat_vars or {})
    missing = required - set(repeat_vars)
    if missing:
        raise ValueError(f"--repeat-vars missing for: {sorted(missing)}")
    if not repeat_vars:
        return [{}]
    lengths = {k: len(v) for k, v in repeat_vars.items()}
    distinct = set(lengths.values())
    if len(distinct) != 1:
        raise ValueError(
            f"--repeat-vars value counts must match across all keys, got: {lengths}"
        )
    n = distinct.pop()
    if n == 0:
        raise ValueError("--repeat-vars must supply at least one value per key")
    return [{k: repeat_vars[k][i] for k in repeat_vars} for i in range(n)]


def merge_default_repeat_vars(
    repeat_vars: dict[str, list[str]] | None,
    default_repeat_vars: dict[str, list[str]] | None,
) -> dict[str, list[str]]:
    """Merge runtime repeat vars on top of embedded defaults."""
    merged = dict(default_repeat_vars or {})
    merged.update(repeat_vars or {})
    return merged


def build_repeat_iterations_from_args(
    raw_items: list[str],
    default_repeat_vars: dict[str, list[str]] | None,
    required: set[str],
) -> list[dict[str, str]]:
    """Parse raw CLI repeat args, merge defaults, and expand iterations."""
    return build_repeat_iterations(
        merge_default_repeat_vars(parse_repeat_args(raw_items), default_repeat_vars),
        required,
    )


def parse_repeat_args(repeat_args: list[str]) -> dict[str, list[str]]:
    """Parse ``name=v1,v2`` repeat CLI entries into a mapping."""
    parsed: dict[str, list[str]] = {}
    for kv in repeat_args:
        if "=" not in kv:
            raise ValueError(f"--repeat-vars requires name=v1,v2,..., got: {kv!r}")
        k, _, v = kv.partition("=")
        k = k.strip()
        if not k:
            raise ValueError(f"--repeat-vars has empty key: {kv!r}")
        if k in parsed:
            raise ValueError(f"--repeat-vars duplicated key: {k!r}")
        values = [x.strip() for x in v.split(",")]
        if not values or any(x == "" for x in values):
            raise ValueError(f"--repeat-vars must supply non-empty comma-separated values: {kv!r}")
        parsed[k] = values
    return parsed


def _now() -> str:
    """Local time stamp with millisecond precision, e.g. ``2026-05-19 23:35:49.123``."""
    now = datetime.datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def _pretty(text: str, enabled: bool) -> str:
    """Re-format ``text`` as 2-space-indent JSON if it parses; else return as-is."""
    if not enabled or not text:
        return text
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        return text


def _print_lines(prefix: str, text: str, *, out=sys.stdout) -> None:
    """Print ``text`` line-by-line with ``prefix`` (e.g. '    > ' or '    < ')."""
    print(f"    {prefix}", file=out)
    for line in text.splitlines() or [""]:
        print(f"    {prefix} {line}", file=out)


def _log_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body_bytes: bytes | None,
    body_form: dict[str, str] | None,
    pretty_json: bool,
    no_mask: bool = False,
    *,
    out=sys.stdout,
) -> None:
    """Print the request line and headers that urllib will actually send."""
    parsed = urllib.parse.urlparse(mask_url(url, disabled=no_mask))
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    print(f"    > {method.upper()} {path} HTTP/1.1", file=out)
    print(f"    > Host: {parsed.netloc}", file=out)
    for k, v in headers.items():
        print(f"    > {k}: {mask_value(k, v, disabled=no_mask)}", file=out)
    lower = {h.lower() for h in headers}
    if body_bytes is not None:
        print(f"    > Content-Length: {len(body_bytes)}", file=out)
    if "user-agent" not in lower:
        print(
            f"    > User-Agent: Python-urllib/{sys.version_info.major}.{sys.version_info.minor}",
            file=out,
        )
    if "accept-encoding" not in lower:
        print("    > Accept-Encoding: identity", file=out)
    if body_form is not None:
        print("    > (form)", file=out)
        for k, v in body_form.items():
            print(f"    >   {k} = {mask_value(k, v, disabled=no_mask)}", file=out)
    elif body_bytes is not None:
        try:
            body_text = body_bytes.decode("utf-8", errors="replace")
            _print_lines(">", _pretty(mask(body_text, disabled=no_mask), pretty_json), out=out)
        except UnicodeDecodeError:
            print(f"    > <{len(body_bytes)} bytes>", file=out)


def _log_response(
    status: int,
    reason: str,
    resp_headers: dict[str, str],
    text: str,
    pretty_json: bool,
    no_mask: bool = False,
    *,
    out=sys.stdout,
) -> None:
    """Print the HTTP status line and response headers/body."""
    print(f"    < HTTP/1.1 {status} {reason}", file=out)
    for k, v in resp_headers.items():
        print(f"    < {k}: {mask_value(k, v, disabled=no_mask)}", file=out)
    if text:
        _print_lines("<", _pretty(mask(text, disabled=no_mask), pretty_json), out=out)


def run_step(
    store: dict,
    name: str,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    body_form: dict[str, str] | None = None,
    capture: dict[str, str] | None = None,
    description: str | None = None,
    quiet: bool = False,
    pretty_json: bool = False,
    no_mask: bool = False,
    out=sys.stdout,
) -> None:
    """Render, send, log, and capture a single HTTP (or SLEEP) attempt.

    On return, ``store["vars"]`` is updated with captured values.
    """
    url = render(url, store)

    if method == "SLEEP":
        try:
            seconds = float(url)
        except ValueError as exc:
            raise RuntimeError(
                f"step {name!r}: 'SLEEP' url must be numeric, got: {url!r}"
            ) from exc
        print(f"==> {_now()} [{name}] SLEEP {url}", file=out)
        if description:
            for line in description.splitlines() or [""]:
                print(f"    # {line}", file=out)
        if not quiet:
            print(f"    > sleep {seconds} seconds", file=out)
        time.sleep(seconds)
        print(f"<== {_now()} [{name}] done", file=out)
        return

    headers = render_mapping(headers or {}, store)
    if body is not None:
        body_bytes = render(body, store).encode("utf-8")
    elif body_form is not None:
        body_form = render_mapping(body_form, store)
        body_bytes = urllib.parse.urlencode(body_form).encode("utf-8")
        if not any(h.lower() == "content-type" for h in headers):
            headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        body_bytes = None

    print(f"==> {_now()} [{name}] {method.upper()} {mask_url(url, disabled=no_mask)}", file=out)
    if description:
        for line in description.splitlines() or [""]:
            print(f"    # {line}", file=out)
    if not quiet:
        _log_request(method, url, headers, body_bytes, body_form, pretty_json, no_mask=no_mask, out=out)

    status, reason, resp_headers, text, body_json = do_request(method, url, headers, body_bytes)
    print(f"<== {_now()} [{name}] status={status}", file=out)
    if not quiet:
        _log_response(status, reason, resp_headers, text, pretty_json, no_mask=no_mask, out=out)

    if capture:
        if body_json is None:
            raise RuntimeError(f"step {name!r}: capture requested but response is not JSON")
        for var, path in capture.items():
            captured = extract(body_json, path)
            store["vars"][var] = captured
            if not quiet:
                shown = mask_value(var, captured, disabled=no_mask)
                print(f"    * capture {var} = {shown!r}", file=out)
