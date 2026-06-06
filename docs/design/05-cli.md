# 6. CLIインターフェース

サブコマンド方式を採用する（`run` / `generate`）。
`run` は省略可能（後方互換のため、サブコマンド未指定時は `run` 扱い）。

```bash
# Python スクリプト生成（既定）
python -m httpflow generate -f workflow.toml -o workflow.py
python -m httpflow generate -f workflow.toml                 # 標準出力に出力

# bash スクリプト生成
python -m httpflow generate -f workflow.toml --format bash -o workflow.sh
```

## 6.1 サブコマンド: `run`

| 引数            | 必須 | 説明                                                                                  |
|-----------------|------|---------------------------------------------------------------------------------------|
| `-f`, `--file`     | ○    | 実行するワークフローTOMLファイルのパス                                             |
| `-v`, `--var`      | -    | `key=value` 形式の変数注入（複数指定可）                                           |
| `-s`, `--step`     | -    | 実行するステップ名（`name`）を指定（複数指定可）。詳細は §6.1.4                    |
| `-q`, `--quiet`    | -    | 詳細出力を抑制し1ステップ1行のサマリのみ出す（**デフォルトは詳細表示ON**）         |
| `--pretty-json`    | -    | リクエスト/レスポンスの body が JSON のとき、インデント2スペースで整形して出力する |
| `--no-mask`        | -    | センシティブフィールドのマスキングを無効化する（**デフォルトはマスキングON**）       |
| `--blank-line N`   | -    | ステップ間の標準出力ログを `N` 行の空行で区切る（未指定時は0）                    |
| `-h`, `--help`     | -    | ヘルプ表示                                                                         |

`${var.K}` 形式で明示参照され、`-v K=...` で与えられていない変数は必須パラメタとして扱う。
`run` と生成スクリプトはいずれも、最初の step 実行前に不足を検出してエラー終了する。

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
<== 2026-05-19 23:35:49.456 [<step_name>]
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
- `--blank-line N` 指定時は、ステップ間（前ステップの出力完了後、次ステップ開始前）に空行を `N` 行出力する。
- `--pretty-json` 指定時、リクエスト/レスポンスの body が JSON としてパースできる場合は
  `json.dumps(..., indent=2, ensure_ascii=False)` で整形して `>` / `<` プリフィックス付きで出力する。
  JSON でない（フォーム/プレーンテキスト等）場合は通常通り未加工で出力する。
  生成スクリプト（`generate`）にも同じ `--pretty-json` フラグが用意される。

### 6.1.2 センシティブフィールドのマスキング

詳細出力（`>` / `<` 行および `==>` の URL、`* capture` 行）に含まれる
機密情報を、ログ表示時に `***` へ置換する。
**マスキングはデフォルトで ON**。実際に送出される HTTP リクエスト本体や
変数ストア (`store["vars"]`) には一切手を加えない（あくまで「画面に出す
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

#### 追加マスキングの指定

環境変数 `HTTPFLOW_MASK_EXTRA` にカンマ区切りでキー名を指定すると、
そのキー名（ヘッダーであれ body/クエリ/capture であれ）もマスキング対象に
加わる。デフォルトの既知キーは変更・削除できない。

| 環境変数            | 説明                                                          |
|---------------------|---------------------------------------------------------------|
| `HTTPFLOW_MASK_EXTRA` | 追加でマスキング対象とするキー名（カンマ区切り）。ヘッダー・body・クエリ・capture の区別なし。 |

判定はキー名を `lower()` してから `_` / `-` / 空白を除去した正規化形で
完全一致比較する。例: `apiKey`, `API-KEY`, `api_key`, `apikey` はすべて
同じキーとして扱う。

#### 無効化

マスキングは `--no-mask` 実行時引数で無効化できる。
環境変数による無効化は提供しない。

#### 生成スクリプト (`generate`) との関係

生成スクリプト側にも同等のマスキングロジックを**インライン**で埋め込み、
`httpflow` 本体が無くても同じ挙動になる。
生成スクリプトでも `--no-mask` 引数で無効化できる。

### 6.1.4 ステップを指定して実行（`--step`）

`-s` / `--step` でステップ名（TOMLの `name`）を指定すると、**指定したステップだけを実行**する。
未指定時は従来通り全ステップを実行する（後方互換）。

```bash
# getUser ステップだけ実行
python -m httpflow run -f workflow.toml --step getUser

# getToken と getUser を実行
python -m httpflow run -f workflow.toml -s getToken -s getUser
```

- 実行順序は常に **TOML での定義順** に従う（`--step` を並べた順番には依存しない）。
- 存在しないステップ名を指定した場合は、最初のリクエスト送信前にエラー終了する
  （`unknown step name(s): [...]`）。
- 必須変数（`${var.*}`）の検証と `${repeat.*}` の必須判定は、**選択されたステップのみ**を
  対象に行う。選択外のステップでしか参照されない変数は要求されない。
- 選択したステップが、選択外の先行ステップの `capture` 結果に依存する場合は、
  その値を `-v` で明示的に渡す必要がある。

## 6.2 サブコマンド: `generate`

| 引数               | 必須 | 説明                                              |
|--------------------|------|---------------------------------------------------|
| `-f`, `--file`     | ○    | 入力ワークフローTOMLファイルのパス                |
| `-o`, `--output`   | -    | 出力先スクリプトファイル（省略時は標準出力）           |
| `--format`         | -    | 出力形式 `python` または `bash`（既定: `python`） |
| `-v`, `--var`      | -    | 生成スクリプトの `DEFAULT_VARS` に埋め込む変数（実行時に `-v` で上書き可能） |
| `--shebang`        | -    | 先頭に shebang を付与（`python` → `#!/usr/bin/env python3`、`bash` → `#!/usr/bin/env bash`）し、実行権を付与 |
| `-h`, `--help`     | -    | ヘルプ表示                                        |

### 6.2.1 埋め込みの挙動

`-v` を `generate` に渡した場合、その値は生成スクリプトに**デフォルト値として埋め込まれる**。埋め込まれた値は、実行時に同名の引数で上書き可能。
