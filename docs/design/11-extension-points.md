# 12. 拡張余地（将来）

- リトライ・タイムアウト設定（`urllib` の `timeout` 引数で対応可）
- ステップ単位の `enabled` フラグ
- `assert` フィールドでレスポンス検証
- `--dry-run` モード（テンプレート展開後のリクエストだけ出力）
- 並列実行モード（`concurrent.futures` で実装可能）
- 特殊ステップの追加 (`SLEEP` 以外の制御フロー等)
- `until` の condition で数値比較（`>`, `<`, `>=`, `<=`）や論理演算（`&&`, `||`）
- `generate --strip-secrets` で機密ヘッダー/フィールドを除外して書き出し
- `generate --format curl` で curl コマンド列としても書き出せるようにする
