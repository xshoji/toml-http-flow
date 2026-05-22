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
| `--no-mask`        | -    | センシティブフィールドのマスキングを無効化する（**デフォルトはマスキングON**）       |
| `--repeat-vars`    | △    | `${repeat.K}` 用のカンマ区切り値リスト（複数指定可）。詳細は §6.1.3              |
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
機密情報を、ログ表示時に `***` へ置換する。
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

### 6.1.3 ワークフローの繰り返し実行（`--repeat-vars`）

TOML側に `${repeat.<name>}` 参照が1つでも存在する場合、CLI実行時に
**`--repeat-vars "name=v1,v2,v3"` の指定が必須**になる。

```bash
python -m httpflow run -f workflow.toml \
    --repeat-vars "id=1,2,3" \
    --repeat-vars "label=a,b,c"
```

- 値はカンマで分割し、前後の空白はトリムされる（空要素は許可しない）。
- 複数の `--repeat-vars` を併用する場合、すべてのキーで
  **カンマ分割後の要素数が一致している必要がある**（不一致はエラー）。
- 同じキーを2回指定するとエラーになる。
- 実行は要素数 `N` 回ループする。`i` 回目（1-origin）の各リクエストでは
  `${repeat.id}` → 第 `i` 要素、`${repeat.label}` → 第 `i` 要素のように
  各キーの **同じインデックスの値** で置換される。
- 反復の境界に `=== repeat iteration i/N {...} ===` 行が出力される。
- 各反復の開始時に `store["steps"]` はリセットされる（前イテレーションの
  capture 結果は次イテレーションに残らない）。`store["vars"]` （`-v` で
  渡した値）は全イテレーションで共有される。
- TOML側に `${repeat.X}` が無く、かつ `--repeat-vars` も指定しない場合は
  従来通り1回だけ実行する（後方互換）。
- TOML側に `${repeat.X}` があるのに `--repeat-vars` で `X` が与えられない
  とエラー（`--repeat-vars missing for: ['X']`）。

## 6.2 サブコマンド: `generate`

| 引数               | 必須 | 説明                                              |
|--------------------|------|---------------------------------------------------|
| `-f`, `--file`     | ○    | 入力ワークフローTOMLファイルのパス                |
| `-o`, `--output`   | -    | 出力先 .py ファイル（省略時は標準出力）           |
| `-v`, `--var`      | -    | 生成スクリプトに **デフォルト値として埋め込む** 変数。`--embed` ありで固定埋め込み、なしで `DEFAULT_VARS` の初期値として埋め込み |
| `--repeat-vars`    | △    | 生成スクリプトに埋め込む `${repeat.K}` 用リスト。**`--embed` 指定時のみ有効**（それ以外はエラー） |
| `--embed`          | -    | `-v` / `--repeat-vars` の値を生成スクリプトのデフォルト実行値として固定埋め込む |
| `--shebang`        | -    | 先頭に `#!/usr/bin/env python3` を付与（実行権付き） |
| `-h`, `--help`     | -    | ヘルプ表示                                        |

### 6.2.1 埋め込み（`--embed`）の有無による差異

| 状態 | `-v K=V` の扱い | `--repeat-vars K=V1,V2` の扱い |
|------|----------------|--------------------------------|
| `--embed` なし | `DEFAULT_VARS` の初期値として埋め込まれる。実行時に `-v` で上書き可能 | **受け付けずエラー** |
| `--embed` あり | `DEFAULT_VARS` に埋め込まれる。実行時に `-v` で上書き可能 | `DEFAULT_REPEAT_VARS` に埋め込まれる。実行時に `--repeat-vars` で上書き可能 |

両方の状態で、生成後のスクリプトは実行時に同じく `-v` や `--repeat-vars` を渡して上書きできる。
`--embed` の目的は「生成時に与えた値をデフォルトとしてスクリプトに持たせ、追加引数なしで実行可能にする」ことである。
