# AGENTS.md

Project-specific instructions for AI agents.
Read this before changing any code.

## Project overview

- Name: `httpflow` (package / CLI name) / `toml-http-flow` (repository)
- Type: CLI tool
- Purpose: run an HTTP workflow defined in TOML in order / emit a single .py script
- Specification: each file under [docs/design/](docs/design/) and [docs/design.md](docs/design.md) is the **single source of truth**. Update it first whenever the spec changes.

## Absolute requirements

1. **Zero dependencies**: the main code, tests, and generated scripts must all be implemented using **only the Python 3.11+ standard library**.
   Do not add `requests` / `pydantic` / `pytest` / `httpx` and similar packages.
2. **Python 3.11+ required**: `tomllib` being bundled in the standard library is a prerequisite. Do not depend on backports such as `tomli`.
3. **Self-contained generated script**: the .py file produced from `httpflow/templates/runner.py.tmpl` must work standalone without importing the `httpflow` package.
4. **Stay in sync with the design doc**: the behavior in `docs/design.md` and the implementation must match.
   When changing the spec, update the design doc → implementation → tests in that order.

## Directory responsibilities

| Path | Responsibility | Notes when editing |
|------|----------------|--------------------|
| [httpflow/config.py](httpflow/config.py) | TOML → `WorkflowSpec` conversion / validation | If you change `parse_kv_list` behavior, also update design doc §4.4 |
| [httpflow/template.py](httpflow/template.py) | `${...}` expansion / `$$` escaping | Keep the `PATTERN` regex aligned with the generator's equivalent |
| [httpflow/cli.py](httpflow/cli.py) | `argparse` dispatch | Preserve backward compatibility: when `run` is omitted, treat it as `run` |
| [httpflow/generator.py](httpflow/generator.py) | TOML → single .py generator | The output must always pass `compile()` syntax validation |
| [httpflow/model.py](httpflow/model.py) | Normalised workflow models (`WorkflowSpec`, `HttpStep`, `SleepStep`, etc.) | Kept free of runtime helpers to avoid circular deps |
| [httpflow/runner.py](httpflow/runner.py) | Step execution engine / variable store / repeat iteration | Same `collect_*` logic used by the generator; keep them in sync |
| [httpflow/runtime/](httpflow/runtime/) | Shared helpers used by both the package and generated scripts (`core`, `http`, `mask`, `until`, `repeat`) | When fixing logic here, always fix the generator template too |
| [httpflow/templates/runner.py.tmpl](httpflow/templates/runner.py.tmpl) | Base template for the generated script | Replace only the placeholders `{{STEP_FUNCTIONS}}` `{{STEP_CALLS}}` `{{DEFAULT_VARS}}` `{{VERSION}}` `{{GENERATED_AT}}` `{{UNTIL_HELPERS}}` `{{MAIN_REPEAT_SETUP}}` |
| [tests/](tests/) | `unittest`-based tests | Follow the convention of standing up a local mock with `http.server` |

## Project layout

```
toml-http-flow/
├── pyproject.toml
├── README.md
├── AGENTS.md
├── docs/
│   ├── spec.md
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

## On the duplicated runtime helpers

Functions equivalent to `render` / `extract` / `do_request` exist in **both
the main package and the generator template** (a deliberate design choice
that prioritizes self-containedness over DRY).

When you fix one side, always fix the other, and use the tests
(`tests/test_generator.py`) to guarantee logical equivalence.

## Tests

```bash
# Run all tests (use the standard library's unittest discover)
python3 -m unittest discover -s tests -v
```

- Add a corresponding test whenever you add a feature
- Tests that perform HTTP must not hit external APIs — stand up a local mock with `http.server.HTTPServer`
- Tests for the generated script should run `python3 generated.py` via `subprocess` to verify the actual behavior

## Smoke checks

After making changes, verify at minimum the following:

```bash
# 1. Tests
python3 -m unittest discover -s tests >/tmp/amp-test.log 2>&1 && echo OK || tail /tmp/amp-test.log

# 2. CLI help is not broken
python3 -m httpflow --help
python3 -m httpflow run --help
python3 -m httpflow generate --help

# 3. `generate` outputs syntactically valid .py
python3 -m httpflow generate -f <some.toml> -o /tmp/g.py
python3 -c "import py_compile; py_compile.compile('/tmp/g.py', doraise=True)"
```

## Coding conventions

- Type hints are mandatory (use `from __future__ import annotations`)
- Use `@dataclass` aggressively
- Add a one-line docstring to every public function
- Exception messages must be concise English and include the cause and the target
- Use `print(..., file=sys.stderr)` to separate error output
- Prefer `pathlib.Path` for paths

## Things you must not do

- Add external libraries (keep `pyproject.toml`'s `dependencies` empty)
- Import `tomli` / `requests` / `httpx` and the like
- Migrate to `pytest` (we are locked to `unittest`)
- Emit a generated script that imports the `httpflow` package
- Change the public spec (CLI arguments / TOML fields / template notation) without updating the design doc
- Silently add fields that are not in the design doc (when extending, align with §12 "Extension points")

## Decision criteria for extensions

When you want to add a new feature, consult the list in
[docs/design.md §12 Extension points](docs/design.md).
If your item is not there, either split it into a separate PR that updates
the design doc, or — even inside the same PR — write the design-doc section
first.

## Commit policy

- One commit = one logical change
- Messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:` / `fix:` / `refactor:` / `docs:` / `test:` / `chore:`, etc.)
- Do not run `git commit` / `git push` on your own unless the user explicitly asks for it
