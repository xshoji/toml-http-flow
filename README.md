# toml-http-flow (`httpflow`)

`httpflow` is a small CLI for **HTTP workflow automation**.
Write ordered HTTP steps in TOML, capture values from one response, reuse them
in later requests, wait or poll when needed, and optionally export the whole
workflow as a single standalone script.

It is implemented using **only the Python 3.11+ standard library** — zero
external dependencies.

It also ships a `generate` subcommand that emits a **single self-contained
Python script** from a workflow TOML — useful for archiving, distribution,
and embedding into CI/CD pipelines without this tool installed.


## What it looks like

```bash
# 1. Define workflow
$ cat demo.toml
[[requests]]
description = "GET request to httpbingo.org/get"
name    = "httpbinorg-get"
method  = "GET"
url     = "https://httpbingo.org/get?uuid=${random.UUID}&cliParameter=${var.query1}"
capture = [
  "responseBodyUuid      = args.uuid[0]",
  "responseHeaderServer  = response.header.server",
]

[[requests]]
description = "POST request to httpbingo.org/post"
name    = "httpbinorg-post"
method  = "POST"
url     = "https://httpbingo.org/post"
headers = ["Content-Type: application/json"]
body    = '''
{
  "uuid": "${responseBodyUuid}",
  "server": "${responseHeaderServer}"
}
'''

# 2. Run workflow
$ python3 -m httpflow run -f demo.toml --var "query1=test-value"
==> 2026-06-12 02:47:30.398 [httpbinorg-get] GET https://httpbingo.org/get?uuid=5c135e93-abc0-42c6-bbb1-7fc13c5f7529&cliParameter=test-value
  # GET request to httpbingo.org/get
  > GET /get?uuid=5c135e93-abc0-42c6-bbb1-7fc13c5f7529&cliParameter=test-value HTTP/1.1
  > Host: httpbingo.org
  > User-Agent: Python-urllib/3.12
  > Accept-Encoding: identity
<== 2026-06-12 02:47:30.570 [httpbinorg-get]
  < HTTP/1.1 200 OK
  < access-control-allow-credentials: true
...
  <
  < {"args": {"cliParameter": ["test-value"], "uuid": ["5c135e93-abc0-42c6-bbb1-7fc13c5f7529"]}, ...
  * capture responseBodyUuid = '5c135e93-abc0-42c6-bbb1-7fc13c5f7529'
  * capture responseHeaderServer = 'Fly/02244e829 (2026-06-11)'
==> 2026-06-12 02:47:30.571 [httpbinorg-post] POST https://httpbingo.org/post
  # POST request to httpbingo.org/post
  > POST /post HTTP/1.1
  > Host: httpbingo.org
  > Content-Type: application/json
...
  >
  > {"uuid": "5c135e93-abc0-42c6-bbb1-7fc13c5f7529", "server": "Fly/02244e829 (2026-06-11)"}
<== 2026-06-12 02:47:30.721 [httpbinorg-post]
  < HTTP/1.1 200 OK
  < access-control-allow-credentials: true
  < access-control-allow-origin: *
...
```


## Features

- **Request chaining** — describe a workflow as one request per block (`[[requests]]`) in TOML
- **Value passing** — capture a value from a response and reference it in later steps via `${name}` or `${var.name}`
- **Per-request dynamic values** — generate values such as `${random.UUID}`, `${random.UUID_HEX}`, and `${time.DATE_ISO}` when each step runs, without shell glue
- **Environment and injected values** — use `${env.USER}` and `-v key=value` / `${var.<name>}` for environment-specific workflows
- **JSON path extraction** — extract values from JSON responses using `data.user.id` / `items[0].id` style paths
- **Wait steps** — built-in `SLEEP` step lets you insert a wait of N seconds
- **Polling with `until`** — retry a step until a response-derived condition is satisfied
- **Multiple body modes** — JSON/text body, form body, raw file upload, and multipart form-data
- **Zero dependencies** — implemented purely on the standard library (`tomllib`, `urllib`, `json`, `argparse`)
- **Generate standalone scripts** — `generate` subcommand produces a single `.py` file that runs anywhere without installing this tool
- **Generate shell scripts** — emit a standalone Bash/curl script when that is easier to hand off

## Why httpflow?

httpflow is not trying to replace every HTTP client or API test tool. Its sweet
spot is **repeatable HTTP workflows** that should stay compact, structured, and
portable: describe the flow in TOML, generate fresh values at each request,
include non-HTTP wait steps, and package the result for CI or operational
handoff.

| Tool | Best at | Where httpflow is different |
|------|---------|-----------------------------|
| curl + shell | One-off requests and ad hoc scripts | Captures, variables, retries, masking, and body modes are built into the workflow file instead of hand-written shell glue |
| Hurl | HTTP request/response assertions and retryable API tests | httpflow emphasizes TOML-structured workflows, per-request random/time values without wrapper scripts, explicit non-HTTP wait steps, and standalone Python/Bash generation |
| Postman / Insomnia | GUI exploration and team collections | httpflow is text-first, small, dependency-free, and easy to review in Git |
| Newman | Running Postman collections in CI | httpflow uses compact TOML and can generate a single script for environments where the CLI is not installed |

If you want a **declarative, version-control-friendly HTTP workflow** that fits
neatly into scripts, CI, and runbooks, httpflow is designed for that.

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
