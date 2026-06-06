# 00 Refactoring Plan

## 目的

機能追加により増えた重複実装と責務混在を整理し、現在の要件に合わせてコード全体を段階的に再設計する。

最終的な方針は次の通り。

- TOML を正規化済みの `WorkflowSpec` に変換し、実行と生成の両方が同じ spec を使う
- 実行時ランタイム helper を source-of-truth にする
- 生成スクリプトには同じ helper のソースコードを埋め込み、自己完結性を維持する
- `generator.py` は薄い emitter にし、長いランタイム実装文字列を持たない
- 本体実行と生成スクリプト実行の挙動差分を parity test で検出する

## 前提・制約

- Python 3.11+ 標準ライブラリのみを使う
- 生成スクリプトは `httpflow` パッケージを import しない
- 仕様変更を伴う場合は `docs/design.md` および `docs/design/` を先に更新する
- 既存 CLI の互換性を維持する
  - `run` 省略時は `run` として扱う
  - 既存 TOML フィールドの意味を変えない
- 変更は小さなステップに分割し、各ステップでテスト可能にする

## 目標アーキテクチャ

```text
httpflow/
  cli.py              # argparse と例外境界
  config.py           # TOML -> WorkflowSpec 変換・検証
  model.py            # WorkflowSpec / HttpStep / SleepStep など
  runner.py           # 実行順序、store 更新、step 分岐
  embedded_runtime.py # 生成スクリプトにも埋め込む helper の source-of-truth
  generator.py        # WorkflowSpec -> standalone .py emitter
  templates/
    runner.py.tmpl    # 生成スクリプトの枠のみ
```

将来的に `embedded_runtime.py` が大きくなった場合は、以下のように分割する余地を残す。

```text
httpflow/runtime/
  templating.py
  transport.py
  extract.py
  until.py
  masking.py
  reporting.py
```

ただし初期リファクタリングでは、埋め込み順序や import 除去の複雑化を避けるため、まずは単一の `embedded_runtime.py` を優先する。

## 基本設計

### 1. 実行と生成の共通入力

`config.py` は TOML を読み込み、検証済み・デフォルト補完済みの `WorkflowSpec` を返す。

```text
TOML file
  -> config.load_workflow(...)
  -> WorkflowSpec
       -> runner.run(...)
       -> generator.generate(...)
```

生成スクリプトは TOML parser を持たない。生成時点の `WorkflowSpec` を Python コードとして出力する。

### 2. 埋め込みランタイム

`embedded_runtime.py` は、本体実行と生成スクリプトの両方で必要な関数だけを含む。

候補:

- `render`
- `extract`
- `do_request`
- `evaluate_until`
- `mask_value` / `mask_url`
- request / response 表示補助
- request / response 表示補助

本体実行では import して使う。

```python
from .embedded_runtime import render, extract, do_request
```

生成時は `embedded_runtime.py` のソースを読み込み、生成スクリプトへ貼り付ける。

```text
runner.py.tmpl
  {{EMBEDDED_RUNTIME}}
  {{STEP_FUNCTIONS}}
  {{STEP_CALLS}}
```

### 3. 埋め込みランタイムのルール

- `httpflow` 内の相対 import をしない
- 標準ライブラリ以外を import しない
- CLI / TOML parser / generator 固有処理を入れない
- モジュール単体のソースを生成スクリプトへ貼っても動く形にする
- 生成スクリプト側でも必要な import はこのファイル内に閉じる

### 4. generator の責務

`generator.py` は以下に限定する。

- `WorkflowSpec` から step 関数を生成する
- step 呼び出し順を生成する
- default vars などの静的データを `repr` / `json.dumps` で安全に出力する
- `embedded_runtime.py` のソースをテンプレートへ埋め込む
- 生成後に `compile()` で構文検証する

`generator.py` に `render` / `extract` / `do_request` 相当の長い文字列実装を置かない。

## リファクタリング手順

### Phase 0: 現状固定と安全網

- [ ] 現在の主要挙動を確認する smoke test を整理する
- [ ] `run` と `generate` の既存テストが通る状態を確認する
- [ ] 生成スクリプトを実際に `subprocess` で実行する parity test の土台を確認・追加する

完了条件:

- `python3 -m unittest discover -s tests -v` が通る
- 既存機能の失敗を検出できるテストがある

Phase 1: embedded_runtime.py の導入

- [x] httpflow/embedded_runtime.py を追加する
- [x] まず render とテンプレート用定数を移す
- [x] 本体実行側を embedded_runtime.render に寄せる
- [x] 生成テンプレート側の手書き render を削除し、generator が embedded_runtime.py を埋め込む
- [x] render の本体実行と生成スクリプト実行の parity test を追加する

完了条件:

- [x] render の実装が1箇所になる
- [x] 生成スクリプトが httpflow を import せずに動く
- [x] 既存テストが通る

Phase 2: extract の共通化

- [x] 現在の本体側 extract とテンプレート側 extract の差分を確認する
- [x] extract を embedded_runtime.py に移す
- [x] 本体 HTTP 処理側を共通 extract に寄せる
- [x] 生成テンプレート側の手書き extract を削除する
- [x] JSON path / header / body capture の parity test を追加する

完了条件:

- [x] extract の仕様変更が1箇所で済む
- [x] 本体実行と生成スクリプトで capture 結果が一致する

Phase 3: HTTP 送信処理の共通化

- [x] do_request 相当の責務を整理する
- [x] request 組み立て
- [x] body encoding
- [x] response decode
- [x] HTTP error response の扱い
- [x] 生成スクリプトに埋め込める形で embedded_runtime.py に移す
- [x] 本体実行側を共通 do_request に寄せる
- [x] 生成テンプレート側の手書き HTTP 処理を削除する
- [x] ローカル http.server を使った parity test を追加する

完了条件:

- [x] HTTP 送信・response 取り扱いの中核実装が1箇所になる
- [x] 外部APIに依存しないテストで同等性を確認できる

Phase 4: until / masking / reporting の整理

- [x] until 条件評価を共通 helper に寄せる
- [x] masking 処理を共通 helper に寄せる
- [x] request / response 表示整形を runner から分離する
- [x] verbose / pretty-json / no-mask の parity test を追加する

完了条件:

- [x] runner は実行順序と store 更新に集中している
- [x] 出力仕様の変更箇所が明確になっている

Phase 5: WorkflowSpec モデル導入

- [x] httpflow/model.py を追加する
- [x] WorkflowSpec, HttpStep, SleepStep, UntilSpec を定義する
- [x] 既存 config dataclass との互換性を確認しながら段階移行する
- [x] SLEEP を HTTP method の特殊値ではなく SleepStep として扱う
- [x] body / body_form の相互排他をモデルで表現する

完了条件:

- [x] config.py の出力が正規化済み model になる
- [x] runner / generator が同じ model を入力にする
- [x] 既存 TOML は引き続き動く

Phase 6: runner / generator の薄型化

- [x] workflow.py の責務を runner.py に整理する
- [x] runner は step 種別分岐、store 更新、実行順序に集中させる
- [x] generator.py から長いランタイム文字列を削除する
- [x] runner.py.tmpl を枠だけにする
- [x] 生成後 compile() 検証を維持する

完了条件:

- [x] generator は emitter として読めるサイズ・責務になっている
- [x] template は placeholder 中心で、実装ロジックをほぼ持たない

### Phase 7: ドキュメント更新と仕上げ

- [ ] `docs/design/02-architecture.md` を新構成に合わせて更新する
- [ ] `docs/design/06-workflow-flow.md` を `WorkflowSpec` ベースに更新する
- [ ] `docs/design/07-script-generation.md` に埋め込みランタイム方式を明記する
- [ ] `docs/design/09-testing.md` に parity test 方針を明記する
- [ ] 古い責務説明や二重実装前提の記述を削除する

完了条件:

- design doc と実装の責務が一致している
- 今後の機能追加時に参照すべき設計が明確になっている

## 推奨コミット単位

1. `test: add generator parity safety net`
2. `refactor: introduce embedded runtime for template rendering`
3. `refactor: share extract logic with generated scripts`
4. `refactor: share HTTP request runtime with generated scripts`
5. `refactor: consolidate until repeat and masking helpers`
6. `refactor: introduce workflow model`
7. `refactor: slim down generator and runner template`
8. `docs: update architecture for embedded runtime design`

## 検証コマンド

基本:

```bash
python3 -m unittest discover -s tests >/tmp/amp-test.log 2>&1 && echo OK || tail -n 80 /tmp/amp-test.log
```

CLI smoke:

```bash
python3 -m httpflow --help
python3 -m httpflow run --help
python3 -m httpflow generate --help
```

生成スクリプト構文検証:

```bash
python3 -m httpflow generate -f <some.toml> -o /tmp/httpflow-generated.py
python3 -c "import py_compile; py_compile.compile('/tmp/httpflow-generated.py', doraise=True)"
```

## 注意点

- 最初から全 helper を一気に移さない
- `embedded_runtime.py` に config / cli / generator の都合を混ぜない
- ソース埋め込みのために複雑な AST 変換を導入しない
- 生成スクリプトの自己完結性を常にテストする
- 本体実行と生成実行の差分は unit test ではなく parity test で確認する
