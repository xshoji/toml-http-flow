# AGENTS.md

AI エージェント向けのプロジェクト固有指示。
コードを変更する前に必ず一読すること。

## プロジェクト概要

- 名前: `httpflow` (パッケージ・CLI 名) / `toml-http-flow` (リポジトリ)
- 種別: CLI ツール
- 機能: TOML で定義した HTTP ワークフローを順次実行 / 単一 .py 生成
- 仕様書: [docs/design.md](docs/design.md) が **唯一の真実の源**。仕様変更時はここを先に更新する。

## 絶対条件

1. **依存ゼロ**: 本体・テスト・生成スクリプトすべて **Python 3.11+ 標準ライブラリのみ**で実装する。
   `requests` / `pydantic` / `pytest` / `httpx` などを追加してはならない。
2. **Python 3.11+ 必須**: `tomllib` 標準同梱が前提。`tomli` などのバックポートに依存しない。
3. **生成スクリプトの自己完結性**: `httpflow/templates/runner.py.tmpl` から生成される .py は
   `httpflow` パッケージを import せずに単体で動作しなければならない。
4. **設計書との同期**: `docs/design.md` と実装の挙動は一致させる。
   仕様を変える時は設計書 → 実装 → テストの順で更新する。

## ディレクトリ責務

| パス | 責務 | 編集時の注意 |
|------|------|--------------|
| [httpflow/config.py](httpflow/config.py) | TOML → `@dataclass` 変換 / バリデーション | `parse_kv_list` の挙動を変える時は設計書 §4.4 も更新 |
| [httpflow/template.py](httpflow/template.py) | `${...}` 展開 / `$$` エスケープ | 正規表現 `PATTERN` は generator の同等品と一致させる |
| [httpflow/httpclient.py](httpflow/httpclient.py) | `urllib` で HTTP送信 / JSONパス抽出 | `extract()` のロジックは generator にも反映 |
| [httpflow/workflow.py](httpflow/workflow.py) | ステップ実行 / 変数ストア管理 | `store = {"vars": ..., "steps": ...}` の構造は固定 |
| [httpflow/cli.py](httpflow/cli.py) | `argparse` ディスパッチ | `run` 省略時に `run` 扱いとする後方互換を保つ |
| [httpflow/generator.py](httpflow/generator.py) | TOML → 単一 .py 生成 | 出力は必ず `compile()` で構文検証可能なこと |
| [httpflow/templates/runner.py.tmpl](httpflow/templates/runner.py.tmpl) | 生成スクリプトのベース | プレースホルダ `{{REQUESTS}}` `{{DEFAULT_VARS}}` `{{VERSION}}` `{{GENERATED_AT}}` のみ置換 |
| [tests/](tests/) | `unittest` ベースのテスト | `http.server` でローカルモックを立てる方式を踏襲 |

## ランタイムヘルパの二重実装について

`render` / `extract` / `do_request` 相当の関数は **本体** と **生成スクリプトのテンプレート** の
両方に存在する（DRY より自己完結性を優先する設計判断）。

片方を直したらもう片方も必ず追従させ、テスト (`tests/test_generator.py`) で
ロジック同等性を担保すること。

## テスト

```bash
# 全テスト実行（標準ライブラリの unittest discover を使う）
python3 -m unittest discover -s tests -v
```

- 新機能を追加したら必ず対応するテストを足す
- HTTP 通信を伴うテストは外部APIを叩かず、`http.server.HTTPServer` でローカルモックを立てる
- 生成スクリプトのテストは `subprocess` で実際に `python3 generated.py` を実行して挙動を検証する

## 動作確認

実装変更後は最低限以下を確認すること:

```bash
# 1. テスト
python3 -m unittest discover -s tests >/tmp/amp-test.log 2>&1 && echo OK || tail /tmp/amp-test.log

# 2. CLI ヘルプが壊れていないか
python3 -m httpflow --help
python3 -m httpflow run --help
python3 -m httpflow generate --help

# 3. generate が構文的に valid な .py を出すか
python3 -m httpflow generate -f <some.toml> -o /tmp/g.py
python3 -c "import py_compile; py_compile.compile('/tmp/g.py', doraise=True)"
```

## コーディング規約

- 型ヒント必須 (`from __future__ import annotations` を使う)
- `@dataclass` を積極的に使う
- public 関数は1行 docstring を付ける
- 例外メッセージは英語で簡潔に、原因と対象を含める
- print は `file=sys.stderr` でエラー出力を分ける
- パスは `pathlib.Path` を優先

## やってはいけないこと

- 外部ライブラリの追加（`pyproject.toml` の `dependencies` は空に保つ）
- `tomli` / `requests` / `httpx` などの import
- `pytest` への移行（`unittest` 縛り）
- `httpflow` 本体を import する形での生成スクリプト書き出し
- 設計書を更新せずに公開仕様（CLI引数・TOMLフィールド・テンプレート記法）を変更すること
- 設計書には書かれていないフィールドの黙認追加（拡張時は §12「拡張余地」と整合させる）

## 拡張時の判断基準

新機能を入れたい時は [docs/design.md §12 拡張余地](docs/design.md) のリストを確認する。
そこに無い項目は、まず設計書を更新する PR と分けるか、同一 PR 内でも先に設計書セクションを書くこと。

## コミット運用

- 1コミット = 1論理的変更
- メッセージは [Conventional Commits](https://www.conventionalcommits.org/) に従う
  (`feat:` / `fix:` / `refactor:` / `docs:` / `test:` / `chore:` 等)
- ユーザーから明示の指示がない限り `git commit` / `git push` を勝手に実行しない
