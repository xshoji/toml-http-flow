# API Workflow CLI Tool 設計書（Python版）

> 各章はトピックごとに分割して独立ファイルに格納しています。
> AIが参照したい箇所だけ読み込むことで、コンテキストを削減できます。

## 目次

| # | トピック | ファイル | 内容要約 |
|---|----------|----------|----------|
| 1 | 概要・機能要件 | [design/01-overview.md](design/01-overview.md) | ツールの目的、Python標準ライブラリのみの方針、機能一覧 |
| 2 | アーキテクチャ | [design/02-architecture.md](design/02-architecture.md) | ディレクトリ構成、各モジュールの責務とインターフェース |
| 3 | TOML仕様 | [design/03-toml-spec.md](design/03-toml-spec.md) | `[[requests]]` ブロックのフィールド定義、`SLEEP` / `until` の特殊構文 |
| 4 | テンプレート記法 | [design/04-template.md](design/04-template.md) | `${...}` 変数参照、`$$` エスケープ、実装方針 |
| 5 | CLIインターフェース | [design/05-cli.md](design/05-cli.md) | `run` / `generate` サブコマンド、出力フォーマット、マスキング |
| 6 | 処理フロー | [design/06-workflow-flow.md](design/06-workflow-flow.md) | 実行時の6ステップフロー |
| 7 | スクリプト生成 | [design/07-script-generation.md](design/07-script-generation.md) | `generate` の設計、生成スクリプト構造、セキュリティ注意 |
| 8 | エラーハンドリング | [design/08-error-handling.md](design/08-error-handling.md) | 各種エラーと対応方針 |
| 9 | テスト方針 | [design/09-testing.md](design/09-testing.md) | `unittest` のカバレージ方針 |
| 10 | Go版との相違 | [design/10-go-python-diff.md](design/10-go-python-diff.md) | 言語間の設計比較 |
| 11 | 拡張余地 | [design/11-extension-points.md](design/11-extension-points.md) | 将来の機能追加候補 |

---

## 絶対要件（全文共通）

- **Python 3.11+ 標準ライブラリのみ**（外部依存ゼロ）
- `tomllib` が前提なので Python 3.11 未満には対応しない
- 生成スクリプトも `httpflow` パッケージに依存してはいけない（自己完結）
