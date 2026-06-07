# toml-http-flow (`httpflow`)

A CLI tool that runs a workflow of HTTP requests defined in TOML, in order.
Implemented using **only the Python 3.11+ standard library** — zero external
dependencies.

It also ships a `generate` subcommand that emits a **single self-contained
Python script** from a workflow TOML — useful for archiving, distribution,
and embedding into CI/CD pipelines without this tool installed.

## Features

- **Request chaining** — describe a workflow as one request per block (`[[requests]]`) in TOML
- **Value passing** — capture a value from a response and reference it in later steps via `${name}` or `${var.name}`
- **External injection** — inject variables with `-v key=value` (referenced as `${var.<name>}`)
- **JSON path extraction** — extract values from JSON responses using `data.user.id` / `items[0].id` style paths
- **Wait steps** — built-in `SLEEP` step lets you insert a wait of N seconds
- **Zero dependencies** — implemented purely on the standard library (`tomllib`, `urllib`, `json`, `argparse`)
- **Generate standalone scripts** — `generate` subcommand produces a single .py file that runs anywhere without installing this tool

## Why httpflow?

| Tool | Difficulty of chaining | CI/CD friendly | Self-contained output |
|------|------------------------|----------------|-----------------------|
| curl + shell | Values must be parsed by hand (`jq`, `grep`, etc.) and passed via shell variables | Possible but fragile | No |
| Postman / Insomnia | GUI-first; collection runner requires the app | Newman exists but JSON definitions are verbose | No |
| Newman | JSON is verbose for simple workflows | Yes | No |
| **httpflow** | TOML-native value passing with `${...}` | Yes — single TOML file + CLI | **Yes — generate a single .py** |

If you want a **declarative, version-control-friendly HTTP workflow** that fits
neatly into scripts and CI, httpflow is designed for that.

## Requirements

- Python 3.11 or newer (because `tomllib` ships in the standard library)

## Quick start

```bash
# Run directly from GitHub with pipx
pipx run --spec git+https://github.com/xshoji/toml-http-flow.git httpflow --help

# Or clone and run
python3 -m httpflow --help
```

### 1. Write a workflow

```toml
# workflow.toml

[[requests]]
name    = "getToken"
method  = "POST"
url     = "https://api.example.com/auth"
headers = ["Content-Type: application/json"]
body    = '{"user":"test","pass":"secret"}'
capture = ["token = access_token"]

[[requests]]
name    = "getUser"
method  = "GET"
url     = "https://api.example.com/me"
headers = ["Authorization: Bearer ${token}"]
capture = ["user_id = data.user.id"]
```

### 2. Run it

```bash
python3 -m httpflow -f workflow.toml
```

### 3. Generate a standalone script

```bash
python3 -m httpflow generate -f workflow.toml -o workflow.py
python3 workflow.py
```

## Usage

```bash
# Run a workflow (run can be omitted)
python3 -m httpflow run -f workflow.toml
python3 -m httpflow -f workflow.toml

# Inject variables
python3 -m httpflow run -f workflow.toml -v env=production -v user_id=123

# Quiet mode, pretty JSON, no masking
python3 -m httpflow run -f workflow.toml -q --pretty-json --no-mask

# Generate a self-contained script
python3 -m httpflow generate -f workflow.toml -o workflow.py
python3 -m httpflow generate -f workflow.toml -o workflow.py --shebang
```

For full specification (TOML fields, template notation, capture sources,
`until` polling, exit codes, development, etc.), see [**docs/spec.md**](docs/spec.md).

## License

[MIT](LICENSE)
