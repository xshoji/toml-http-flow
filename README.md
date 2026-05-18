# toml-http-flow (`apiwf`)

TOMLで定義したHTTPリクエストのワークフローを順次実行するCLIツール。
**Python 3.11+ の標準ライブラリのみ**で動作し、外部依存はゼロ。

加えて、ワークフローTOMLを **本ツールに依存しない単一の Python スクリプト** に
書き出す `generate` サブコマンドを備えており、証跡保存・配布・CI/CDへの
組み込みに使える。

## 特徴

- TOML で1リクエスト = 1ブロック (`[[requests]]`) として記述
- 後段のステップから前段のレスポンスを `${steps.<name>.<key>}` で参照
- `-v key=value` で外部変数を注入 (`${vars.<name>}` で参照)
- JSONレスポンスから `data.user.id` / `items[0].id` 形式でフィールド抽出
- 標準ライブラリ (`tomllib`, `urllib`, `json`, `argparse`) のみで実装
- 単一の自己完結 Python スクリプトを生成可能 (`generate` サブコマンド)

## 要件

- Python 3.11 以上 (`tomllib` 標準同梱のため)

## インストール

```bash
# リポジトリを clone してそのまま実行
git clone https://github.com/xshoji/toml-http-flow.git
cd toml-http-flow
python3 -m apiwf --help

# あるいは pip install (editable)
pip install -e .
apiwf --help
```

## 使い方

### ワークフローの実行

```bash
# 基本
python3 -m apiwf run -f workflow.toml

# `run` は省略可（後方互換）
python3 -m apiwf -f workflow.toml

# 変数注入
python3 -m apiwf run -f workflow.toml -v env=production -v user_id=123

# デフォルトでリクエスト/レスポンスの詳細を表示する
# サマリのみで十分なときは --quiet (-q) で抑制できる
python3 -m apiwf run -f workflow.toml -q
```

### 出力フォーマット

デフォルトでは各ステップごとに、curl `-v` 風の `>` (リクエスト) と `<` (レスポンス)
プレフィックス付きでヘッダーとボディを表示する。

```
==> [getToken] POST https://api.example.com/auth
    > Content-Type: application/json
    >
    > {"user":"test","pass":"secret"}
<== [getToken] status=200
    < Content-Type: application/json
    <
    < {"access_token":"tok-xyz"}
    * capture token = 'tok-xyz'
```

`--quiet` (`-q`) を指定すると `==>` / `<==` の各ステップ1行サマリだけになる。

### 単一スクリプトの生成

```bash
# .py ファイルに書き出し
python3 -m apiwf generate -f workflow.toml -o workflow.py

# 標準出力に書き出し
python3 -m apiwf generate -f workflow.toml

# 実行権付き shebang を先頭に付与
python3 -m apiwf generate -f workflow.toml -o workflow.py --shebang

# デフォルト変数を埋め込み
python3 -m apiwf generate -f workflow.toml -v env=production -o workflow.py
```

生成されたスクリプトは本ツール非依存で、どこでも動く:

```bash
python3 workflow.py
python3 workflow.py -v env=staging --quiet
```

## TOML 仕様

### 設計方針

**1リクエスト = 1つの `[[requests]]` ブロック**に収めることを最優先。
`headers` / `body_form` / `capture` はサブテーブルではなく
`"Key: Value"` / `"key = value"` 形式の **文字列配列** として記述する。

- HTTP / curl と同じ記法で親しみやすい
- 改行・末尾カンマが使えて項目が増えても読みやすい
- 1ブロックで全情報が完結し、視認性が高い

### サンプル

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
capture = ["user_id = data.user.id"]


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

### フィールド一覧

| フィールド | 必須 | 型             | 説明 |
|------------|------|----------------|------|
| `name`     | ○    | string         | ステップ名（変数参照に使用） |
| `method`   | ○    | string         | HTTPメソッド (GET/POST/PUT/DELETE 等) |
| `url`      | ○    | string         | リクエストURL |
| `headers`  | -    | array[string]  | `"Key: Value"` 形式 |
| `body`     | -    | string         | 生テキストボディ (`body_form` と排他) |
| `body_form`| -    | array[string]  | `"key = value"` 形式、`application/x-www-form-urlencoded` 自動付与 |
| `capture`  | -    | array[string]  | `"var_name = json.path"` 形式 |

### パース規則

| フィールド | 区切り | 分割回数 | 例 | 結果 |
|------------|--------|----------|-----|------|
| `headers`  | 最初の `:` | 1回 | `"Authorization: Bearer abc"` | `{"Authorization": "Bearer abc"}` |
| `body_form`| 最初の `=` | 1回 | `"email = a@example.com"` | `{"email": "a@example.com"}` |
| `capture`  | 最初の `=` | 1回 | `"token = access_token"` | `{"token": "access_token"}` |

- 区切り文字の前後の空白はトリムされる
- 区切り文字が値側に含まれていても、**最初の1つ**だけが区切りとして扱われる
  - 例: `"X-Url: https://example.com:8080/path"` → `key=X-Url`, `value=https://example.com:8080/path`

### `capture` のパス記法

JSONレスポンスから値を取り出して、変数ストアの `steps.<step_name>.<var_name>` に保存する。

```jsonc
// レスポンス
{
  "data": { "user": { "id": 42, "tags": ["admin", "owner"] } }
}
```

```toml
capture = [
    "uid       = data.user.id",
    "first_tag = data.user.tags[0]",
]
```

- ドット区切りで階層を辿る
- `[N]` でリストのインデックスを指定
- 指定パスが存在しなければエラーで停止

## テンプレート記法

`${...}` 形式の変数参照。`$$` で `$` 1文字にエスケープ。

```toml
url     = "https://api.${vars.env}.example.com/me"
headers = ["Authorization: Bearer ${steps.getToken.token}"]
body    = '{"price":"$$100"}'   # → {"price":"$100"}
```

参照可能な名前空間:

- `vars.<name>` … CLI の `-v key=value` で注入した変数
- `steps.<step_name>.<capture_key>` … 前段ステップで `capture` した値

未定義変数を参照すると `TemplateError` で停止する。

## プロジェクト構成

```
toml-http-flow/
├── pyproject.toml
├── README.md
├── AGENTS.md
├── docs/
│   └── design.md
├── apiwf/
│   ├── __init__.py
│   ├── __main__.py         # `python -m apiwf` のエントリポイント
│   ├── cli.py              # CLI 引数パース＋ディスパッチ
│   ├── config.py           # TOMLパース＋データクラス
│   ├── template.py         # ${...} 展開エンジン
│   ├── httpclient.py       # urllib HTTPクライアント＋JSONパス抽出
│   ├── workflow.py         # ステップ実行＋変数ストア
│   ├── generator.py        # ワークフロー → 単一 .py 生成
│   └── templates/
│       └── runner.py.tmpl  # 生成スクリプトのテンプレート
└── tests/
    ├── test_template.py
    ├── test_config.py
    ├── test_httpclient.py
    ├── test_workflow.py
    └── test_generator.py
```

## 開発

```bash
# テスト実行 (標準ライブラリ unittest)
python3 -m unittest discover -s tests -v

# CLI 動作確認
python3 -m apiwf --help
python3 -m apiwf run --help
python3 -m apiwf generate --help
```

テストは標準ライブラリ `http.server` でローカルモックを立ててHTTP往復もE2E検証する。

## 終了コード

| コード | 意味 |
|--------|------|
| `0`    | 全ステップ成功 |
| `1`    | TOMLパース失敗 / バリデーション失敗 / HTTP失敗 / capture失敗 など |

## ライセンス

[MIT](LICENSE)
