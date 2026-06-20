# 9. テスト方針

`unittest`（標準ライブラリ）で以下をカバー:

## 9.1 単体テスト

- **template**: 各種記法の展開、ネスト参照、未定義変数のエラー
- **config**: 正常 TOML のパース、排他フィールドのバリデーション
- **runtime.http**: `http.server` でローカルモックを起動し、E2E で検証
- **runner**: 複数ステップ間の変数受け渡し、SLEEP ステップ
- **until**: `==` / `!=` / `~` / `in` 各オペレータの評価
- **masking**: ヘッダー・ボディ・URL クエリ・capture 値のマスキング

## 9.2 Parity Test（本体実行 vs 生成スクリプト実行）

`httpflow/runtime/*.py` を修正した場合、**本体実行と生成スクリプト実行で結果が一致することを担保する**。

### 実施方法

`test_generator.py` で `exec(script, ns)` として生成スクリプトの `render` / `extract` / `run_step` を ns 経由で取得し、
本体側 `runtime` モジュールの同名関数と `assertEqual` する。

```python
ns = {"__name__": "generated_parity_test"}
exec(script, ns)
for text in cases:
    self.assertEqual(ns["render"](text, store), render(text, store))
```

### 対象範囲

| helper      | parity test 内容                                      |
|-------------|-------------------------------------------------------|
| `render`    | `${var.x}` / `${env.X}` / `${random.UUID}` / `$$` 等  |
| `extract`   | JSON path: `data.items[0].id`                         |
| `run_step`  | HTTP 送受信・capture・ログ出力・マスキング            |
| `eval_until`| `==` / `!=` / `~` / `in` 各ケース                    |
| `mask*`     | JSON ボディ / URL クエリ / ヘッダー値のマスキング     |

外部 API には依存せず、`http.server.HTTPServer` によるローカルモックサーバで検証する。

## 9.3 CLI Smoke Check

変更後は以下を必ず実行:

```bash
# 全テスト
python3 -m unittest discover -s tests -v

# CLI help
python3 -m httpflow --help
python3 -m httpflow run --help
python3 -m httpflow generate --help

# 生成スクリプトの構文検証
python3 -m httpflow generate -f <some.toml> -o /tmp/g.py
python3 -c "import py_compile; py_compile.compile('/tmp/g.py', doraise=True)"
```

## 9.4 自己完結性の担保

生成スクリプトは以下の観点で検証する:

- `python3 generated.py` が `httpflow` パッケージを import せずに動く
- `--no-mask` / `--pretty-json` 等のフラグが生成スクリプトでも有効
