# toml-http-flow (`httpflow`)

A CLI tool that runs a workflow of HTTP requests defined in TOML, in order.
Implemented using **only the Python 3.11+ standard library** — zero external
dependencies.

It also ships a `generate` subcommand that emits a **single self-contained
Python script** from a workflow TOML — useful for archiving, distribution,
and embedding into CI/CD pipelines without this tool installed.

## Features

- Describe a workflow as one request per block (`[[requests]]`) in TOML
- Reference previous responses from later steps via `${steps.<name>.<key>}`
- Inject external variables with `-v key=value` (referenced as `${vars.<name>}`)
- Extract values from JSON responses using `data.user.id` / `items[0].id` style paths
- **Special step `SLEEP`** lets you insert a wait of N seconds
- Implemented purely on the standard library (`tomllib`, `urllib`, `json`, `argparse`)
- Generates a single self-contained Python script (`generate` subcommand)

## Requirements

- Python 3.11 or newer (because `tomllib` ships in the standard library)

## Installation

```bash
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
```

### Output format

By default, for each step the request and response are printed with
curl `-vvv`-style `>` (request) and `<` (response) prefixes, including
headers and body.

```
==> 2026-05-19 23:35:49.123 [getToken]
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
    * capture token = 'tok-xyz'
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
`step_<name>` function.

```python
def step_getToken(store, quiet=False):
    """[[requests]] name = 'getToken' — POST https://api.example.com/auth"""
    name = 'getToken'
    method = 'POST'
    url = render('https://api.example.com/auth', store)
    headers = render_mapping({
        'Content-Type': 'application/json',
    }, store)
    body_form = None
    body_bytes = render('{"user":"test","pass":"secret"}', store).encode("utf-8")
    ...

def main():
    ...
    # === Workflow ===
    # Comment out a line to skip that step. Reorder lines to change execution order.
    step_getToken(store, quiet=args.quiet)
    step_getUser(store, quiet=args.quiet)
    step_updateProfile(store, quiet=args.quiet)
```

Common editing use cases:

- **Re-run just this step** → comment out the other step calls in `main()`
- **Reorder steps** → reorder the calls in `main()`
- **Slightly tweak URL/headers/body and re-run** → edit the corresponding `step_*` function directly
- **Add a brand-new step** → copy an existing function, rename it, change the contents, and add one line to `main()`

The runtime helpers (`render` / `extract` / `do_request` / `log_*`) are
inlined at the top of the generated script and do not depend on this tool's
codebase.

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
    "Authorization: Bearer ${steps.getToken.token}",
    "Accept: application/json",
]
capture = ["user_id = data.user.id"]


[[requests]]
name    = "updateProfile"
method  = "PUT"
url     = "https://api.example.com/profile"
headers = [
    "Authorization: Bearer ${steps.getToken.token}",
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
`steps.<step_name>.<var_name>` in the variable store.

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

### SLEEP special step

Setting `method = "SLEEP"` inserts a step that waits for a specified number of seconds.

```toml
[[requests]]
name   = "wait2s"
method = "SLEEP"
url    = "2"
```

- Specify the wait in seconds via `url` (template variables such as `${vars.delay}` are also allowed).
- `headers` / `body` / `body_form` / `capture` cannot be set (validation error).
- Runtime output:
  ```
  ==> 2026-05-20 01:00:00.000 [wait2s] SLEEP 2
      > sleep 2.0 seconds
  <== 2026-05-20 01:00:02.000 [wait2s] done
  ```

## Template notation

Variable references use the `${...}` form. Escape `$` with `$$`.

```toml
url     = "https://api.${vars.env}.example.com/me"
headers = ["Authorization: Bearer ${steps.getToken.token}"]
body    = '{"price":"$$100"}'   # → {"price":"$100"}
```

Available namespaces:

- `vars.<name>` — variables injected via the CLI's `-v key=value`
- `steps.<step_name>.<capture_key>` — values captured by previous steps

Referencing an undefined variable raises `TemplateError` and stops execution.

## Project layout

```
toml-http-flow/
├── pyproject.toml
├── README.md
├── AGENTS.md
├── docs/
│   └── design.md
├── httpflow/
│   ├── __init__.py
│   ├── __main__.py         # entry point for `python -m httpflow`
│   ├── cli.py              # CLI argument parsing and dispatch
│   ├── config.py           # TOML parsing and dataclasses
│   ├── template.py         # ${...} expansion engine
│   ├── httpclient.py       # urllib HTTP client and JSON path extraction
│   ├── workflow.py         # step execution and variable store
│   ├── generator.py        # workflow → single .py generator
│   └── templates/
│       └── runner.py.tmpl  # template for the generated script
└── tests/
    ├── test_template.py
    ├── test_config.py
    ├── test_httpclient.py
    ├── test_workflow.py
    ├── test_generator.py
    └── test_sleep.py
```

## Development

```bash
# Run tests (standard-library unittest)
python3 -m unittest discover -s tests -v

# Smoke-check the CLI
python3 -m httpflow --help
python3 -m httpflow run --help
python3 -m httpflow generate --help
```

Tests spin up a local mock with the standard-library `http.server` so even
the HTTP round-trip is exercised end-to-end.

## Exit codes

| Code | Meaning |
|------|---------|
| `0`  | All steps succeeded |
| `1`  | TOML parse failure / validation failure / HTTP failure / capture failure, etc. |

## License

[MIT](LICENSE)
