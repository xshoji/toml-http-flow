# 7. 処理フロー

1. CLI引数のパース（`argparse` で `-f`, `-v`）
2. TOMLファイルの読み込み（`tomllib.load()` はバイナリモードで開く必要あり）
3. dictをデータクラスへ変換＋バリデーション
4. 変数ストアの初期化:
   ```python
   store = {"vars": {...}, "steps": {}}
   ```
5. 各ステップを順次実行:
   1. テンプレート展開（URL、ヘッダー、ボディ内の変数参照を解決）
   2. `urllib.request.urlopen()` でHTTPリクエストを送信
   3. レスポンスを受信＆JSONとしてパース（`Content-Type` 判定）
   4. `capture` 定義に従い、指定パスから値を抽出
   5. 抽出した値を `store["steps"][name][key]` に保存
6. 全ステップ完了後、サマリを標準出力に表示
