# toml-http-flow (`httpflow`)

A CLI tool that runs a workflow of HTTP requests defined in TOML, in order.
Implemented using **only the Python 3.11+ standard library** — zero external
dependencies.

It also ships a `generate` subcommand that emits a **single self-contained
Python script** from a workflow TOML — useful for archiving, distribution,
and embedding into CI/CD pipelines without this tool installed.

## Features

- Describe a workflow as one request per block (`[[requests]]`) in TOML
- Reference captured values from later steps via `${name}` or `${var.name}`
- Inject external variables with `-v key=value` (referenced as `${var.<name>}`)
- Extract values from JSON responses using `data.user.id` / `items[0].id` style paths
- **Special step `SLEEP`** lets you insert a wait of N seconds
- Implemented purely on the standard library (`tomllib`, `urllib`, `json`, `argparse`)
- Generates a single self-contained Python script (`generate` subcommand)

## Requirements

- Python 3.11 or newer (because `tomllib` ships in the standard library)

## Installation

```bash
# Run directly from GitHub with pipx
pipx run --spec git+https://github.com/xshoji/toml-http-flow.git httpflow --help

# Clone the repo and run directly
git clone https://github.com/xshoji/toml-http-flow.git
cd toml-http-flow
python3 -m httpflow --help

# Or install with pip (editable)
pip install -e .
httpflow --help
```

## Usage

### Running a workflow

```bash
# Basic
python3 -m httpflow run -f workflow.toml

# `run` can be omitted (backward compatibility)
python3 -m httpflow -f workflow.toml

# Inject variables
python3 -m httpflow run -f workflow.toml -v env=production -v user_id=123

# By default, request/response details are shown.
# Use --quiet (-q) when you only need the summary lines.
python3 -m httpflow run -f workflow.toml -q

# Pretty-print JSON bodies with 2-space indent
python3 -m httpflow run -f workflow.toml --pretty-json

# Disable masking of sensitive fields
python3 -m httpflow run -f workflow.toml --no-mask
```

### Output format

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

### Generating a single script

```bash
# Write to a .py file
python3 -m httpflow generate -f workflow.toml -o workflow.py

# Write to stdout
python3 -m httpflow generate -f workflow.toml

# Prepend an executable shebang
python3 -m httpflow generate -f workflow.toml -o workflow.py --shebang

# Embed default variables
python3 -m httpflow generate -f workflow.toml -v env=production -o workflow.py
```

The generated script does not depend on this tool and runs anywhere:

```bash
python3 workflow.py
python3 workflow.py -v env=staging --quiet
```

#### Structure of the generated script (designed to be edited by hand)

The generated script is laid out to prioritize **readability and ease of
ad-hoc editing**. Each `[[requests]]` block expands to an independent
`step_<name>` function that calls the shared `run_step` helper.

```python
def step_getToken(store, quiet=False, pretty_json=False, no_mask=False):
    """[[requests]] name = 'getToken' — POST https://api.example.com/auth"""
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
    step_getToken(store, quiet=args.quiet, pretty_json=args.pretty_json, no_mask=args.no_mask)
    step_getUser(store, quiet=args.quiet, pretty_json=args.pretty_json, no_mask=args.no_mask)
```

Common editing use cases:

- **Re-run just this step** → comment out the other step calls in `main()`
- **Reorder steps** → reorder the calls in `main()`
- **Slightly tweak URL/headers/body and re-run** → edit the corresponding `step_*` function directly
- **Add a brand-new step** → copy an existing function, rename it, change the contents, and add one line to `main()`

The runtime helpers (`render` / `extract` / `do_request` / `run_step` / `mask_*`) are
inlined at the top of the generated script from `embedded_runtime.py` and do not
depend on this tool's codebase.

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
| `capture`   | -        | array[string]  | `"var_name = json.path"` form |
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
- HTTP errors (4xx/5xx) during polling fail immediately without retry.

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
- `repeat.<name>` — variables provided by `--repeat-vars`
- `env.<name>` — environment variables
- `random.UUID` / `random.UUID_HEX` — generated UUID values

Referencing an undefined variable raises `TemplateError` and stops execution.

## Project layout

```
toml-http-flow/
├── pyproject.toml
├── README.md
├── AGENTS.md
├── docs/
│   └── design/
│       ├── 01-overview.md
│       ├── 02-architecture.md
│       ├── 03-toml-spec.md
│       ├── 04-template.md
│       ├── 05-cli.md
│       ├── 06-workflow-flow.md
│       ├── 07-script-generation.md
│       ├── 08-error-handling.md
│       ├── 09-testing.md
│       ├── 10-go-python-diff.md
│       └── 11-extension-points.md
├── httpflow/
│   ├── __init__.py
│   ├── __main__.py          # entry point for `python -m httpflow`
│   ├── cli.py               # CLI argument parsing and dispatch
│   ├── config.py            # TOML → WorkflowSpec loader / validation
│   ├── model.py             # WorkflowSpec / HttpStep / SleepStep / Body union
│   ├── runner.py            # step execution engine and variable store
│   ├── embedded_runtime.py  # source-of-truth helpers shared with generated scripts
│   ├── generator.py         # WorkflowSpec → standalone .py emitter
│   ├── httpclient.py        # urllib HTTP client (embedded_runtime wrapper)
│   ├── template.py          # ${...} expansion engine (embedded_runtime wrapper)
│   ├── masking.py           # log output masking (embedded_runtime wrapper)
│   ├── until.py             # until condition evaluator (embedded_runtime wrapper)
│   ├── workflow.py          # backward-compatible shim → runner
│   └── templates/
│       └── runner.py.tmpl   # frame template for generated scripts (placeholders only)
└── tests/
    ├── __init__.py
    ├── test_cli.py
    ├── test_config.py
    ├── test_description.py
    ├── test_generator.py
    ├── test_httpclient.py
    ├── test_masking.py
    ├── test_pretty_json.py
    ├── test_repeat.py
    ├── test_sleep.py
    ├── test_template.py
    ├── test_until.py
    └── test_workflow.py
```

### Key modules

| Module | Responsibility |
|---|---|
| `config.py` | TOML parsing → normalized `WorkflowSpec`. No longer returns raw `WorkflowConfig` for `load()`. |
| `model.py` | `WorkflowSpec`, `HttpStep`, `SleepStep`, `Body` union (`TextBody` / `FormBody`). |
| `runner.py` | Execution engine: iteration order, store updates, step branching. |
| `embedded_runtime.py` | Source-of-truth helpers (`render`, `extract`, `do_request`, `run_step`, `mask_*`, `eval_until`) used by both the package and the generated script. |
| `generator.py` | Thin emitter: `WorkflowSpec` → Python source. No long runtime strings. |
| `workflow.py` | Backward-compatible shim that re-exports from `runner`. |

## Development

```bash
# Run tests (standard-library unittest)
python3 -m unittest discover -s tests -v

# Smoke-check the CLI
python3 -m httpflow --help
python3 -m httpflow run --help
python3 -m httpflow generate --help

# Verify generated script compiles
python3 -m httpflow generate -f workflow.toml -o /tmp/g.py
python3 -c "import py_compile; py_compile.compile('/tmp/g.py', doraise=True)"
```

Tests spin up a local mock with the standard-library `http.server` so even
the HTTP round-trip is exercised end-to-end. Parity tests verify that the
generated script behaves identically to the package runtime.

## Exit codes

| Code | Meaning |
|------|---------|
| `0`  | All steps succeeded |
| `1`  | TOML parse failure / validation failure / HTTP failure / capture failure, etc. |

## License

[MIT](LICENSE)
