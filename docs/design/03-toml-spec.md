# 3. TOML仕様

## 3.1 設計方針

TOMLの素直な使い方（`[requests.headers]` などのサブテーブル）だと、1リクエストが複数ブロックに分割されてしまい「どこからどこまでが1リクエストか」が一目で分からない。

そこで、**1リクエスト = 1つの `[[requests]]` ブロックに収める**ことを最優先とし、ネストする `headers` / `body_form` / `body_multipart` / `capture` はすべて **配列形式の文字列リスト** で記述する方式を採用する。

- HTTP / curl と同じ「`Key: Value`」「`key=value`」記法なので親しみやすい
- 配列リテラル `[ ... ]` はTOML 1.0でも複数行展開・末尾カンマが許可されているため、項目数が増えても読みやすい
- インラインテーブル `{ ... }` と違って改行できるため、長くなっても破綻しない
- 1ブロックの中に全情報が完結し、視認性が高い

## 3.2 サンプル

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
    "Authorization: Bearer ${token}",
    "Accept: application/json",
]


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


[[requests]]
name = "uploadRaw"
method = "PUT"
url = "https://api.example.com/files/${var.file_id}"
body_file = "./data/archive.bin"


[[requests]]
name = "uploadMultipart"
method = "POST"
url = "https://api.example.com/upload"
body_multipart = [
    "title = test upload",
    "file = @./data/report.pdf; filename=report.pdf; type=application/pdf",
]
```

## 3.3 フィールド定義

| フィールド名 | 必須 | 型             | 説明                                                                 |
|--------------|------|----------------|----------------------------------------------------------------------|
| name         | ○    | string         | ステップ名（変数参照に使用）                                         |
| description  | -    | string         | このステップの意図・補足。`==>` 行の直後に `# ...` として出力される  |
| method       | ○    | string         | HTTPメソッド（GET/POST/PUT/DELETE）または特殊メソッド（SLEEP）       |
| url          | ○    | string         | リクエストURL、または特殊メソッドのパラメータ（例：SLEEP の秒数）   |
| headers      | -    | array[string]  | `"Key: Value"` 形式の文字列リスト                                    |
| body         | -    | string         | 生テキストボディ（複数行リテラル `'''...'''` 推奨。他 body mode と排他）|
| body_form    | -    | array[string]  | `"key = value"` 形式の文字列リスト。他 body mode と排他              |
| body_file    | -    | string         | ファイル内容をそのまま request body として送信する。`Content-Type` 未指定時は `application/octet-stream`。他 body mode と排他 |
| body_multipart | -  | array[string]  | `multipart/form-data` body。通常フィールドは `"key = value"`、ファイルフィールドは `"key = @path; filename=...; type=..."`。他 body mode と排他 |
| capture      | -    | array[string]  | `"var_name = source"` 形式の文字列リスト（`source` の記法は §3.5）   |
| until        | -    | array[string]  | ポーリング設定（§3.8）。条件を満たすまでリクエストを繰り返す         |

## 3.4 パース規則

`headers` / `body_form` / `body_multipart` / `capture` の各要素は、Python側でパースする。

| フィールド   | 区切り文字 | 分割回数 | 例                                | 結果                                  |
|--------------|------------|----------|-----------------------------------|---------------------------------------|
| headers      | 最初の `:` | 1回      | `"Authorization: Bearer abc"`     | `{"Authorization": "Bearer abc"}`     |
| body_form    | 最初の `=` | 1回      | `"email = test@example.com"`      | `{"email": "test@example.com"}`       |
| body_multipart | 最初の `=` | 1回   | `"file = @./a.bin; filename=a.bin; type=application/octet-stream"` | 順序付き multipart part |
| capture      | 最初の `=` | 1回      | `"token = data.access_token"`     | `{"token": "data.access_token"}`      |

- 区切り文字の左右の空白は自動でトリムする（`"a = b"` も `"a=b"` も同じ）
- 値側に区切り文字を含めたい場合も、最初の1つだけが区切りとして扱われる
  例: `"X-Url: https://example.com:8080/path"` → key=`X-Url`, value=`https://example.com:8080/path`
- 区切り文字が無い行は `ValueError` でエラー
- `body_file` のファイルパスはテンプレート展開対象。ファイル内容は展開せず bytes として送信する。
- `body_multipart` の値が `@` で始まる場合はファイルフィールド。`filename` 省略時はファイル名、`type` 省略時は `application/octet-stream`。値を `@` で始めたい通常フィールドは `@@value` と書く。
- `body_multipart` は boundary 付き `Content-Type` を自動生成するため、`headers` に `Content-Type` を指定すると実行時エラーになる。
- ファイル読み込みと multipart 組み立ては実行時に行われ、相対パスは実行時の current working directory 基準。大きなファイルはメモリに載る。

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

## 3.5 capture の意味とパス記法

`capture` の各要素は `"<変数名> = <source>"` 形式で、
**`<source>` が指す値を、「変数名」として変数ストアに保存する** という指示。

保存先はトップレベル変数 `<変数名>` のみで、後続ステップから `${<変数名>}` で参照できる。
変数名が重複した場合は、後から capture した値で上書きする。

`<source>` は名前空間プレフィックスで保存元を切り替える。プレフィックスが無い場合は
**従来通りレスポンスボディのJSONパス**として解釈する（後方互換）。

| `<source>` の記法            | 保存元                                            | 例                                       |
|------------------------------|---------------------------------------------------|------------------------------------------|
| `<json.path>`（プレフィックス無し） | レスポンスボディのJSON（§3.5.1〜3.5.4）          | `token = access_token`                    |
| `response.body.<json.path>`  | 同上（明示形）                                     | `token = response.body.access_token`     |
| `response.header.<Name>`     | レスポンスヘッダー値（大文字小文字を無視）        | `loc = response.header.Location`         |
| `request.header.<Name>`      | リクエストヘッダー値（送信した実値、大小無視）    | `sent_auth = request.header.Authorization` |
| `request.url`                | テンプレート展開後のリクエストURL                 | `called_url = request.url`               |
| `request.body`              | 送信したリクエストボディ全体（文字列）（form は urlencode 済み、file/multipart は UTF-8 replacement decode）| `sent_body = request.body`               |
| `request.body.<json.path>`   | リクエストボディをJSONとしてパースしパスで抽出（§3.5.1〜3.5.4 と同じ記法）| `dateIso = request.body.date.time_DATE_ISO` |

- `response.header.*` / `request.header.*` のヘッダー名は大文字小文字を区別しない。
  該当ヘッダーが存在しない場合はエラーで停止する。
- `request.header.*` は TOML の `headers` で明示指定したヘッダー（form 指定時に
  自動付与される `Content-Type` を含む）のみが対象。`urllib` が自動付与する
  `Host` / `User-Agent` / `Content-Length` / `Accept-Encoding` は対象外。
- `request.url` / `request.body` / `request.header.*` は**レスポンスに現れない値**を
  保存する用途に使う（リクエスト時に確定する値のキャプチャー）。
- `request.body.<path>` はリクエストボディがJSONとしてパースできる場合のみ成功する。パース失敗時はエラーで停止する。
- `body_form` の場合は URL エンコード済み文字列をパースしようとするため通常は失敗する（→ エラー）。フォーム値のキャプチャが必要な場合は `${var}` 経由で変数を参照すること。
- `body_file` / `body_multipart` については `request.body` 同様、`request.body.<path>` もサポート外（バリデーションエラー）とする。

### 3.5.1 トップレベルフィールドの抽出

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
token   == "xxxx"
expires == 3600
```

### 3.5.2 ネストオブジェクトの抽出（ドット区切り）

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

### 3.5.3 配列要素の抽出（インデックス）

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

### 3.5.4 パス解決アルゴリズム

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

### 3.5.5 抽出失敗時の挙動

- 指定パスが存在しない / ヘッダーが存在しない → エラーで停止（後続ステップは実行しない）
- レスポンスボディ系の `capture`（プレフィックス無し / `response.body.*`）が指定されているが
  レスポンスがJSONとしてパース不能 → エラー
- ヘッダー系・リクエスト系の `capture`（`response.header.*` / `request.*`）のみの場合は、
  レスポンスがJSONでなくてもエラーにならない
- `capture` 指定なしでJSONパース失敗 → 警告ログのみで継続

## 3.6 TOML特有の注意点

- 配列テーブル `[[requests]]` で順序保証されたリストとして定義
- 配列リテラル内で改行・末尾カンマが使えるため、項目数が多くても1ブロックを崩さず書ける
- ヘッダー名にハイフンが含まれても、文字列の中にあるためクォート不要

## 3.7 特殊ステップ `SLEEP`

`method = "SLEEP"` とすることで、指定秒数の待機（`time.sleep`）を行うステップを挿入できる。

```toml
[[requests]]
name   = "wait"
method = "SLEEP"
url    = "5"
```

- `url` に待機秒数を指定する（テンプレート変数も使用可）。
- `headers` / `body` / `body_form` / `body_file` / `body_multipart` / `capture` は指定不可（バリデーションエラー）。
- 出力: `==> [name] SLEEP 5` → `    > sleep 5.0 seconds` → `<== [name] done`

## 3.8 ポーリング（`until` フィールド）

「あるリソースのステータスが Active になるまで GET し続ける」ような
**条件成立までの繰り返し** を1ステップ内で完結させる。

```toml
[[requests]]
name    = "pollStatus"
method  = "GET"
url     = "https://api.example.com/jobs/${id}"
capture = ["status = data.status"]
until = [
    "condition    = ${status} == Active",
    "interval     = 2.0",     # 試行間の待機秒数（省略時 1.0）
    "max_attempts = 30",      # 最大試行回数（省略時 10）
]
```

### 3.8.1 動作

1. リクエスト送信（最初の試行は通常通り）
2. `capture` を評価して変数ストアを更新
3. `condition` をテンプレート展開 → 評価
4. 真なら次のステップへ。偽なら `interval` 秒待ってから 1. に戻る
5. `max_attempts` を超えても真にならなければ `RuntimeError` で失敗

- HTTP 4xx/5xx は特別扱いせず、通常レスポンスと同じくステータス・ヘッダー・本文を処理して `until` 判定へ進む。
- 各試行ごとに通常の request/response ログを出力し、最後に
  `* until satisfied on attempt N` または
  `* until not satisfied (attempt N/M), retrying in Xs` を出力する。
- `until` は SLEEP ステップでは指定できない。

### 3.8.2 `until` の各キー

| キー         | 必須 | 型     | デフォルト | 説明                                       |
|--------------|------|--------|------------|--------------------------------------------|
| condition    | ○    | string | —          | 真偽を判定する式（§3.8.3）                 |
| interval     | -    | float  | `1.0`      | 試行間の待機秒数（0 以上）                 |
| max_attempts | -    | int    | `10`       | 最大試行回数（1 以上）                     |

`until` は `parse_kv_list(..., "=")` で dict にパースする。
未知のキーはバリデーションエラー。

### 3.8.3 condition の式言語

`<LHS> <演算子> <RHS>` 形式のシンプルな比較式のみをサポートする。
LHS / RHS は両方ともテンプレート展開後に文字列として評価される。

| 演算子 | 例                                                  | 意味                                   |
|--------|-----------------------------------------------------|----------------------------------------|
| `==`   | `${status} == Active`                               | 文字列が一致                           |
| `!=`   | `${status} != Pending`                              | 文字列が不一致                         |
| `~`    | `${message} ~ /success/i`                           | `/pattern/[flags]` で正規表現マッチ    |
| `in`   | `${code} in [200, 201, 204]`                        | カンマ区切りリストに含まれる           |

- 演算子は LHS に最も近いものから探索する（典型的に LHS は `${...}` で
  これらの演算子を含まないので曖昧さは生じない）。
- `~` の RHS は `/pattern/flags` 形式。`flags` は `i` / `m` / `s` の組み合わせ。
- `in` の RHS は `[A, B, C]` 形式。各要素は空白トリム後に文字列比較。
- 真偽以外の判定（`>`, `<` 等の数値比較）は将来拡張（[11-extension-points.md](11-extension-points.md)）。
