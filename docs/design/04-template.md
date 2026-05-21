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

## 5.3 リテラル `$` のエスケープ

`string.Template` の慣例に倣い `$$` で `$` 1文字として扱う。

```toml
body = '{"price":"$$100"}'   # → {"price":"$100"}
```

## 5.4 パス要素で使える文字

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

## 5.5 実装方針

`re.sub` のコールバックで置換する。
ネームスペース（`steps` / `vars`）を区別せず、単一のルックアップ関数で解決する。

```python
import re
from typing import Any

PATTERN = re.compile(r"\$(?:\$|\{([\w.\-]+)\})")

class TemplateError(KeyError):
    pass

def _lookup(store: dict, parts: list[str]) -> Any:
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
