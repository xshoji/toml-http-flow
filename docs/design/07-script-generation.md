# 8. スクリプト生成機能 (`generate` サブコマンド)

ワークフロー TOML から、本ツールに**一切依存しない単一の Python スクリプト**を生成する。
証跡保存・他人への共有・CI/CD 組み込み等を想定し、生成スクリプトも **標準ライブラリのみ** で動作する。

## 8.1 設計方針

| 項目           | 方針                                                                     |
|----------------|--------------------------------------------------------------------------|
| 依存関係       | 生成スクリプトは Python 3.11+ 標準ライブラリのみ（本ツール本体は不要）   |
| 自己完結性     | 1ファイルで完結。必要なランタイム helper だけを httpflow/runtime/*.py から flatten して埋め込む |
| 可読性         | "監査用" のため、人間が読んで何をしているか追える構造にする                |
| 入力との対応   | コメントで「どの `[[requests]]` ブロック由来か」を明示                     |
| 変数注入       | `argparse` で `-v key=value` を受け付ける（本ツールと同じ）                |
| 再実行性       | 何度実行しても同じ振る舞い（副作用は対象 API に依存）                      |

## 8.2 生成スクリプトの構造

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

## 8.3 ランタイム flatten 方式

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
- `${repeat.*}` 参照があれば `runtime/repeat.py` を含める
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
| `repeat`  | --        |

解決順は常に `core → mask → http → until → repeat` とする。

## 8.4 生成アルゴリズム

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
   - `{{STEP_FUNCTIONS}}`: 各ステップ関数の定義（空行2つで区切り）
   - `{{STEP_CALLS}}`: `main()` 内に並べる `step_xxx(store, ...)` の列
   - `{{DEFAULT_VARS}}`: `-v` で渡されたデフォルト変数
   - `{{REQUIRED_VARS}}`: `${var.<key>}` で参照されているが `DEFAULT_VARS` に無い変数名
   - `{{DEFAULT_REPEAT_VARS}}`: `--repeat-vars` で渡されたデフォルト repeat 変数（辞書形式、値はリスト）
   - `{{GENERATED_AT}}`: 生成タイムスタンプ
   - `{{VERSION}}`: 本ツールのバージョン
   - `{{UNTIL_HELPERS}}`: `until` 使用時のみ `poll_until` を含むヘルパ群（未使用時は省略）
   - `{{REPEAT_HELPERS}}`: `${repeat.*}` 参照時のみヘルパ群（未使用時は省略）
   - `{{ARGPARSE_REPEAT}}`: `--repeat-vars` 引数の定義（未使用時は空文字）
   - `{{MAIN_REPEAT_SETUP}}`: repeat 使用時の反復処理、未使用時は `store['repeat'] = {}`
   - 生成スクリプトの `main()` は `-v` を `store["vars"]` に反映した直後、step 呼び出し前に `REQUIRED_VARS` の不足を検証する
6. 出力先（`-o` または stdout）に書き出す
7. `--shebang` 指定時は先頭に `#!/usr/bin/env python3` を付け、`chmod +x` 相当を実施

## 8.5 ヘルパ関数の二重実装と parity 担保

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
| `parse_repeat_args` / `build_repeat_iterations` | `runtime.repeat` | `runtime/repeat.py` 平滑化 |

本体側の `template.py` / `httpclient.py` / `masking.py` / `until.py` は、
原則として `httpflow.runtime.*` へ thin wrapper として delegate する。

## 8.6 生成スクリプトの使い方（生成後）

```bash
# 生成
python -m httpflow generate -f workflow.toml -o audit/workflow_2026-05-19.py

# どこでも実行（本ツールは不要）
python3 audit/workflow_2026-05-19.py
python3 audit/workflow_2026-05-19.py -v env=staging -v token=abc
python3 audit/workflow_2026-05-19.py --quiet     # 詳細出力を抑制（デフォルトは詳細ON）
```

## 8.7 セキュリティ・運用上の注意

- TOML 中にハードコードされた認証情報はそのまま埋め込まれるので、
  必要に応じて `-v` で上書きする運用を推奨（埋め込み値はあくまでデフォルト）
- 生成スクリプトは先頭に生成元コメント（`Generated by toml-http-flow ...`）を明記し、
  手で書き換えてしまった場合でも再生成方法が分かるようにする
- 機密値は生成スクリプトから除外するオプション（`--strip-secrets=KEY,KEY` 等）を将来追加検討
