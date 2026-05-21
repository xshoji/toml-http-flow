# 7. 処理フロー

1. CLI引数のパース（`argparse` で `-f`, `-v`, `--repeat-vars`）
2. TOMLファイルの読み込み（`tomllib.load()` はバイナリモードで開く必要あり）
3. dictをデータクラスへ変換＋バリデーション
4. 変数ストアの初期化:
   ```python
   store = {"vars": {...}, "steps": {}, "repeat": {}}
   ```
5. `${repeat.<name>}` 参照の検出と `--repeat-vars` の検証:
   1. TOML中の `${repeat.X}` を全て列挙
   2. 必要な name が `--repeat-vars` で揃っているか確認（不足はエラー）
   3. 複数の `--repeat-vars` がある場合、カンマ分割した要素数が一致するか確認
   4. 反復回数 `N` を決定（参照も指定も無ければ `N=1`）
6. `N` 回のループ:
   1. `store["repeat"]` を今回の反復値で更新
   2. 2回目以降のループでは `store["steps"]` をクリア
   3. 各ステップを順次実行:
      1. テンプレート展開（URL、ヘッダー、ボディ内の変数参照を解決）
      2. `urllib.request.urlopen()` でHTTPリクエストを送信
      3. レスポンスを受信＆JSONとしてパース（`Content-Type` 判定）
      4. `capture` 定義に従い、指定パスから値を抽出
      5. 抽出した値を `store["steps"][name][key]` に保存
7. 全ステップ完了後、サマリを標準出力に表示
