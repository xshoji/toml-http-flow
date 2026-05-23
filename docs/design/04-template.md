# 5. テンプレート記法

Pythonの `string.Template` / シェル / Make などで広く使われている
**`${...}` 記法**を採用する。理由:

- Python標準ライブラリ `string.Template` と同系統で、Python開発者に馴染み深い
- `{...}` 単独だと `str.format` や f-string と紛らわしいが、`${...}` は曖昧さがない
- TOMLのインラインテーブル `{ }` と視覚的に衝突しない
- 先頭の `.` （Goテンプレート由来のクセ）が不要で簡潔
- 区切り文字が明示的なので、文字列中に埋め込んでも境界が分かりやすい

なお、ネストアクセスは `string.Template` 単体ではサポートされないため、
ドット区切りパスは独自に正規表現で実装する。

## 5.1 ステップ結果の参照

```
${steps.<step_name>.<capture_key>}
```

例:
```toml
Authorization = "Bearer ${steps.getToken.token}"
```

## 5.2 CLI引数変数の参照

```
${vars.<variable_name>}
```

例:
```toml
url = "https://api.${vars.env}.example.com/user"
```

## 5.3 repeat変数の参照

```
${repeat.<variable_name>}
```

ワークフロー全体を「実行時に与えた値リストの数だけ」繰り返し実行する
仕組み。`${repeat.X}` を1箇所でも使ったTOMLでは、CLI実行時に
`--repeat-vars "X=v1,v2,v3"` の指定が **必須** になる。
詳細は [05-cli.md §6.1.3](05-cli.md) と
[06-workflow-flow.md](06-workflow-flow.md) を参照。

例:

```toml
[[requests]]
name   = "echo"
method = "GET"
url    = "https://api.example.com/echo?id=${repeat.id}&label=${repeat.label}"
```

```bash
python -m httpflow run -f workflow.toml \
    --repeat-vars "id=1,2,3" \
    --repeat-vars "label=a,b,c"
```

→ `(id=1, label=a)` → `(id=2, label=b)` → `(id=3, label=c)` の3回、
ワークフロー全体が実行される。

## 5.4 ランダム値の参照

```
${random.UUID}
${random.UUID_HEX}
```

レンダリング時に UUID v4 を生成して文字列として展開する。
`${random.UUID}` はハイフン付き、`${random.UUID_HEX}` はハイフンなしの32桁16進文字列を返す。
同じテンプレート文字列内で複数回参照した場合も、参照ごとに新しい UUID を生成する。

例:

```toml
body = '{"request_id":"${random.UUID}"}'
headers = ["X-Request-Id: ${random.UUID_HEX}"]
```

## 5.5 リテラル `$` のエスケープ

`string.Template` の慣例に倣い `$$` で `$` 1文字として扱う。

```toml
body = '{"price":"$$100"}'   # → {"price":"$100"}
```

## 5.6 パス要素で使える文字

`${...}` 内のパス要素には以下を許可する:

- 英数字 `A-Z a-z 0-9`
- アンダースコア `_`
- ハイフン `-` （ステップ名や `-v key=value` の key にハイフンを含めるケースに対応）

ドット `.` はパス区切り（ネスト階層の境界）として扱う。
正規表現で言うと `\{[\w.\-]+\}` がパス全体にマッチする。

例:

```toml
# ステップ名にハイフンを含むケース
url = "https://api.example.com/x?args=${steps.httpbinorg-post.token}"
```

## 5.7 実装方針

`re.sub` のコールバックで置換する。
ネームスペース（`steps` / `vars`）を区別せず、単一のルックアップ関数で解決する。

```python
import re
import uuid
from typing import Any

PATTERN = re.compile(r"\$(?:\$|\{([\w.\-]+)\})")

class TemplateError(KeyError):
    pass

def _lookup(store: dict, parts: list[str]) -> Any:
    if parts == ["random", "UUID"]:
        return uuid.uuid4()
    if parts == ["random", "UUID_HEX"]:
        return uuid.uuid4().hex
    if len(parts) == 1 and parts[0] in store.get("vars", {}):
        return store["vars"][parts[0]]
    cur: Any = store
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            raise TemplateError(".".join(parts))
        cur = cur[p]
    return cur

def render(text: str, store: dict) -> str:
    def repl(m: re.Match) -> str:
        # $$ → $ のエスケープ
        if m.group(0) == "$$":
            return "$"
        path = m.group(1)
        return str(_lookup(store, path.split(".")))
    return PATTERN.sub(repl, text)
```

呼び出し例:

```python
store = {
    "vars": {"env": "production"},
    "steps": {"getToken": {"token": "abc123"}},
}
render("Bearer ${steps.getToken.token}", store)
# → "Bearer abc123"
```
