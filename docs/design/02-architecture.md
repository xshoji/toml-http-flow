# 3. アーキテクチャ

```
httpflow/
├── __init__.py
├── __main__.py          # `python -m httpflow` のエントリポイント
├── cli.py               # 引数パース・ディスパッチ・例外境界
├── config.py            # TOML読込み・バリデーション（出力は WorkflowSpec）
├── model.py             # WorkflowSpec / HttpStep / SleepStep / UntilSpec
├── runner.py            # ステップ実行エンジン＋変数ストア
├── embedded_runtime.py  # 旧 monolithic runtime の互換 shim（非推奨）
├── generator.py         # WorkflowSpec → standalone .py emitter
├── httpclient.py        # urllib ベースの HTTP クライアント（runtime.http ラッパー）
├── template.py          # テンプレート展開エンジン（runtime.core ラッパー）
├── masking.py           # ログ出力用マスキング（runtime.mask ラッパー）
├── until.py             # until 条件評価（runtime.until ラッパー）
├── workflow.py          # backward-compatible shim → runner
├── runtime/             # 本体と生成スクリプトの両方で使う helper
│   ├── __init__.py
│   ├── core.py          # render / render_mapping / TemplateError
│   ├── mask.py          # mask / mask_url / mask_value
│   ├── http.py          # do_request / extract / run_step / ログ出力
│   ├── until.py         # eval_until / poll_until
│   └── repeat.py        # parse_repeat_args / build_repeat_iterations
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

## 3.3 httpflow/runtime/（本体＋生成スクリプトの共通 helper）

本体実行と生成スクリプトの両方で必要な helper を、機能別にファイル分割して保持する。
`generator.py` はワークフローに応じて必要なファイルだけ選び、フラット化して生成スクリプトに埋め込む。

| ファイル | 提供する機能 | 依存 |
|---------|------------|------|
| `runtime/core.py` | `render` / `render_mapping` / `TemplateError` | -- |
| `runtime/mask.py` | `mask` / `mask_url` / `mask_value` | -- |
| `runtime/http.py` | `do_request` / `extract` / `run_step` / ログ出力 | `core`, `mask` |
| `runtime/until.py` | `eval_until` / `poll_until` | `core` |
| `runtime/repeat.py` | `parse_repeat_args` / `build_repeat_iterations` | -- |

**ルール**:

- 各モジュールは対応する `runtime/*.py` 同士の相対 import のみを行う
- 標準ライブラリ以外を import しない
- CLI / TOML parser / generator 固有処理を入れない
- フラット化時に `from __future__ import annotations` と相対 import 行は除去される

本体では `from .runtime.core import render` や `from .runtime.http import run_step` として import して使う。
生成時は `runtime/*.py` のソーステキストを選んでフラット化し、`{{RUNTIME_HELPERS}}` に埋め込む。

## 3.4 httpflow/embedded_runtime.py（互換 shim）

旧 monolithic runtime。現在は `httpflow.runtime.*` への re-export shim として残しており、
外部コードからの `from httpflow.embedded_runtime import render` 等を維持している。

## 3.5 httpflow/runner.py

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
- `WorkflowSpec` を解析して必要な `runtime/*.py` を選び、テンプレートへ flatten して埋め込む
- 生成後に `compile()` で構文検証
- **主要な** ランタイム実装文字列を持たない（共通 helper は `httpflow/runtime/*.py` を source-of-truth とする）

## 3.6 httpflow/workflow.py

後方互換シム。`from .runner import collect_repeat_names, run` のみエクスポート。
既存テストや外部コードからの `from httpflow.workflow import run` 等を維持する。

## 3.7 httpflow/cli.py

- `argparse` で `-f`, `-v`, `--repeat-vars` をパース
- `-v key=value` を複数回受け取り `vars` 名前空間に格納
- `config.load()` で `WorkflowSpec` を読み込み、`runner.run()` / `generator.generate()` に渡す
- 例外をキャッチして非ゼロ終了コードで終了
