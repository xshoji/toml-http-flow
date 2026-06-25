# 7. スクリプト生成機能 (`generate` サブコマンド)

ワークフロー TOML から、本ツールに**一切依存しない単一のスクリプト**を生成する。
既定の出力形式は **bash**（§7.8 参照）。`--format python` を指定すると以下の
§7.1〜§7.7 で述べる Python スクリプトを生成する。
証跡保存・他人への共有・CI/CD 組み込み等を想定し、いずれの生成スクリプトも
**標準ライブラリのみ** で動作する（bash 版は `curl` / `jq` を利用）。

## 7.1 設計方針

| 項目           | 方針                                                                     |
|----------------|--------------------------------------------------------------------------|
| 依存関係       | 生成スクリプトは Python 3.11+ 標準ライブラリのみ（本ツール本体は不要）   |
| 自己完結性     | 1ファイルで完結。必要なランタイム helper だけを httpflow/runtime/*.py から flatten して埋め込む |
| 可読性         | "監査用" のため、人間が読んで何をしているか追える構造にする                |
| 入力との対応   | コメントで「どの `[[requests]]` ブロック由来か」を明示                     |
| 変数注入       | `argparse` で `-v key=value` を受け付ける（本ツールと同じ）                |
| 再実行性       | 何度実行しても同じ振る舞い（副作用は対象 API に依存）                      |

## 7.2 生成スクリプトの構造

```
generated_script.py
  ├── import / argparse
  ├── {{RUNTIME_HELPERS}}  ← 必要な httpflow/runtime/*.py ソースを flatten して貼り付け
  ├── step 関数群            ← 各 step を run_step(...) 呼び出しとして展開
  ├── main()                 ← argparse + ループ + step 呼び出し
  └── __main__ エントリポイント
```

設計方針: **人間の可読性とアドホック編集のしやすさを最優先**。
データテーブル `+ for` ループ形式ではなく、**1 `[[requests]]` ブロック = 1 関数**として
展開する。これにより:

- 各ステップのリクエスト定義（URL/ヘッダー/ボディ/capture）を1関数内で完結して読める
- ステップを一時的にスキップしたい → `main()` 内の呼び出し1行をコメントアウトすればよい
- ステップを並べ替えたい → `main()` 内の呼び出し順を入れ替えればよい
- 1ステップのURLやペイロードだけを少し変えて再実行 → そのステップ関数だけを編集

## 7.3 ランタイム flatten 方式

`generator.py` は **長いランタイム実装文字列を持たない**。
代わりに `httpflow/runtime/*.py` のソーステキストを読み込み、必要な機能だけを選んで
テンプレートの `{{RUNTIME_HELPERS}}` にフラット化して埋め込む。

```text
runner.py.tmpl
  {{RUNTIME_HELPERS}}
  {{STEP_FUNCTIONS}}
  {{STEP_CALLS}}
```

**flatten のルール**:

- ワークフローに `until` を使う step があれば `runtime/until.py` を含める
- step が存在すれば `runtime/http.py`（これは `core` と `mask` に依存する）を含める
- `from __future__ import annotations` と `httpflow/runtime/` 内の相対 import は除去する
- `import httpflow` や `from httpflow ...` は絶対に残さない
- 標準ライブラリ以外を import しない
- CLI / TOML parser / generator 固有処理を入れない
- 重複する stdlib import は許容する（AST 変換等の複雑な処理を避けるため）

**依存関係マニフェスト**:

| モジュール | 依存       |
|-----------|-----------|
| `core`    | --        |
| `mask`    | --        |
| `http`    | `core`, `mask` |
| `until`   | `core`    |

解決順は常に `core → mask → http → until` とする。

## 7.4 生成アルゴリズム

`httpflow/generator.py` の責務:

1. `config.load()` で TOML を読み込み :class:`WorkflowSpec` を得る
2. `templates/runner.py.tmpl` をベーステンプレートとして読み込む
3. `WorkflowSpec` から必要なランタイム機能を検出し、`httpflow/runtime/*.py` を選んで flatten し
   `{{RUNTIME_HELPERS}}` に埋め込む
4. 各 `Step` から `step_<sanitized_name>` 関数の本体を組み立てる
   - 関数名はステップ名を `[A-Za-z0-9_]` のみに正規化し、衝突時は数字サフィックスで一意化
   - HTTP ステップは `run_step(store, name, method, url, headers=..., body=..., capture=..., ...)` の呼び出し1つに集約
     （URL/ヘッダー/ボディは Python リテラルとしてインライン化。複数行ボディは `"""..."""`、ヘッダー/form は複数行 dict）
   - `until` 指定ありの HTTP ステップは内部関数 `attempt()` に `run_step` を包み、`poll_until(...)` で実行する
   - `SLEEP` ステップも `run_step(method="SLEEP", url=seconds, ...)` として統一
5. 以下のプレースホルダを置換:
   - `{{RUNTIME_HELPERS}}`: 必要な `runtime/*.py` から flatten したソース
   - `{{UNTIL_HELPERS}}`: `until` 使用時のみ `poll_until` を含むヘルパ群（未使用時はコメントのみ）
   - `{{DEFAULT_VARS}}`: `-v` で渡されたデフォルト変数（dict リテラル）
   - `{{REQUIRED_VARS}}`: `${var.<key>}` で参照されているが `DEFAULT_VARS` に無い変数名（list リテラル）
   - `{{STEP_FUNCTIONS}}`: 各ステップ関数の定義（空行2つで区切り）
   - `{{STEP_CALLS}}`: `main()` 内に並べる `step_xxx(store, ...)` の列
   - `{{GENERATED_AT}}`: 生成タイムスタンプ
   - `{{VERSION}}`: 本ツールのバージョン
   - 生成スクリプトの `main()` は `-v` を `store["vars"]` に反映した直後、step 呼び出し前に `REQUIRED_VARS` の不足を検証する
6. 出力先（`-o` または stdout）に書き出す
7. `--shebang` 指定時は先頭に `#!/usr/bin/env python3` を付け、`chmod +x` 相当を実施

## 7.5 ヘルパ関数の二重実装と parity 担保

`httpflow/runtime/*.py` の helper は以下の通り、**本体コードからも import して使い、
生成スクリプトには必要なモジュールだけ flatten して埋め込む**。

| function        | パッケージ側 import 元                  | 生成スクリプト提供元      |
|-----------------|----------------------------------------|--------------------------|
| `render`        | `runtime.core`                         | `runtime/core.py` 平滑化  |
| `extract`       | `runtime.http`                         | `runtime/http.py` 平滑化  |
| `do_request`    | `runtime.http`                         | `runtime/http.py` 平滑化  |
| `run_step`      | `runtime.http`                         | `runtime/http.py` 平滑化  |
| `eval_until`    | `runtime.until`                        | `runtime/until.py` 平滑化 |
| `mask` / `mask_url` / `mask_value` | `runtime.mask`         | `runtime/mask.py` 平滑化  |

本体側の `template.py` は、`runtime.core` の `render` を import し、
`${var.*}` の名前を抽出する `find_var_names()` のみを追加実装する。
独立ファイルは存在せず、すべて `runtime/` 配下のモジュールが source-of-truth である。

## 7.6 生成スクリプトの使い方（生成後）

```bash
# 生成（既定は bash、--format python で Python スクリプト）
python -m httpflow generate -f workflow.toml --format python -o audit/workflow_2026-05-19.py

# どこでも実行（本ツールは不要）
python3 audit/workflow_2026-05-19.py
python3 audit/workflow_2026-05-19.py -v env=staging -v token=abc
python3 audit/workflow_2026-05-19.py --quiet     # 詳細出力を抑制（デフォルトは詳細ON）
```

## 7.7 セキュリティ・運用上の注意

- TOML 中にハードコードされた認証情報はそのまま埋め込まれるので、
  必要に応じて `-v` で上書きする運用を推奨（埋め込み値はあくまでデフォルト）
- 生成スクリプトは先頭に生成元コメント（`Generated by toml-http-flow ...`）を明記し、
  手で書き換えてしまった場合でも再生成方法が分かるようにする
- 機密値は生成スクリプトから除外するオプション（`--strip-secrets=KEY,KEY` 等）を将来追加検討

## 7.8 bash スクリプト生成（`--format bash`・既定）

`--format bash`（既定）を指定すると、Python スクリプトではなく**単一の bash スクリプト**を生成する。
HTTP リクエストには `curl` を利用する。

### 7.8.1 設計方針（簡易版）

| 項目           | 方針                                                                     |
|----------------|--------------------------------------------------------------------------|
| 依存関係       | 生成スクリプトは `bash` / `curl` を利用する。capture または実行時 `--pretty-json` 指定時は JSON 処理に `jq` も利用する |
| 自己完結性     | 1ファイルで完結。`httpflow` パッケージには一切依存しない                   |
| 可読性         | 1 `[[requests]]` ブロック = 1 `step_<name>()` 関数として展開             |
| 変数展開       | **自前テンプレートエンジンは持たない**。`${random.UUID}` / `${random.UUID_HEX}` は bash ヘルパー、`${var.X}` は `${VAR_X}` に変換し、それ以外の `${...}` や `$VAR` はそのままシェルに渡す |
| 未対応機能     | --quiet / -v は生成スクリプトでは実装しない |
| ファイル埋め込み | `--embed-files` 指定時、body_file / body_multipart のリテラルパスを Base64 埋め込みする。`${...}` プレースホルダを含むパスは既存の実行時解決にフォールバックする |

### 7.8.2 生成スクリプトの構造

```bash
workflow.sh
  ├── # Generated by toml-http-flow <ver> at <timestamp>
  ├── set -uo pipefail
  ├── curl 依存チェック
  ├── jq 依存チェック（capture 使用時のみ）
  ├── mask() ヘルパー（sed による単純なログ用マスキング）
  ├── capture_*() ヘルパー（capture 使用時のみ）
  ├── uuid() / uuid_hex() ヘルパー（Python標準ライブラリの uuid を利用）
  ├── step_<name>() 関数群（各 [[requests]] 由来をコメント明記）
  └── main()（step呼び出しの単純な列挙）
```

- ステップ関数名はステップ名を `[A-Za-z0-9_]` に正規化し、衝突時は数字サフィックスで一意化
- ログ出力時は `authorization` / `password` / `token` などの既定キーワードに対して、`sed` で値部分を `***` に置換する（JSON/form/query の構造保持は保証しない）
- `${random.UUID}` は `uuid`、`${random.UUID_HEX}` は `uuid_hex` 関数呼び出しとして生成し、参照ごとに新しい UUID v4 を生成する
- **HTTP ステップ**は各 step 関数が `curl_command` 文字列を構築する。ベースは `curl -sS -L -v --no-buffer --stderr - -X <METHOD>` で、body / headers / form / multipart に応じて `-H "..."`, `-d "$body"`, `--data-urlencode k=v`, `--data-binary "@$path"`, `--form-string name=value`, `-F "name=@"path";filename="...";type="...""` 等の curl 引数をインラインで並べる。これらは `cat << 'EOT' ... EOT`（quoted delimiter）による heredoc で `curl_command` 変数に格納し、step 関数を見るだけで実行される curl コマンドが分かるようになっている
- `http_step` ヘルパは **curl 実行 + ログ出力 + trace_file 作成 + body_log 導出** を担う薄い executor。step 関数から `http_step "$step_name" "$method" "$url" "$description" "$curl_command"` の形式で呼び出す。`http_step` 内で `curl_command` を1回だけ `eval` して bash 配列 `cmd_args` に展開し、その配列から body 系フラグ（`-d` / `--data*` / `-F` / `--form*`）の値を集めて `body_log` を組み立てる。続いて同じ `cmd_args` 配列で curl を実行するため、ログと実際のリクエストが同じ値（`$(uuid)` / `$(time_date_ymd)` 等の動的値も含む）を観察する。`http_step` は作成した一時 trace ファイルのパスをグローバル変数 `HF_TRACE_FILE` にセットし、step 関数が capture に利用できるようにする。`capture ... = request.body`（または `request.body.<json.path>`）を使う step が存在する場合のみ、導出した `body_log` を `HF_BODY_LOG` にも公開する（未使用時は `set -u` 下で未定義変数となるのを防ぐため代入を省略する）
- curl 出力は `grep -v '^\({\|}\) \[.*bytes data\]'` と `grep -v '^\*'`、さらに `sed -e 's/\* Closing.*//' -e 's/\* Connection.*//'` で転送量メタ行・SSL等の診断行を除外してから、`tee` で標準出力と一時 trace ファイルへ同時に出力する
- `curl -v` はリクエストボディを出力しないため、ボディがある場合は curl 実行後、`>` 行（または `> ` 行）が出力されたタイミングで導出済みの `body_log` を `> ` プレフィックス付きで出力し、同じ trace ファイルに保存する
- **ボディ（text）**は `body=$(cat << EOT ... EOT)` による heredocument で変数に格納し（末尾に改行を補完）、curl 引数としては `-d "$body"` として `curl_command` に並べる
- **ボディ（form）**は各フィールドを `--data-urlencode "k=v"` として `curl_command` に直接展開し、`Content-Type: application/x-www-form-urlencoded` ヘッダを自動付与する。`body_log` は step 関数内では構築せず、`http_step` が `cmd_args` から `--data-urlencode` 値を集めて生成する
- **ボディ（file）**は `--data-binary "@$path"` として `curl_command` に並べる。step 関数内でファイル存在チェックを行う。`body_log` は `http_step` が `cmd_args` から `--data-binary` 値を集めて生成する
- **ボディ（multipart）**は各パートを `--form-string "name=value"` または `-F "name=@"path";filename="...";type="...""` として `curl_command` に直接展開し、ファイル存在チェックも step 関数内で行う。`body_kind` / `body_form_text` のような中間表現は使わない。`body_log` は `http_step` が `cmd_args` から `-F` / `--form-string` 値を集めて生成し、`>` 行のタイミングでリクエストボディとして出力する。`==>` バナーより前に echo で出力してはならない（前ステップの出力に混入するため）
- **SLEEP ステップ**は `sleep <seconds>` を実行
- **capture** は `response.body.*` / プレフィックス無し JSON path、`response.header.*`、`request.header.*`、`request.url`、`request.body`、`request.body.<json.path>` をサポートする。JSON path は `jq` で抽出し、capture 結果は `VAR_<NAME>`（英数字と `_` 以外は `_` に正規化、英字は大文字化）として `export` する。capture 定義は `captures_text` のタブ区切りデータを経由せず、各 step 関数が `http_step` 呼び出しの後に `capture_response_body_json "..."` / `capture_request_response_header "..."` / `capture_value "..."` / `capture_request_body_json "..."` を直接呼び出す形で展開する
- **until** 指定ありの HTTP ステップは、各試行で通常のリクエスト・レスポンス出力・capture を実行した後に条件を評価する。条件が満たされると `* until satisfied on attempt N` を出力し、満たされない場合は `* until not satisfied (attempt N/M), retrying in Xs` を出力して `interval` 秒待つ。`max_attempts` で満たされなければ標準エラーに失敗理由を出力し非ゼロ終了する。`curl --fail` は使わないため HTTP 4xx/5xx は通常レスポンスとして扱い、capture や条件評価の対象になる
- `main()` は step 呼び出しを1行ずつ並べるだけ（スキップ・並べ替えがコメントアウトで容易）
- テンプレートファイルは使わず、`bashgen/` パッケージ（`bashgen/steps.py` 等）のコード内で完結して出力する
- `bash_generator.py` は単一のディスパッチ関数を持ち、`bashgen` パッケージの各モジュールに委譲する
- `${time.DATE_ISO}` / `${time.DATE_YMD}` / `${time.DATE_YMDHMS}` は `date` コマンドと shell 関数として展開する
- until ステップの inner attempt 関数名は `{fn}_attempt` として生成する。この名前は通常ステップの関数名と衝突しないよう、`bashgen/analysis.py` で予約する
- SLEEP ステップの name / description は shell injection を防ぐため `printf` + シングルクォートでデータとして出力する
- **変数の CLI 注入**: bash 形式の生成スクリプトは `--<name> <value>`（または `--<name>=<value>`）形式の引数で `${var.<NAME>}` 変数を注入できる。`main()` の引数解析で `--` 以降を取り出し、`-` → `_` 置換 → `awk` で大文字化 → `VAR_` 接頭辞付与、の変換を行い `printf -v` で代入する。予約フラグ（`--pretty-json` / `--no-mask` / `--blank-line` / `--help`）は先に match するためそちらが優先され、同名変数は `--<name>=<value>` 形式で回避できる。優先順位は CLI 引数 > 事前 export > `DEFAULT_VARS`。必須変数の不足検証は引数解析の**後**に実行する（§5.2.3）
- **`--embed-files` オプション（bash のみ）**:
  - `-f` で指定された TOML ファイルと同じディレクトリ基準でファイルパスを解決する
  - `body_file` または `body_multipart` のファイルフィールドのうち、リテラルパス（`${...}` プレースホルダを含まないもの）を生成時に読み込み Base64 エンコードする
  - `${var.*}` / `${env.*}` などを含むパスは埋め込み対象外とし、警告を出力した上で既存の実行時ファイル参照にフォールバックする
  - 埋め込まれた内容は `__HF_EMBED_<step_fn>_body` や `__HF_EMBED_<step_fn>_mp<N>` などの変数として宣言され、`_hf_b64decode` ヘルパー（base64 -d / -D の差異を吸収）で実行時に `$HF_TMPDIR` 配下の一時ファイルに復元される
  - マルチパート内の通常フィールド（`kind=field`）は埋め込み対象外
  - ファイルが存在しない場合は生成時にエラーで終了する
