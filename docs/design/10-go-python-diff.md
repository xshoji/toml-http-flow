# 10. Go版との相違点

| 観点         | Go版                          | Python版                                  |
|--------------|-------------------------------|-------------------------------------------|
| 設定ファイル | YAML（外部ライブラリ必要）    | TOML（`tomllib` 標準）                    |
| 配列定義     | YAMLの `-` リスト              | TOMLの `[[requests]]` 配列テーブル        |
| HTTPクライアント | `net/http`                | `urllib.request`                          |
| ビルド成果物 | 単一バイナリ                  | スクリプト or `pip install` 配布           |
| 起動コマンド | `./api-workflow-cli`          | `python -m httpflow` / `httpflow`               |
| 型システム   | 構造体＋静的型                | `@dataclass` + `typing`                   |
