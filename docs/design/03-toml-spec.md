# 4. TOML仕様

## 4.1 設計方針

TOMLの素直な使い方（`[requests.headers]` などのサブテーブル）だと、1リクエストが複数ブロックに分割されてしまい「どこからどこまでが1リクエストか」が一目で分からない。

そこで、**1リクエスト = 1つの `[[requests]]` ブロックに収める**ことを最優先とし、ネストする `headers` / `body_form` / `capture` はすべて **配列形式の文字列リスト** で記述する方式を採用する。

- HTTP / curl と同じ「`Key: Value`」「`key=value`」記法なので親しみやすい
- 配列リテラル `[ ... ]` はTOML 1.0でも複数行展開・末尾カンマが許可されているため、項目数が増えても読みやすい
- インラインテーブル `{ ... }` と違って改行できるため、長くなっても破綻しない
- 1ブロックの中に全情報が完結し、視認性が高い

## 4.2 サンプル

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
name    = "getUser"
method  = "GET"
url     = "https://api.example.com/me"
headers = [
    "Authorization: Bearer ${steps.getToken.token}",
    "Accept: application/json",
]


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

## 4.3 フィールド定義

| フィールド名 | 必須 | 型             | 説明                                                                 |
|--------------|------|----------------|----------------------------------------------------------------------|
| name         | ○    | string         | ステップ名（変数参照に使用）                                         |
| description  | -    | string         | このステップの意図・補足。`==>` 行の直後に `# ...` として出力される  |
| method       | ○    | string         | HTTPメソッド（GET/POST/PUT/DELETE）または特殊メソッド（SLEEP）       |
| url          | ○    | string         | リクエストURL、または特殊メソッドのパラメータ（例：SLEEP の秒数）   |
| headers      | -    | array[string]  | `"Key: Value"` 形式の文字列リスト                                    |
| body         | -    | string         | 生テキストボディ（複数行リテラル `'''...'''` 推奨。`body_form`と排他）|
| body_form    | -    | array[string]  | `"key = value"` 形式の文字列リスト（`body`と排他）                   |
| capture      | -    | array[string]  | `"var_name = json.path"` 形式の文字列リスト                          |
| until        | -    | array[string]  | ポーリング設定（§4.5）。条件を満たすまでリクエストを繰り返す         |

## 4.4 パース規則

`headers` / `body_form` / `capture` の各要素は、Python側でパースして dict に変換する。

| フィールド   | 区切り文字 | 分割回数 | 例                                | 結果                                  |
|--------------|------------|----------|-----------------------------------|---------------------------------------|
| headers      | 最初の `:` | 1回      | `"Authorization: Bearer abc"`     | `{"Authorization": "Bearer abc"}`     |
| body_form    | 最初の `=` | 1回      | `"email = test@example.com"`      | `{"email": "test@example.com"}`       |
| capture      | 最初の `=` | 1回      | `"token = data.access_token"`     | `{"token": "data.access_token"}`      |

- 区切り文字の左右の空白は自動でトリムする（`"a = b"` も `"a=b"` も同じ）
- 値側に区切り文字を含めたい場合も、最初の1つだけが区切りとして扱われる
  例: `"X-Url: https://example.com:8080/path"` → key=`X-Url`, value=`https://example.com:8080/path`
- 区切り文字が無い行は `ValueError` でエラー

```python
def parse_kv_list(items: list[str], sep: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in items:
        if sep not in raw:
            raise ValueError(f"invalid entry (missing '{sep}'): {raw!r}")
        k, v = raw.split(sep, 1)
        result[k.strip()] = v.strip()
    return result
```

## 4.5 capture の意味とパス記法

`capture` の各要素は `"<変数名> = <JSONパス>"` 形式で、
**レスポンスJSONの「JSONパス」位置にある値を、「変数名」として変数ストアに保存する** という指示。

保存先は `steps.<step_name>.<変数名>` で、後続ステップから `${steps.<step_name>.<変数名>}` で参照できる。

### 4.5.1 トップレベルフィールドの抽出

レスポンス:
```json
{ "access_token": "xxxx", "expires_in": 3600 }
```

TOML:
```toml
capture = [
    "token   = access_token",
    "expires = expires_in",
]
```

結果（変数ストア）:
```python
steps.<step>.token   == "xxxx"
steps.<step>.expires == 3600
```

### 4.5.2 ネストオブジェクトの抽出（ドット区切り）

入れ子になったオブジェクトは **ドット区切り** で辿る。

レスポンス:
```json
{
  "data": {
    "user": {
      "id": 42,
      "profile": { "email": "a@example.com" }
    }
  }
}
```

TOML:
```toml
capture = [
    "user_id = data.user.id",
    "email   = data.user.profile.email",
]
```

### 4.5.3 配列要素の抽出（インデックス）

配列は `[N]` でインデックス指定する。

レスポンス:
```json
{ "items": [ {"id": "a1"}, {"id": "a2"} ] }
```

TOML:
```toml
capture = [
    "first_id  = items[0].id",
    "second_id = items[1].id",
]
```

### 4.5.4 パス解決アルゴリズム

```python
import re
from typing import Any

PATH_TOKEN = re.compile(r"([^\.\[\]]+)|\[(\d+)\]")

def extract(body: Any, path: str) -> Any:
    cur: Any = body
    for name, idx in PATH_TOKEN.findall(path):
        if name:
            if not isinstance(cur, dict) or name not in cur:
                raise KeyError(f"path not found: {path}")
            cur = cur[name]
        else:
            i = int(idx)
            if not isinstance(cur, list) or i >= len(cur):
                raise IndexError(f"index out of range: {path}")
            cur = cur[i]
    return cur
```

### 4.5.5 抽出失敗時の挙動

- 指定パスが存在しない → エラーで停止（後続ステップは実行しない）
- レスポンスがJSONとしてパース不能だが `capture` が指定されている → エラー
- `capture` 指定なしでJSONパース失敗 → 警告ログのみで継続

## 4.6 TOML特有の注意点

- 配列テーブル `[[requests]]` で順序保証されたリストとして定義
- 配列リテラル内で改行・末尾カンマが使えるため、項目数が多くても1ブロックを崩さず書ける
- ヘッダー名にハイフンが含まれても、文字列の中にあるためクォート不要

## 4.7 特殊ステップ `SLEEP`

`method = "SLEEP"` とすることで、指定秒数の待機（`time.sleep`）を行うステップを挿入できる。

```toml
[[requests]]
name   = "wait"
method = "SLEEP"
url    = "5"
```

- `url` に待機秒数を指定する（テンプレート変数も使用可）。
- `headers` / `body` / `body_form` / `capture` は指定不可（バリデーションエラー）。
- 出力: `==> [name] SLEEP 5` → `    > sleep 5.0 seconds` → `<== [name] done`

## 4.8 ポーリング（`until` フィールド）

「あるリソースのステータスが Active になるまで GET し続ける」ような
**条件成立までの繰り返し** を1ステップ内で完結させる。

```toml
[[requests]]
name    = "pollStatus"
method  = "GET"
url     = "https://api.example.com/jobs/${steps.createJob.id}"
capture = ["status = data.status"]
until = [
    "condition    = ${steps.pollStatus.status} == Active",
    "interval     = 2.0",     # 試行間の待機秒数（省略時 1.0）
    "max_attempts = 30",      # 最大試行回数（省略時 10）
]
```

### 4.8.1 動作

1. リクエスト送信（最初の試行は通常通り）
2. `capture` を評価して変数ストアを更新
3. `condition` をテンプレート展開 → 評価
4. 真なら次のステップへ。偽なら `interval` 秒待ってから 1. に戻る
5. `max_attempts` を超えても真にならなければ `RuntimeError` で失敗

- HTTP エラー（4xx/5xx）が発生した場合は **即失敗** とする（リトライしない）。
- 各試行ごとに通常の request/response ログを出力し、最後に
  `* until satisfied on attempt N` または
  `* until not satisfied (attempt N/M), retrying in Xs` を出力する。
- `until` は SLEEP ステップでは指定できない。

### 4.8.2 `until` の各キー

| キー         | 必須 | 型     | デフォルト | 説明                                       |
|--------------|------|--------|------------|--------------------------------------------|
| condition    | ○    | string | —          | 真偽を判定する式（§4.8.3）                 |
| interval     | -    | float  | `1.0`      | 試行間の待機秒数（0 以上）                 |
| max_attempts | -    | int    | `10`       | 最大試行回数（1 以上）                     |

`until` は `parse_kv_list(..., "=")` で dict にパースする。
未知のキーはバリデーションエラー。

### 4.8.3 condition の式言語

`<LHS> <演算子> <RHS>` 形式のシンプルな比較式のみをサポートする。
LHS / RHS は両方ともテンプレート展開後に文字列として評価される。

| 演算子 | 例                                                  | 意味                                   |
|--------|-----------------------------------------------------|----------------------------------------|
| `==`   | `${steps.x.status} == Active`                       | 文字列が一致                           |
| `!=`   | `${steps.x.status} != Pending`                      | 文字列が不一致                         |
| `~`    | `${steps.x.message} ~ /success/i`                   | `/pattern/[flags]` で正規表現マッチ    |
| `in`   | `${steps.x.code} in [200, 201, 204]`                | カンマ区切りリストに含まれる           |

- 演算子は LHS に最も近いものから探索する（典型的に LHS は `${...}` で
  これらの演算子を含まないので曖昧さは生じない）。
- `~` の RHS は `/pattern/flags` 形式。`flags` は `i` / `m` / `s` の組み合わせ。
- `in` の RHS は `[A, B, C]` 形式。各要素は空白トリム後に文字列比較。
- 真偽以外の判定（`>`, `<` 等の数値比較）は将来拡張（[11-extension-points.md](11-extension-points.md)）。
