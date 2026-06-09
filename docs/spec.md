# Specification

Detailed specification of `httpflow`.

## Table of contents

- [Output format](#output-format)
- [TOML specification](#toml-specification)
  - [Design policy](#design-policy)
  - [Sample](#sample)
  - [Field list](#field-list)
  - [Parse rules](#parse-rules)
  - [Path notation for `capture`](#path-notation-for-capture)
  - [Capture sources](#capture-sources)
  - [SLEEP special step](#sleep-special-step)
  - [Polling with `until`](#polling-with-until)
- [Template notation](#template-notation)
- [Generated script structure](#generated-script-structure)
- [Exit codes](#exit-codes)

---

## Output format

By default, for each step the request and response are printed with
curl `-vvv`-style `>` (request) and `<` (response) prefixes, including
headers and body.

```
==> 2026-05-19 23:35:49.123 [getToken] POST https://api.example.com/auth
    > POST /auth HTTP/1.1
    > Host: api.example.com
    > Content-Length: 31
    > User-Agent: Python-urllib/3.12
    > Accept-Encoding: identity
    > Content-Type: application/json
    >
    > {"user":"test","pass":"secret"}
<== 2026-05-19 23:35:49.456 [getToken] status=200
    < HTTP/1.1 200 OK
    < Content-Type: application/json
    < Content-Length: 27
    <
    < {"access_token":"tok-xyz"}
    * capture token = '***'
```

Each step prints local time (millisecond precision) on the `==>` (right
before send) and `<==` (right after receive) lines. The request side also
shows headers `urllib` auto-adds (such as `Host`), and the response side
shows the status line such as `HTTP/1.1 200 OK`.
With `--quiet` (`-q`) only the two summary lines are shown.

---

## TOML specification

### Design policy

The top priority is **fit one request into one `[[requests]]` block**.
`headers` / `body_form` / `capture` are written as **arrays of strings**
in the form `"Key: Value"` / `"key = value"` rather than as sub-tables.

- Familiar — same notation as HTTP / curl
- Multi-line and trailing commas keep things readable as items grow
- Each block is self-contained, which improves scannability

### Sample

```toml
# workflow.toml

[[requests]]
name    = "getToken"
method  = "POST"
url     = "https://api.example.com/auth"
headers = ["Content-Type: application/json"]
body    = '''
{"user":"test","pass":"secret"}
'''
capture = ["token = access_token"]


[[requests]]
name    = "wait"
method  = "SLEEP"
url     = "2"


[[requests]]
name    = "getUser"
method  = "GET"
url     = "https://api.example.com/me"
headers = [
    "Authorization: Bearer ${token}",
    "Accept: application/json",
]
capture = ["user_id = data.user.id"]


[[requests]]
name    = "updateProfile"
method  = "PUT"
url     = "https://api.example.com/profile"
headers = [
    "Authorization: Bearer ${token}",
    "Content-Type: application/x-www-form-urlencoded",
]
body_form = [
    "nickname = new_name",
    "email    = test@example.com",
]
```

### Field list

| Field       | Required | Type           | Description |
|-------------|----------|----------------|-------------|
| `name`      | ✓        | string         | Step name (used for variable references) |
| `method`    | ✓        | string         | HTTP method (GET/POST/PUT/DELETE) or special step (`SLEEP`) |
| `url`       | ✓        | string         | Request URL, or the parameter for a special step (e.g. seconds for SLEEP) |
| `headers`   | -        | array[string]  | `"Key: Value"` form |
| `body`      | -        | string         | Raw text body (mutually exclusive with `body_form`) |
| `body_form` | -        | array[string]  | `"key = value"` form; `application/x-www-form-urlencoded` is auto-added |
| `capture`   | -        | array[string]  | `"var_name = source"` form (see "Capture sources" below) |
| `until`     | -        | array[string]  | Polling settings (see below). Repeat the request until a condition is met |

### Parse rules

| Field       | Separator     | Split count | Example | Result |
|-------------|---------------|-------------|---------|--------|
| `headers`   | first `:`     | 1           | `"Authorization: Bearer abc"` | `{"Authorization": "Bearer abc"}` |
| `body_form` | first `=`     | 1           | `"email = a@example.com"`     | `{"email": "a@example.com"}` |
| `capture`   | first `=`     | 1           | `"token = access_token"`      | `{"token": "access_token"}` |

- Whitespace around the separator is trimmed
- Even if the separator character also appears in the value, **only the first occurrence** is treated as a separator
  - Example: `"X-Url: https://example.com:8080/path"` → `key=X-Url`, `value=https://example.com:8080/path`

### Path notation for `capture`

Extract a value from a JSON response and store it under
`store["vars"][<var_name>]` in the variable store.

```jsonc
// Response
{
  "data": { "user": { "id": 42, "tags": ["admin", "owner"] } }
}
```

```toml
capture = [
    "uid       = data.user.id",
    "first_tag = data.user.tags[0]",
]
```

- Dots descend through the hierarchy
- `[N]` selects a list index
- A missing path stops execution with an error
- Captured values can be referenced later as `${uid}` or `${var.uid}`

### Capture sources

The right-hand side of a `capture` entry defaults to a JSON path into the
**response body** (backward compatible). A namespace prefix selects a
different source, including response headers and request-time values that
never appear in the response:

| `source` syntax              | Captured from                                          |
|------------------------------|-------------------------------------------------------|
| `<json.path>` (no prefix)    | response body JSON (default)                           |
| `response.body.<json.path>`  | response body JSON (explicit form)                    |
| `response.header.<Name>`     | response header value (case-insensitive)              |
| `request.header.<Name>`      | request header value sent (case-insensitive)          |
| `request.url`                | request URL after template expansion                  |
| `request.body`               | request body as sent (urlencoded for `body_form`)     |

```toml
capture = [
    "token     = access_token",                  # response body (default)
    "location  = response.header.Location",      # response header
    "sent_auth = request.header.Authorization",  # request header
    "called    = request.url",                   # request URL
    "sent_body = request.body",                  # request body
]
```

- Header lookups are case-insensitive; a missing header stops execution with an error.
- `request.header.*` only sees headers you set in `headers` (plus the auto-added
  `Content-Type` for `body_form`), not transport headers added by `urllib`
  (`Host`, `User-Agent`, `Content-Length`, `Accept-Encoding`).
- Only response-body captures require a JSON response; header/request captures do not.

### SLEEP special step

Setting `method = "SLEEP"` inserts a step that waits for a specified number of seconds.

```toml
[[requests]]
name   = "wait2s"
method = "SLEEP"
url    = "2"
```

- Specify the wait in seconds via `url` (template variables such as `${var.delay}` are also allowed).
- `headers` / `body` / `body_form` / `capture` cannot be set (validation error).
- Runtime output:
  ```
  ==> 2026-05-20 01:00:00.000 [wait2s] SLEEP 2
      > sleep 2.0 seconds
  <== 2026-05-20 01:00:02.000 [wait2s] done
  ```

### Polling with `until`

Use the optional `until` field to repeatedly send the same request until a condition is satisfied. This is useful when you need to wait for a resource to reach a target state (for example, `active`).

```toml
[[requests]]
name    = "pollStatus"
method  = "GET"
url     = "https://api.example.com/jobs/${id}"
capture = ["status = data.status"]
until   = [
    "condition    = ${status} == active",
    "interval     = 2.0",
    "max_attempts = 30",
]
```

| Key           | Required | Type  | Default | Description                                    |
|---------------|----------|-------|---------|------------------------------------------------|
| `condition`   | Yes      | string| —       | Expression to evaluate after each attempt      |
| `interval`    | No       | float | `1.0`   | Seconds to wait between attempts               |
| `max_attempts`| No       | int   | `10`    | Maximum number of attempts (raises error on exhaustion) |

How it works:

1. Send the request (first attempt outputs the normal request/response log).
2. Evaluate `capture`, update the variable store.
3. Expand templates in `condition`, then evaluate.
4. If truthy, proceed to the next step. If falsy, wait for `interval` seconds and repeat from 1.
5. If `max_attempts` is exceeded without satisfying the condition, the step fails.

Supported operators in `condition`:

| Operator | Example                              | Meaning                     |
|----------|--------------------------------------|-----------------------------|
| `==`     | `${status} == active`                | String equality             |
| `!=`     | `${status} != pending`               | String inequality           |
| `~`      | `${message} ~ /success/i`            | Regular expression match (`/pattern/flags`) |
| `in`     | `${code} in [200, 201, 204]`         | Included in a comma-separated list |

- Both operands are evaluated as strings after template expansion.
- HTTP 4xx/5xx responses are treated like normal responses during polling; capture and `until` evaluation still run. Transport errors still fail.

---

## Template notation

Variable references use the `${...}` form. Escape `$` with `$$`.

```toml
url     = "https://api.${var.env}.example.com/me"
headers = ["Authorization: Bearer ${token}"]
body    = '{"price":"$$100"}'   # → {"price":"$100"}
```

Available namespaces:

- `<name>` — shorthand for a captured value or injected variable in `store["vars"]`
- `var.<name>` — variables in `store["vars"]` (including CLI `-v key=value` and captured values)
- `env.<name>` — environment variables
- `random.UUID` / `random.UUID_HEX` — generated UUID values
- `time.DATE_ISO` / `time.DATE_YMD` / `time.DATE_YMDHMS` — current timestamp placeholders

Referencing an undefined variable raises `TemplateError` and stops execution.

### `time.*` placeholders

| Placeholder        | Output example                     | Format                 |
|--------------------|------------------------------------|------------------------|
| `${time.DATE_ISO}` | `2026-06-09T12:34:56.123456+09:00` | ISO 8601 with microseconds |
| `${time.DATE_YMD}` | `20260609`                         | `%Y%m%d`               |
| `${time.DATE_YMDHMS}` | `20260609123456`                | `%Y%m%d%H%M%S`         |

---

## Generated script structure

The generated script is laid out to prioritize **readability and ease of
ad-hoc editing**. Each `[[requests]]` block expands to an independent
`step_<name>` function that calls the shared `run_step` helper.

```python
def step_getToken(store, quiet=False, pretty_json=False, no_mask=False, blank_line=0):
    """[[requests]] name = 'getToken' — POST https://api.example.com/auth"""
    for _ in range(blank_line):
        print()
    run_step(
        store, 'getToken', 'POST', 'https://api.example.com/auth',
        headers={
            'Content-Type': 'application/json',
        },
        body='{"user":"test","pass":"secret"}',
        capture={'token': 'access_token'},
        quiet=quiet, pretty_json=pretty_json, no_mask=no_mask,
    )

def main():
    ...
    # === Workflow ===
    # Comment out a line to skip that step. Reorder lines to change execution order.
    step_getToken(store, quiet=args.quiet, pretty_json=args.pretty_json, no_mask=args.no_mask, blank_line=0)
    step_getUser(store, quiet=args.quiet, pretty_json=args.pretty_json, no_mask=args.no_mask, blank_line=args.blank_line)
```

- Each step function accepts `blank_line` parameter; only the second and subsequent steps in main execute the blank-line logic (the first step passes `0` unconditionally).
- Generated scripts support `-v`, `-q`/`--quiet`, `--pretty-json`, `--no-mask`, `--blank-line`.

Common editing use cases:

- **Re-run just this step** → comment out the other step calls in `main()`
- **Reorder steps** → reorder the calls in `main()`
- **Slightly tweak URL/headers/body and re-run** → edit the corresponding `step_*` function directly
- **Add a brand-new step** → copy an existing function, rename it, change the contents, and add one line to `main()`

The runtime helpers (`render` / `extract` / `do_request` / `run_step` / `mask_*`) are
inlined at the top of the generated script from `runtime/*.py` (flattened via
`generator._flatten_modules()`) and do not depend on this tool's codebase.

---

## Exit codes

| Code | Meaning |
|------|---------|
| `0`  | All steps succeeded |
| `1`  | TOML parse failure / validation failure / HTTP failure / capture failure, etc. |
