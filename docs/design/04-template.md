# 4. テンプレート記法

Pythonの `string.Template` / シェル / Make などで広く使われている
**`${...}` 記法**を採用する。理由:

- Python標準ライブラリ `string.Template` と同系統で、Python開発者に馴染み深い
- `{...}` 単独だと `str.format` や f-string と紛らわしいが、`${...}` は曖昧さがない
- TOMLのインラインテーブル `{ }` と視覚的に衝突しない
- 先頭の `.` （Goテンプレート由来のクセ）が不要で簡潔
- 区切り文字が明示的なので、文字列中に埋め込んでも境界が分かりやすい

なお、ネストアクセスは `string.Template` 単体ではサポートされないため、
ドット区切りパスは独自に正規表現で実装する。

## 4.1 ステップ結果の参照

```
${<capture_key>}
${var.<capture_key>}
```

例:
```toml
Authorization = "Bearer ${token}"
```

## 4.2 CLI引数変数の参照

```
${var.<variable_name>}
```

例:
```toml
url = "https://api.${var.env}.example.com/user"
```

## 4.3 環境変数の参照

```
${env.<environment_variable_name>}
```

実行プロセスの環境変数を参照して文字列として展開する。
環境変数が未定義の場合はテンプレートエラーにする。

例:

```toml
headers = ["Authorization: Bearer ${env.API_TOKEN}"]
url = "https://api.example.com/users/${env.USER}"
```

## 4.5 現在時刻の参照

```
${time.DATE_ISO}
${time.DATE_YMD}
${time.DATE_YMDHMS}
```

レンダリング時に現在時刻を取得して文字列として展開する。

| プレースホルダ | 出力例 | 形式 |
|---------------|--------|------|
| `${time.DATE_ISO}` | `2026-06-09T12:34:56.123456+09:00` | ISO 8601（マイクロ秒精度） |
| `${time.DATE_YMD}` | `20260609` | `%Y%m%d` |
| `${time.DATE_YMDHMS}` | `20260609123456` | `%Y%m%d%H%M%S` |

## 4.6 ランダム値の参照

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

## 4.7 リテラル `$` のエスケープ

`string.Template` の慣例に倣い `$$` で `$` 1文字として扱う。

```toml
body = '{"price":"$$100"}'   # → {"price":"$100"}
```

## 4.8 パス要素で使える文字

`${...}` 内のパス要素には以下を許可する:

- 英数字 `A-Z a-z 0-9`
- アンダースコア `_`
- ハイフン `-` （ステップ名や `-v key=value` の key にハイフンを含めるケースに対応）

ドット `.` はパス区切り（ネスト階層の境界）として扱う。
正規表現で言うと `\{[\w.\-]+\}` がパス全体にマッチする。

例:

```toml
# ステップ名にハイフンを含むケース
url = "https://api.example.com/x?args=${token}"
```

## 4.9 実装方針

`re.sub` のコールバックで置換する。
値は `store["vars"]` / `env.*` / `random.*` を単一のルックアップ関数で解決する。

```python
import re
import os
import datetime
import uuid
from typing import Any

PATTERN = re.compile(r"\$(?:\$|\{([\w.\-]+)\})")

class TemplateError(KeyError):
    pass

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
    if parts == ["time", "DATE_ISO"]:
        return datetime.datetime.now().astimezone().isoformat(timespec="microseconds")
    if parts == ["time", "DATE_YMD"]:
        return datetime.datetime.now().astimezone().strftime("%Y%m%d")
    if parts == ["time", "DATE_YMDHMS"]:
        return datetime.datetime.now().astimezone().strftime("%Y%m%d%H%M%S")
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
    "vars": {"env": "production", "token": "abc123"},
}
render("Bearer ${token}", store)
# → "Bearer abc123"
```
