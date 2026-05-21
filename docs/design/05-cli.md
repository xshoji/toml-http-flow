# 6. CLIインターフェース

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

## 6.1 サブコマンド: `run`

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

### 6.1.2 センシティブフィールドのマスキング

詳細出力（`>` / `<` 行および `==>` の URL、`* capture` 行）に含まれる
機密情報を、ログ表示時に `***`（既定値）へ置換する。
**マスキングはデフォルトで ON**。実際に送出される HTTP リクエスト本体や
変数ストア (`store["steps"]`) には一切手を加えない（あくまで「画面に出す
文字列」だけを差し替える）。

対象は以下:

| 箇所                    | 判定対象                                         |
|-------------------------|--------------------------------------------------|
| リクエストヘッダー      | ヘッダー名（既定: `Authorization`, `Cookie`, …） |
| レスポンスヘッダー      | ヘッダー名（同上）                               |
| リクエスト URL のクエリ | クエリパラメータ名（既定: `token`, `password`, …）|
| リクエスト body (JSON)  | キー名を再帰的に判定                             |
| リクエスト body (form)  | キー名                                           |
| レスポンス body (JSON)  | キー名を再帰的に判定                             |
| `* capture` 行          | キャプチャ先変数名                               |

JSON / form として解釈できない plain-text body はそのまま出力する。

#### デフォルトの既知キー（大文字小文字・`_`/`-` の差は無視）

- ヘッダー: `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`,
  `X-Api-Key`, `X-Auth-Token`, `X-Access-Token`, `X-Csrf-Token`,
  `X-Xsrf-Token`, `X-Session-Token`, `X-Session-Id`, `X-Secret-Key`
- ボディ/クエリ/capture: `password`, `passwd`, `pwd`, `secret`,
  `client_secret`, `token`, `access_token`, `refresh_token`, `id_token`,
  `auth_token`, `session_token`, `api_key`, `apikey`, `private_key`,
  `authorization`, `auth`, `session`, `session_id`, `cookie`,
  `credit_card`, `card_number`, `cvv`, `cvc`, `pin`, `ssn`

#### 環境変数による上書き

| 環境変数                          | 説明                                                                 |
|-----------------------------------|----------------------------------------------------------------------|
| `HTTPFLOW_MASK_DISABLED`          | `1` / `true` / `yes` / `on` でマスキング全体を無効化                 |
| `HTTPFLOW_MASK_PLACEHOLDER`       | 置換文字列（デフォルト `***`）                                       |
| `HTTPFLOW_MASK_HEADERS`           | 対象ヘッダー名（カンマ区切り）。**デフォルトを置き換える**           |
| `HTTPFLOW_MASK_HEADERS_EXTRA`     | 対象ヘッダー名（カンマ区切り）。**デフォルトに追加する**             |
| `HTTPFLOW_MASK_BODY_KEYS`         | 対象 body/クエリ/capture キー名（カンマ区切り）。**デフォルトを置換**|
| `HTTPFLOW_MASK_BODY_KEYS_EXTRA`   | 対象 body/クエリ/capture キー名（カンマ区切り）。**デフォルトに追加**|

判定はキー名を `lower()` してから `_` / `-` / 空白を除去した正規化形で
完全一致比較する。例: `apiKey`, `API-KEY`, `api_key`, `apikey` はすべて
同じキーとして扱う。

#### 生成スクリプト (`generate`) との関係

生成スクリプト側にも同等のマスキングロジック（既定キーと環境変数の解釈）
を**インライン**で埋め込み、`httpflow` 本体が無くても同じ環境変数で同じ
ようにマスキングできるようにする。

## 6.2 サブコマンド: `generate`

| 引数            | 必須 | 説明                                              |
|-----------------|------|---------------------------------------------------|
| `-f`, `--file`  | ○    | 入力ワークフローTOMLファイルのパス                |
| `-o`, `--output`| -    | 出力先 .py ファイル（省略時は標準出力）           |
| `-v`, `--var`   | -    | 生成スクリプトに **デフォルト値として埋め込む** 変数 |
| `--shebang`     | -    | 先頭に `#!/usr/bin/env python3` を付与（実行権付き） |
| `-h`, `--help`  | -    | ヘルプ表示                                        |
