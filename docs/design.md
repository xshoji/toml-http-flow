# API Workflow CLI Tool 設計書（Python版）

## 1. 概要

TOMLで定義されたHTTPリクエストワークフローを順次実行するPython製CLIツール。
後続のステップで前段の実行結果を変数として参照可能。
**Python標準ライブラリのみで実装**（外部依存ゼロ）。

加えて、ワークフローTOMLから **本ツールに依存しない単一の Python スクリプト** を
書き出す機能を備える。証跡保存・他者への共有・CI/CD への組み込みなど、
ツール本体が無い環境でも同じ検証を再現できるようにする。

## 2. 機能要件

| 項目         | 内容                                                              |
|--------------|-------------------------------------------------------------------|
| 実行方式     | シンプルな順次実行                                                |
| 設定形式     | TOML（`tomllib` / Python 3.11+ 標準）                              |
| Body形式     | JSON文字列 / form-data(x-www-form-urlencoded)                      |
| 変数スコープ | 後続の全ステップから参照可能                                      |
| 結果抽出     | JSONレスポンスからドット区切りパスで特定フィールドを抽出          |
| 引数         | `-f` (TOMLファイル指定)、`-v` (変数注入 `key=value`、複数回指定可)|
| 出力         | 標準出力                                                          |
| スクリプト生成 | `generate` サブコマンドでワークフローを独立した単一 .py に変換  |
| 依存         | Python 3.11+ 標準ライブラリのみ（生成スクリプトも同じく標準libのみ）|

### 2.1 使用する標準ライブラリ

| モジュール     | 用途                                          |
|----------------|-----------------------------------------------|
| `tomllib`      | TOMLファイル読み込み（Python 3.11+）          |
| `urllib.request` | HTTPリクエスト送信                          |
| `urllib.parse` | URLエンコード（form-data用）                  |
| `json`         | リクエスト/レスポンスJSONの処理               |
| `argparse`     | CLI引数パース                                 |
| `re`           | テンプレート変数の検知                        |
| `sys`          | 標準出力・終了コード制御                      |
| `dataclasses`  | 設定モデル定義                                |
| `typing`       | 型ヒント                                      |

## 3. アーキテクチャ

```
api-workflow-cli/
├── pyproject.toml          # メタデータのみ（依存なし）
├── README.md
├── httpflow/
│   ├── __init__.py
│   ├── __main__.py         # `python -m httpflow` のエントリポイント
│   ├── cli.py              # CLI引数パース・ディスパッチ
│   ├── config.py           # TOMLパース＋データクラス定義
│   ├── template.py         # テンプレート展開エンジン
│   ├── httpclient.py       # urllib ベースのHTTPクライアント
│   ├── workflow.py         # ステップ実行エンジン＋変数ストア
│   ├── generator.py        # ワークフロー → 単一 .py スクリプト生成
│   └── templates/
│       └── runner.py.tmpl  # 生成スクリプトのベーステンプレート
└── tests/
    ├── test_template.py
    ├── test_config.py
    └── test_workflow.py
```

### 3.1 httpflow/config.py

- `RequestConfig` を `@dataclass` で定義
- `tomllib.load()` でTOMLをパース
- `dict → dataclass` への変換ヘルパを提供
- 不正なフィールド（`body` と `body_form` の同時指定など）をバリデーション

TOMLでは1リクエストの可読性を最優先するため、`headers` / `body_form` / `capture` は
`"Key: Value"` / `"key = value"` 形式の **文字列リスト** として受け取り、
データクラスへ変換する段階で dict にパースする（詳細は § 4.4）。

```python
@dataclass
class RequestConfig:
    name: str
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    body_form: dict[str, str] | None = None
    capture: dict[str, str] = field(default_factory=dict)

@dataclass
class WorkflowConfig:
    requests: list[RequestConfig]


def load(path: str) -> WorkflowConfig:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return WorkflowConfig(
        requests=[_build_request(r) for r in raw.get("requests", [])]
    )


def _build_request(d: dict) -> RequestConfig:
    return RequestConfig(
        name      = d["name"],
        method    = d["method"],
        url       = d["url"],
        headers   = parse_kv_list(d.get("headers", []), ":"),
        body      = d.get("body"),
        body_form = parse_kv_list(d["body_form"], "=") if "body_form" in d else None,
        capture   = parse_kv_list(d.get("capture", []), "="),
    )
```

### 3.2 httpflow/template.py

- 正規表現 `r"\$(?:\$|\{([\w.\-]+)\})"` で `${...}` 形式の変数参照を検知（パス要素にハイフンも許可）
- 実行時変数ストア（`dict[str, Any]`）から値を解決
- ヘッダー値・URL・ボディ文字列・form値の各文字列に対して再帰的に適用
- 未定義変数参照時は例外を送出（厳格モード）

```python
def render(text: str, store: dict) -> str: ...
def render_mapping(mapping: dict[str, str], store: dict) -> dict[str, str]: ...
```

### 3.3 httpflow/httpclient.py

- `urllib.request.Request` でリクエストを構築
- `Content-Type` に応じて Body のエンコードを切り替え:
  - `application/json` または body が文字列 → そのまま bytes 化
  - `application/x-www-form-urlencoded` → `urllib.parse.urlencode()`
- `urllib.request.urlopen()` でリクエスト送信
- レスポンスを JSON として `json.loads()` でデコード
- `capture` 定義に従い、ドット区切りパスから値を抽出（再帰的辞書探索）
- HTTPエラー（`urllib.error.HTTPError`）はステータスコードと本文を含めて送出

```python
@dataclass
class Response:
    status: int
    reason: str
    headers: dict[str, str]
    body_text: str
    body_json: Any | None

def prepare_request(req: RequestConfig) -> tuple[urllib.request.Request, bytes | None]: ...
def execute(req: RequestConfig) -> Response: ...
def extract(body: Any, path: str) -> Any: ...
```

### 3.4 httpflow/workflow.py

- ステップを順次ループで実行
- 各ステップ実行前にテンプレート展開
- 実行後に `capture` の結果を変数ストアの `steps.<name>.<key>` に保存
- 後続ステップで参照可能にする
- ステップ毎にリクエスト/レスポンスの要約を標準出力に出力

### 3.5 httpflow/cli.py

- `argparse` で `-f`, `-v` をパース
- `-v key=value` を複数回受け取り `vars` 名前空間に格納
- `workflow.run(config, vars_)` を呼び出す
- 例外をキャッチして非ゼロ終了コードで終了

## 4. TOML仕様

### 4.1 設計方針

TOMLの素直な使い方（`[requests.headers]` などのサブテーブル）だと、1リクエストが複数ブロックに分割されてしまい「どこからどこまでが1リクエストか」が一目で分からない。

そこで、**1リクエスト = 1つの `[[requests]]` ブロックに収める**ことを最優先とし、ネストする `headers` / `body_form` / `capture` はすべて **配列形式の文字列リスト** で記述する方式を採用する。

- HTTP / curl と同じ「`Key: Value`」「`key=value`」記法なので親しみやすい
- 配列リテラル `[ ... ]` はTOML 1.0でも複数行展開・末尾カンマが許可されているため、項目数が増えても読みやすい
- インラインテーブル `{ ... }` と違って改行できるため、長くなっても破綻しない
- 1ブロックの中に全情報が完結し、視認性が高い

### 4.2 サンプル

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

### 4.3 フィールド定義

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

### 4.4 パース規則

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

### 4.5 capture の意味とパス記法

`capture` の各要素は `"<変数名> = <JSONパス>"` 形式で、
**レスポンスJSONの「JSONパス」位置にある値を、「変数名」として変数ストアに保存する** という指示。

保存先は `steps.<step_name>.<変数名>` で、後続ステップから `${steps.<step_name>.<変数名>}` で参照できる。

#### 4.5.1 トップレベルフィールドの抽出

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

#### 4.5.2 ネストオブジェクトの抽出（ドット区切り）

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

#### 4.5.3 配列要素の抽出（インデックス）

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

#### 4.5.4 パス解決アルゴリズム

```python
import re
from typing import Any

PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")

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

#### 4.5.5 抽出失敗時の挙動

- 指定パスが存在しない → エラーで停止（後続ステップは実行しない）
- レスポンスがJSONとしてパース不能だが `capture` が指定されている → エラー
- `capture` 指定なしでJSONパース失敗 → 警告ログのみで継続

### 4.6 TOML特有の注意点

- 配列テーブル `[[requests]]` で順序保証されたリストとして定義
- 配列リテラル内で改行・末尾カンマが使えるため、項目数が多くても1ブロックを崩さず書ける
- ヘッダー名にハイフンが含まれても、文字列の中にあるためクォート不要

### 4.7 特殊ステップ `SLEEP`

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

### 4.8 ポーリング（`until` フィールド）

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

#### 4.8.1 動作

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

#### 4.8.2 `until` の各キー

| キー         | 必須 | 型     | デフォルト | 説明                                       |
|--------------|------|--------|------------|--------------------------------------------|
| condition    | ○    | string | —          | 真偽を判定する式（§4.8.3）                 |
| interval     | -    | float  | `1.0`      | 試行間の待機秒数（0 以上）                 |
| max_attempts | -    | int    | `10`       | 最大試行回数（1 以上）                     |

`until` は `parse_kv_list(..., "=")` で dict にパースする。
未知のキーはバリデーションエラー。

#### 4.8.3 condition の式言語

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
- 真偽以外の判定（`>`, `<` 等の数値比較）は将来拡張（§12）。

## 5. テンプレート記法

Pythonの `string.Template` / シェル / Make などで広く使われている
**`${...}` 記法**を採用する。理由:

- Python標準ライブラリ `string.Template` と同系統で、Python開発者に馴染み深い
- `{...}` 単独だと `str.format` や f-string と紛らわしいが、`${...}` は曖昧さがない
- TOMLのインラインテーブル `{ }` と視覚的に衝突しない
- 先頭の `.` （Goテンプレート由来のクセ）が不要で簡潔
- 区切り文字が明示的なので、文字列中に埋め込んでも境界が分かりやすい

なお、ネストアクセスは `string.Template` 単体ではサポートされないため、
ドット区切りパスは独自に正規表現で実装する。

### 5.1 ステップ結果の参照

```
${steps.<step_name>.<capture_key>}
```

例:
```toml
Authorization = "Bearer ${steps.getToken.token}"
```

### 5.2 CLI引数変数の参照

```
${vars.<variable_name>}
```

例:
```toml
url = "https://api.${vars.env}.example.com/user"
```

### 5.3 リテラル `$` のエスケープ

`string.Template` の慣例に倣い `$$` で `$` 1文字として扱う。

```toml
body = '{"price":"$$100"}'   # → {"price":"$100"}
```

### 5.4 パス要素で使える文字

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

### 5.5 実装方針

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

## 6. CLIインターフェース

サブコマンド方式を採用する（`run` / `generate`）。
`run` は省略可能（後方互換のため、サブコマンド未指定時は `run` 扱い）。

```bash
# ワークフローを実行（基本）
python -m httpflow run -f workflow.toml
python -m httpflow     -f workflow.toml          # run 省略形

# 変数注入
python -m httpflow run -f workflow.toml -v env=production -v user_id=123

# 単一の Python スクリプトを生成
python -m httpflow generate -f workflow.toml -o workflow.py
python -m httpflow generate -f workflow.toml    # 標準出力に出力
```

### 6.1 サブコマンド: `run`

| 引数            | 必須 | 説明                                                                                  |
|-----------------|------|---------------------------------------------------------------------------------------|
| `-f`, `--file`     | ○    | 実行するワークフローTOMLファイルのパス                                             |
| `-v`, `--var`      | -    | `key=value` 形式の変数注入（複数指定可）                                           |
| `-q`, `--quiet`    | -    | 詳細出力を抑制し1ステップ1行のサマリのみ出す（**デフォルトは詳細表示ON**）         |
| `--pretty-json`    | -    | リクエスト/レスポンスの body が JSON のとき、インデント2スペースで整形して出力する |
| `-h`, `--help`     | -    | ヘルプ表示                                                                         |

### 6.1.1 詳細出力フォーマット

デフォルト（`--quiet` 未指定）では、各ステップごとに以下を出力する。
リクエストは `>`、レスポンスは `<` をプレフィックスとし、curl の `-vvv` に近い書式で揃える。

```
==> 2026-05-19 23:35:49.123 [<step_name>] <METHOD> <url>
    # <description 1行ずつ>            ← description 指定時のみ
    > <METHOD> <path> HTTP/1.1
    > Host: api.example.com
    > Content-Length: 31
    > User-Agent: Python-urllib/3.12
    > Accept-Encoding: identity
    > Header-Key: value
    > ...
    >
    > <body 1行ずつ>
<== 2026-05-19 23:35:49.456 [<step_name>] status=<code>
    < HTTP/1.1 200 OK
    < Header-Key: value
    < ...
    <
    < <body 1行ずつ>
    * capture <var_name> = <value>
```

- 各リクエスト送信直前 / レスポンス受信直後にローカル時刻（ミリ秒精度）を出力する。
- `description` が指定されていれば `==>` 行の直後に `    # <description>` として出力する（複数行は1行ずつ）。
- リクエストライン（`> POST /auth HTTP/1.1`）を出力する。
- urllib が自動付与する `Host`, `Content-Length`, `User-Agent`, `Accept-Encoding` は推定値で出力する。
- レスポンスのステータスライン（`< HTTP/1.1 200 OK`）を出力する。
- `--quiet` 指定時は `==>` / `<==` の2行（タイムスタンプ付き）と `description`（指定時）のみ出力する。
- `--pretty-json` 指定時、リクエスト/レスポンスの body が JSON としてパースできる場合は
  `json.dumps(..., indent=2, ensure_ascii=False)` で整形して `>` / `<` プリフィックス付きで出力する。
  JSON でない（フォーム/プレーンテキスト等）場合は通常通り未加工で出力する。
  生成スクリプト（`generate`）にも同じ `--pretty-json` フラグが用意される。

### 6.2 サブコマンド: `generate`

| 引数            | 必須 | 説明                                              |
|-----------------|------|---------------------------------------------------|
| `-f`, `--file`  | ○    | 入力ワークフローTOMLファイルのパス                |
| `-o`, `--output`| -    | 出力先 .py ファイル（省略時は標準出力）           |
| `-v`, `--var`   | -    | 生成スクリプトに **デフォルト値として埋め込む** 変数 |
| `--shebang`     | -    | 先頭に `#!/usr/bin/env python3` を付与（実行権付き） |
| `-h`, `--help`  | -    | ヘルプ表示                                        |

## 7. 処理フロー

1. CLI引数のパース（`argparse` で `-f`, `-v`）
2. TOMLファイルの読み込み（`tomllib.load()` はバイナリモードで開く必要あり）
3. dictをデータクラスへ変換＋バリデーション
4. 変数ストアの初期化:
   ```python
   store = {"vars": {...}, "steps": {}}
   ```
5. 各ステップを順次実行:
   1. テンプレート展開（URL、ヘッダー、ボディ内の変数参照を解決）
   2. `urllib.request.urlopen()` でHTTPリクエストを送信
   3. レスポンスを受信＆JSONとしてパース（`Content-Type` 判定）
   4. `capture` 定義に従い、指定パスから値を抽出
   5. 抽出した値を `store["steps"][name][key]` に保存
6. 全ステップ完了後、サマリを標準出力に表示

## 8. スクリプト生成機能（`generate` サブコマンド）

ワークフローTOMLから、本ツールに**一切依存しない単一の Python スクリプト**を生成する。
証跡保存・他人への共有・CI/CD 組み込み等を想定し、生成スクリプトも **標準ライブラリのみ**で動作する。

### 8.1 設計方針

| 項目                  | 方針                                                                       |
|-----------------------|----------------------------------------------------------------------------|
| 依存関係              | 生成スクリプトは Python 3.11+ 標準ライブラリのみ（本ツール本体は不要）     |
| 自己完結性            | 1ファイルで完結。インポート可能なヘルパも全て同ファイル内にインライン化    |
| 可読性                | "監査用" のため、人間が読んで何をしているか追える構造にする                |
| 入力との対応          | コメントで「どの `[[requests]]` ブロック由来か」を明示                     |
| 変数注入              | `argparse` で `-v key=value` を受け付ける（本ツールと同じ）                |
| 再実行性              | 何度実行しても同じ振る舞い（副作用は対象APIに依存）                        |

### 8.2 生成スクリプトの構造

**設計方針: 人間の可読性とアドホック編集のしやすさを最優先**。
データテーブル `+ for` ループ形式ではなく、**1 `[[requests]]` ブロック = 1 関数**として
展開する。これにより:

- 各ステップのリクエスト定義（URL/ヘッダー/ボディ/capture）を1関数内で完結して読める
- ステップを一時的にスキップしたい → `main()` 内の呼び出し1行をコメントアウトすればよい
- ステップを並べ替えたい → `main()` 内の呼び出し順を入れ替えればよい
- 1ステップのURLやペイロードだけを少し変えて再実行 → そのステップ関数だけを編集

```python
#!/usr/bin/env python3          # --shebang 指定時のみ
"""
Generated by httpflow vX.Y.Z at 2026-05-19T...
DO NOT EDIT — regenerate with: `python -m httpflow generate -f workflow.toml`

NOTE: each [[requests]] block becomes one self-contained step_* function below.
"""

import argparse, json, re, sys, urllib.error, urllib.parse, urllib.request

# ─── runtime helpers (inlined, no httpflow dependency) ─────────
def render(text, store): ...
def render_mapping(mapping, store): ...
def extract(body, path): ...
def do_request(method, url, headers, body_bytes, timeout=None): ...
def log_request(...): ...
def log_response(...): ...
def log_capture(...): ...
def apply_form_content_type(headers): ...

# ─── default variables (override with -v at runtime) ────────
DEFAULT_VARS = {
    "env": "production",
}

# ─── steps (one function per [[requests]] block) ────────────
def step_getToken(store, quiet=False):
    """[[requests]] name = 'getToken' — POST https://api.example.com/auth"""
    name = "getToken"
    method = "POST"
    url = render("https://api.example.com/auth", store)
    headers = render_mapping({
        "Content-Type": "application/json",
    }, store)
    body_form = None
    body_bytes = render('{"user":"test","pass":"secret"}', store).encode("utf-8")

    log_request(method, url, headers, body_bytes, body_form, quiet)
    status, reason, resp_headers, text, body_json = do_request(method, url, headers, body_bytes)
    log_response(name, status, reason, resp_headers, text, quiet)

    if body_json is None:
        raise RuntimeError(f"step {name!r}: capture requested but response is not JSON")
    captured = {}
    captured["token"] = extract(body_json, "access_token")
    log_capture("token", captured["token"], quiet)
    store["steps"][name] = captured


def step_getUser(store, quiet=False):
    ...

# ─── main ───────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Generated API workflow runner")
    parser.add_argument("-v", "--var", action="append", default=[], ...)
    parser.add_argument("-q", "--quiet", action="store_true", ...)
    args = parser.parse_args()

    store = {"vars": dict(DEFAULT_VARS), "steps": {}}
    for kv in args.var:
        k, _, v = kv.partition("=")
        store["vars"][k.strip()] = v

    # === Workflow ===
    # Comment out a line to skip that step. Reorder lines to change order.
    step_getToken(store, quiet=args.quiet)
    step_getUser(store, quiet=args.quiet)

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### 8.3 生成アルゴリズム

`httpflow/generator.py` の責務:

1. `config.load()` で TOML を読み込み、`WorkflowConfig` を得る
2. `templates/runner.py.tmpl` をベーステンプレートとして読み込む
3. 各 `RequestConfig` から `step_<sanitized_name>` 関数の本体を組み立てる
   - 関数名はステップ名を `[A-Za-z0-9_]` のみに正規化し、衝突時は数字サフィックスで一意化
   - URL/ヘッダー/ボディは Python リテラルとして可読的にインライン化
     (複数行ボディは `"""..."""`、ヘッダーは複数行 dict、form は `urllib.parse.urlencode`)
   - capture は `extract(body_json, "<path>")` の呼び出しを1変数ずつ展開
4. 以下のプレースホルダを置換:
   - `{{STEP_FUNCTIONS}}`: 各ステップ関数の定義（空行2つで区切り）
   - `{{STEP_CALLS}}`: `main()` 内に並べる `step_xxx(store, quiet=args.quiet)` の列
   - `{{DEFAULT_VARS}}`: `-v` で渡されたデフォルト変数
   - `{{GENERATED_AT}}`: 生成タイムスタンプ
   - `{{VERSION}}`: 本ツールのバージョン
5. 出力先（`-o` または stdout）に書き出す
6. `--shebang` 指定時は先頭に `#!/usr/bin/env python3` を付け、`chmod +x` 相当を実施

### 8.4 ヘルパ関数のインライン化方針

`render` / `extract` / `do_request` などのランタイム関数は、
**本ツールの実装をそのままコピー**して生成スクリプトに埋め込む（DRY より自己完結性を優先）。

これらの関数は `httpflow/template.py` `httpflow/httpclient.py` の実装と
**ロジック同等性をテストで担保する**（§ 9 参照）。

### 8.5 生成スクリプトの使い方（生成後）

```bash
# 生成
python -m httpflow generate -f workflow.toml -o audit/workflow_2026-05-19.py

# どこでも実行（本ツールは不要）
python3 audit/workflow_2026-05-19.py
python3 audit/workflow_2026-05-19.py -v env=staging -v token=abc
python3 audit/workflow_2026-05-19.py --quiet     # 詳細出力を抑制（デフォルトは詳細ON）
```

### 8.6 セキュリティ・運用上の注意

- TOML中にハードコードされた認証情報はそのまま埋め込まれるので、
  必要に応じて `-v` で上書きする運用を推奨（埋め込み値はあくまでデフォルト）
- 生成スクリプトはヘッダーに `DO NOT EDIT — regenerate with: ...` を明記し、
  手で書き換えてしまった場合でも再生成方法が分かるようにする
- 機密値は生成スクリプトから除外するオプション（`--strip-secrets=KEY,KEY` 等）を将来追加検討

## 9. エラーハンドリング

| 発生箇所         | 対応                                                                |
|------------------|---------------------------------------------------------------------|
| TOML パースエラー | `tomllib.TOMLDecodeError` を捕捉し、ファイル名と行番号を表示        |
| バリデーション   | `ValueError` を送出（`body` と `body_form` 同時指定など）           |
| 未定義変数参照   | `KeyError` を `TemplateError` に変換し、参照キーと位置を表示        |
| HTTPエラー       | `urllib.error.HTTPError` を捕捉し、ステータス＋本文を表示して終了   |
| JSONデコード失敗 | `capture` 指定があれば失敗、なければ警告のみで継続                  |

異常終了時は非ゼロの終了コードを返す（例: `sys.exit(1)`）。

## 10. テスト方針

`unittest`（標準ライブラリ）で以下を最低限カバー:

- **template**: 各種記法の展開、ネスト参照、未定義変数のエラー
- **config**: 正常TOMLのパース、排他フィールドのバリデーション
- **httpclient**: `http.server` でローカルモックを起動し、E2Eで検証
- **workflow**: 複数ステップ間の変数受け渡し

## 11. Go版との相違点

| 観点         | Go版                          | Python版                                  |
|--------------|-------------------------------|-------------------------------------------|
| 設定ファイル | YAML（外部ライブラリ必要）    | TOML（`tomllib` 標準）                    |
| 配列定義     | YAMLの `-` リスト              | TOMLの `[[requests]]` 配列テーブル        |
| HTTPクライアント | `net/http`                | `urllib.request`                          |
| ビルド成果物 | 単一バイナリ                  | スクリプト or `pip install` 配布           |
| 起動コマンド | `./api-workflow-cli`          | `python -m httpflow` / `httpflow`               |
| 型システム   | 構造体＋静的型                | `@dataclass` + `typing`                   |

## 12. 拡張余地（将来）

- リトライ・タイムアウト設定（`urllib` の `timeout` 引数で対応可）
- ステップ単位の `enabled` フラグ
- `assert` フィールドでレスポンス検証
- `--dry-run` モード（テンプレート展開後のリクエストだけ出力）
- 並列実行モード（`concurrent.futures` で実装可能）
- 特殊ステップの追加 (`SLEEP` 以外の制御フロー等)
- `until` の condition で数値比較（`>`, `<`, `>=`, `<=`）や論理演算（`&&`, `||`）
- `generate --strip-secrets` で機密ヘッダー/フィールドを除外して書き出し
- `generate --format curl` で curl コマンド列としても書き出せるようにする
