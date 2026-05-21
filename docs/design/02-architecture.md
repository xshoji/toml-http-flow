# 3. アーキテクチャ

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

## 3.1 httpflow/config.py

- `RequestConfig` を `@dataclass` で定義
- `tomllib.load()` でTOMLをパース
- `dict → dataclass` への変換ヘルパを提供
- 不正なフィールド（`body` と `body_form` の同時指定など）をバリデーション

TOMLでは1リクエストの可読性を最優先するため、`headers` / `body_form` / `capture` は
`"Key: Value"` / `"key = value"` 形式の **文字列リスト** として受け取り、
データクラスへ変換する段階で dict にパースする（詳細は [03-toml-spec.md](03-toml-spec.md) §4.4）。

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

## 3.2 httpflow/template.py

- 正規表現 `r"\$(?:\$|\{([\w.\-]+)\})"` で `${...}` 形式の変数参照を検知（パス要素にハイフンも許可）
- 実行時変数ストア（`dict[str, Any]`）から値を解決
- ヘッダー値・URL・ボディ文字列・form値の各文字列に対して再帰的に適用
- 未定義変数参照時は例外を送出（厳格モード）

```python
def render(text: str, store: dict) -> str: ...
def render_mapping(mapping: dict[str, str], store: dict) -> dict[str, str]: ...
```

## 3.3 httpflow/httpclient.py

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

## 3.4 httpflow/workflow.py

- ステップを順次ループで実行
- 各ステップ実行前にテンプレート展開
- 実行後に `capture` の結果を変数ストアの `steps.<name>.<key>` に保存
- 後続ステップで参照可能にする
- ステップ毎にリクエスト/レスポンスの要約を標準出力に出力

## 3.5 httpflow/cli.py

- `argparse` で `-f`, `-v` をパース
- `-v key=value` を複数回受け取り `vars` 名前空間に格納
- `workflow.run(config, vars_)` を呼び出す
- 例外をキャッチして非ゼロ終了コードで終了
