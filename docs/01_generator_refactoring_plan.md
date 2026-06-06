# Generator runtime refactoring plan
  
## Goal
  
Reduce generated script size and improve readability without reintroducing runtime double-maintenance.
  
The target architecture is:
  
- split generated/runtime helpers into shared importable modules under `httpflow/runtime/`
- use those modules from package execution code
- during `generate`, inspect the parsed workflow and flatten only the required runtime modules into the standalone script
- keep generated scripts self-contained, stdlib-only, and free of `httpflow` imports
  
## Current issues
  
Current generation embeds the monolithic `httpflow/embedded_runtime.py` source into every generated script. Even when a workflow does not use `until`, the generated script can still include generic helpers for those features.
  
Relevant files:
  
- `httpflow/embedded_runtime.py`: monolithic helper implementation
- `httpflow/generator.py`: reads and embeds `embedded_runtime.py`
- `httpflow/templates/runner.py.tmpl`: has the runtime placeholder
- package wrappers importing embedded helpers:
  - `httpflow/template.py`
  - `httpflow/httpclient.py`
  - `httpflow/masking.py`
  - `httpflow/until.py`
  - `httpflow/runner.py`
  - `httpflow/cli.py`
  
## Target module layout
  
Create shared runtime modules:
  
```text
httpflow/runtime/
├── __init__.py
├── core.py
├── mask.py
├── http.py
└── until.py
```
  
Suggested split:
  
| Module | Contents |
|---|---|
| `runtime/core.py` | `PATTERN`, `TemplateError`, `_lookup`, `render`, `render_mapping` |
| `runtime/mask.py` | masking constants, `mask`, `mask_url`, `mask_value` |
| `runtime/http.py` | `PATH_TOKEN`, `extract`, `do_request`, logging helpers, `run_step` |
| `runtime/until.py` | `eval_until`, `poll_until`, until condition constants/helpers |
  
Keep `httpflow/embedded_runtime.py` temporarily as a compatibility re-export shim. The generator should stop using it as the embedding source.
  
## Generator design
  
### Feature detection
  
In `httpflow/generator.py`, detect required runtime features from `WorkflowSpec`.
  
Initial coarse feature set:
  
- any step exists → `http`
  - current `run_step` handles both HTTP and `SLEEP`
  - `http` depends on `core` and `mask`
- any HTTP step has `until` → `until`
  
Avoid micro-feature splitting in the first pass. Do not initially split by capture, form bodies, pretty JSON, masking, env vars, or random UUID.
  
### Runtime dependency manifest
  
Use an explicit manifest instead of clever tree-shaking:
  
```python
_RUNTIME_DEPS = {
    "core": (),
    "mask": (),
    "http": ("core", "mask"),
    "until": ("core",),
}
```
  
Resolve dependencies in deterministic order:
  
```text
core → mask → http → until
```
  
### Flattening source
  
The generator should read selected `httpflow/runtime/*.py` files and concatenate them into the generated script.
  
Strip only controlled package-only lines:
  
- `from __future__ import annotations`
- relative runtime imports such as `from .core import render`
  
Do not start with AST rewriting or import merging. Duplicate stdlib imports are acceptable for the first implementation because they are safer than fragile source transformation.
  
Generated script must not contain:
  
- `import httpflow`
- `from httpflow ...`
- `from . ...`
  
### Template update
  
Rename the runtime placeholder in `httpflow/templates/runner.py.tmpl`:
  
```text
{{EMBEDDED_RUNTIME}} → {{RUNTIME_HELPERS}}
```
  
Update the section label to something like:
  
```python
# ─── selected runtime helpers (inlined; no httpflow dependency) ─────────
```
  
`{{UNTIL_HELPERS}}` can remain as a comment/glue placeholder for now, but the actual `poll_until` implementation should come from selected `runtime/until.py` only when needed.
  
## Implementation phases
  
### Phase 0: design docs
  
Update the spec/design documentation before code changes, because project rules treat design docs as the source of truth.
  
Likely docs to update:
  
- `docs/design/07-script-generation.md`
  - replace “embed `embedded_runtime.py` source” with “flatten selected runtime modules”
  - document feature selection and self-contained output rules
- `docs/design.md`
  - keep the top-level generated-script description in sync if it mentions `embedded_runtime.py`
- optionally architecture/testing docs if they reference the old model
  
### Phase 1: split runtime modules without changing behavior
  
1. Create `httpflow/runtime/` modules.
2. Move code from `embedded_runtime.py` into the modules above.
3. Replace `embedded_runtime.py` with a re-export shim.
4. Update package imports to use `httpflow.runtime.*` directly.
5. Keep behavior and generated output effectively unchanged at this phase.
6. Run the full test suite.
  
Suggested import changes:
  
- `template.py` → `from .runtime.core import ...`
- `httpclient.py` → `from .runtime.http import do_request, extract`
- `until.py` → `from .runtime.until import eval_until`
- `runner.py` → `from .runtime.http import run_step`, `from .runtime.until import poll_until`
- `masking.py` → delegate/re-export from `runtime.mask`
  
### Phase 2: generator flattening
  
1. Replace `_EMBEDDED_RUNTIME_PATH` with `_RUNTIME_DIR`.
2. Add feature detection, dependency resolution, source stripping, and module flattening helpers.
3. Build runtime helpers after until detection is known.
4. Replace `{{RUNTIME_HELPERS}}` in the template.
5. Ensure generated scripts compile.
6. Ensure generated scripts contain no package or relative imports.
  
### Phase 3: tests
  
Add string-level selection tests. Avoid exact line-count or byte-size assertions.
  
Recommended cases:
  
1. Workflow without `until` omits:
   - `def eval_until`
   - `def poll_until`
   - `_UNTIL_OPS`
2. Workflow with `until` includes those helpers.
3. Generated script never contains:
   - `import httpflow`
   - `from httpflow`
   - `from .`
4. Empty or minimal workflow still compiles.
5. Existing generated-script execution tests still pass from outside the repository.
  
### Phase 4: optional readability cleanup
  
Do this only after the feature-based flattening is green.
  
Possible follow-ups:
  
- generate direct `SLEEP` step code instead of using `run_step(method="SLEEP")`
- split `runtime/sleep.py` or inline sleep-only code for sleep-only workflows
- strip type annotations from generated runtime output
- merge duplicate stdlib imports in generated output
  
These are secondary. The main size win comes from omitting unused `until` modules.
  
## Validation commands
  
Full tests:
  
```bash
python3 -m unittest discover -s tests >/tmp/amp-test.log 2>&1 && echo OK || tail -n 120 /tmp/amp-test.log
```
  
CLI smoke checks:
  
```bash
python3 -m httpflow --help
python3 -m httpflow run --help
python3 -m httpflow generate --help
```
  
Manual no-until/no-repeat check:
  
```bash
cat >/tmp/httpflow_no_until.toml <<'TOML'
[[requests]]
name = "ping"
method = "GET"
url = "http://127.0.0.1:1/ping"
TOML

python3 -m httpflow generate -f /tmp/httpflow_no_until.toml -o /tmp/no_until.py
python3 -m py_compile /tmp/no_until.py
! grep -E 'from httpflow|import httpflow|from \.|def eval_until|_UNTIL_OPS|def poll_until' /tmp/no_until.py
```
  
Manual until check:
  
```bash
cat >/tmp/httpflow_until.toml <<'TOML'
[[requests]]
name = "poll"
method = "GET"
url = "http://127.0.0.1:1/poll"

[requests.until]
condition = "${status} == Active"
interval = 0
max_attempts = 1
TOML

python3 -m httpflow generate -f /tmp/httpflow_until.toml -o /tmp/until.py
python3 -m py_compile /tmp/until.py
grep -E 'def eval_until|def poll_until|_UNTIL_OPS' /tmp/until.py
! grep -E 'from httpflow|import httpflow|from \.' /tmp/until.py
```

## Risks and guardrails
  
| Risk | Guardrail |
|---|---|
| Flattened script leaks `httpflow` imports | Strip relative imports and test for no package/relative imports |
| Runtime feature dependencies are incomplete | Use explicit dependency manifest and compile generated scripts for each feature combination |
| Behavior changes during module move | Phase 1 should be mostly move/re-export only; keep
