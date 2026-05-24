# 7. 処理フロー

1. CLI 引数のパース (`argparse` で `-f`, `-v`, `--repeat-vars`)
2. TOML ファイルの読み込み (`tomllib.load()` はバイナリモードで開く必要あり)
3. dict を `WorkflowConfig` に変換＋バリデーション (`config.py`)
4. `WorkflowConfig` → `WorkflowSpec` へ正規化 (`model.from_config()`)
   - `method = "SLEEP"` を `SleepStep` へ変換
   - `body` と `body_form` の相互排他はモデル型で表現
5. 変数ストアの初期化:
   ```python
   store = {"vars": {...}, "repeat": {}}
   ```
6. `${repeat.<name>}` 参照の検出と `--repeat-vars` の検証:
   1. `WorkflowSpec` 中の `${repeat.X}` を全て列挙
   2. 必要な name が `--repeat-vars` で揃っているか確認（不足はエラー）
   3. 複数の `--repeat-vars` がある場合、カンマ分割した要素数が一致するか確認
   4. 反復回数 `N` を決定（参照も指定も無ければ `N=1`）
7. `N` 回のループ:
   1. `store["repeat"]` を今回の反復値で更新
    2. `runner._run_once(spec, store, ...)` で各ステップを順次実行 (内部関数):
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
