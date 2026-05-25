# 3. アーキテクチャ

```
httpflow/
├── __init__.py
├── __main__.py          # `python -m httpflow` のエントリポイント
├── cli.py               # 引数パース・ディスパッチ・例外境界
├── config.py            # TOML読込み・バリデーション（出力は WorkflowSpec）
├── model.py             # WorkflowSpec / HttpStep / SleepStep / UntilSpec
├── runner.py            # ステップ実行エンジン＋変数ストア
├── embedded_runtime.py  # 生成スクリプトにも埋め込む helper の source-of-truth
├── generator.py         # WorkflowSpec → standalone .py emitter
├── httpclient.py        # urllib ベースの HTTP クライアント（embedded_runtime ラッパー）
├── template.py          # テンプレート展開エンジン（embedded_runtime ラッパー）
├── masking.py           # ログ出力用マスキング（embedded_runtime ラッパー）
├── until.py             # until 条件評価（embedded_runtime ラッパー）
├── workflow.py          # backward-compatible shim → runner
├── templates/
│   └── runner.py.tmpl   # 生成スクリプトの枠（placeholder のみ）
└── tests/
    ├── test_template.py
    ├── test_config.py
    ├── test_workflow.py
    ├── test_generator.py
    └── …
```

## 3.1 httpflow/config.py

- TOML を `tomllib.load()` でパース
- `dict → WorkflowSpec` への変換ヘルパを提供
- 不正なフィールド（`body` と `body_form` の同時指定、`SLEEP` に無関係なフィールドが付いている等）をバリデーション
- 出力は **正規化済み** の `WorkflowSpec`

```python
def load(path: str | Path) -> WorkflowSpec:
    """TOML を読み込み、検証済み WorkflowSpec を返す。"""
```

## 3.2 httpflow/model.py

- `config.py` が返す **正規化済みモデル** `WorkflowSpec` を定義する
- `SLEEP` を `method` の特殊値から独立した `SleepStep` として扱う
- `HttpStep` と `SleepStep` の Union を `Step` として定義
- `runner` と `generator` の共通入力として機能

```python
@dataclass
class HttpStep:
    name: str
    method: str
    url: str
    description: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: TextBody | FormBody | None = None
    capture: dict[str, str] = field(default_factory=dict)
    until: UntilSpec | None = None

@dataclass
class SleepStep:
    name: str
    seconds: str                     # テンプレート式、実行時に評価
    description: str | None = None

type Step = HttpStep | SleepStep

@dataclass
class WorkflowSpec:
    steps: list[Step] = field(default_factory=list)
```

## 3.3 httpflow/embedded_runtime.py

**設計上最重要**: 本体実行と生成スクリプトの両方で必要な helper の **source-of-truth**。

含む関数:

- `render` / `render_mapping`: テンプレート `${...}` 展開
- `extract`: JSON path 抽出
- `do_request`: HTTP 送受信（`urllib`）
- `eval_until`: until 条件評価
- `mask` / `mask_url` / `mask_value`: ログ出力用マスキング
- `build_repeat_iterations` / `parse_repeat_args`: repeat 展開
- `run_step`: 単一 HTTP/SLEEP ステップを render → send → log → capture まで一括実行
- `_now` / `_pretty` / `_log_request` / `_log_response`: 出力整形

**ルール**:

- `httpflow` 内の相対 import をしない
- 標準ライブラリ以外を import しない
- CLI / TOML parser / generator 固有処理を入れない
- モジュール単体のソースを生成スクリプトへ貼っても動く形にする

本体では `from .embedded_runtime import render, extract, run_step` として import して使う。
生成時は `embedded_runtime.py` のソーステキストをそのまま埋め込む。

## 3.4 httpflow/runner.py

- `WorkflowSpec` を受け取り、ステップを順次実行
- 各ステップ実行前にテンプレート展開 (`run_step` 内で実施)
- 実行後に `capture` の結果を変数ストアの `<key>` に保存
- 後続ステップで参照可能にする
- `repeat_vars` による反復実行
- `until` 条件付きポーリング対応
- **責務は「実行順序と store 更新」のみ**。出力整形はすべて `embedded_runtime.run_step` に委譲

## 3.5 httpflow/generator.py

薄い **emitter**。

- `WorkflowSpec` から step 関数を生成する
- `embedded_runtime.py` のソースをテンプレートへ埋め込む
- 生成後に `compile()` で構文検証
- **主要な** ランタイム実装文字列を持たない（共通 helper は `embedded_runtime.py` を source-of-truth とする）

## 3.6 httpflow/workflow.py

後方互換シム。`from .runner import collect_repeat_names, run` のみエクスポート。
既存テストや外部コードからの `from httpflow.workflow import run` 等を維持する。

## 3.7 httpflow/cli.py

- `argparse` で `-f`, `-v`, `--repeat-vars` をパース
- `-v key=value` を複数回受け取り `vars` 名前空間に格納
- `config.load()` で `WorkflowSpec` を読み込み、`runner.run()` / `generator.generate()` に渡す
- 例外をキャッチして非ゼロ終了コードで終了
