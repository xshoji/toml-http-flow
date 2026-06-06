# 7. 処理フロー

1. CLI 引数のパース (`argparse` で `-f`, `-v`)
2. TOML ファイルの読み込み (`tomllib.load()` はバイナリモードで開く必要あり)
3. dict を検証しつつ `WorkflowSpec` に変換 (`config.py`)
   - `method = "SLEEP"` を `SleepStep` へ変換
   - `body` と form body の相互排他をモデル型で表現
5. 変数ストアの初期化:
   ```python
   store = {"vars": {...}}
   ```
6. `${var.<name>}` 参照の検出と `-v/--var` の検証:
   1. `WorkflowSpec` 中の明示的な `${var.X}` を全て列挙
   2. 必要な name が `store["vars"]` に揃っているか確認（不足はエラー）
   3. この検証は最初の step 実行前に行い、不足時は HTTP リクエストを送らない
7. `runner._run_once(spec, store, ...)` で各ステップを順次実行 (内部関数):
   1. ステップ種別分岐 (
         - `SleepStep` → `run_step(method="SLEEP", ...)`
         - `HttpStep` (until なし) → `run_step(...)` 一回実行
         - `HttpStep` (until あり) → `run_step(...)` を `until` 条件が真になるまで繰り返し
      )
   2. `run_step` 内で一括処理:
         - テンプレート展開（URL、ヘッダー、ボディ内の変数参照を解決）
         - `urllib.request.urlopen()` で HTTP リクエストを送信
         - レスポンスを受信 & JSON としてパース
         - `capture` 定義に従い、指定パスから値を抽出
         - 抽出した値を `store["vars"][key]` に保存
         - リクエスト / レスポンスのログ出力
8. 全ステップ完了後、最終 `store` を返却
