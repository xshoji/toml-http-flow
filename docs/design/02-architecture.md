# 3. アーキテクチャ

```
httpflow/
├── __init__.py
├── __main__.py          # `python -m httpflow` のエントリポイント
├── cli.py               # 引数パース・ディスパッチ・例外境界
├── config.py            # TOML読込み・バリデーション（出力は WorkflowSpec）
├── model.py             # WorkflowSpec / HttpStep / SleepStep / UntilSpec
├── runner.py            # ステップ実行エンジン＋変数ストア
├── generator.py         # WorkflowSpec → standalone .py emitter
├── template.py          # テンプレート変数名抽出（runtime.core への thin wrapper）
├── bash_generator.py    # WorkflowSpec → standalone .sh emitter（bashgen パッケージへのディスパッチ）
├── bashgen/             # bash スクリプト生成パッケージ
│   ├── __init__.py
│   ├── analysis.py      # ワークフロー解析・機能検出
│   ├── capture.py       # capture 定義の bash コード生成
│   ├── conditions.py    # until 条件式の bash コード生成
│   ├── names.py         # 変数名・関数名の正規化
│   ├── placeholders.py  # ${time.*} / ${random.*} 等のプレースホルダ置換
│   ├── runtime.py       # ランタイム helper 関数群の生成
│   ├── script.py        # スクリプト全体の組み立て
│   ├── shell.py         # シェルエスケープ・引用符ユーティリティ
│   └── steps.py         # 各ステップ関数のコード生成
├── runtime/             # 本体と生成スクリプトの両方で使う helper
│   ├── __init__.py
│   ├── core.py          # render / render_mapping / TemplateError
│   ├── mask.py          # mask / mask_url / mask_value
│   ├── http.py          # do_request / extract / run_step / ログ出力
│   └── until.py         # eval_until / poll_until
└── templates/
    └── runner.py.tmpl   # 生成スクリプトの枠（placeholder のみ）
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

Step: TypeAlias = HttpStep | SleepStep

@dataclass
class WorkflowSpec:
    steps: list[Step] = field(default_factory=list)
```

## 3.3 httpflow/runtime/（本体＋生成スクリプトの共通 helper）

本体実行と生成スクリプトの両方で必要な helper を、機能別にファイル分割して保持する。
`generator.py` はワークフローに応じて必要なファイルだけ選び、フラット化（`from __future__` と相対 import 行を除去）して生成スクリプトに埋め込む。

| ファイル | 提供する機能 | 依存 |
|---------|------------|------|
| `runtime/core.py` | `render` / `render_mapping` / `TemplateError` | -- |
| `runtime/mask.py` | `mask` / `mask_url` / `mask_value` | -- |
| `runtime/http.py` | `do_request` / `extract` / `run_step` / ログ出力 | `core`, `mask` |
| `runtime/until.py` | `eval_until` / `poll_until` | `core` |

**ルール**:

- 各モジュールは対応する `runtime/*.py` 同士の相対 import のみを行う
- 標準ライブラリ以外を import しない
- CLI / TOML parser / generator 固有処理を入れない
- フラット化時に `from __future__ import annotations` と相対 import 行は除去される
- docstring も除去される（生成スクリプトのサイズ削減のため）
- 重複する stdlib import は許容する（AST 変換等の複雑な処理を避ける）

本体では `from .runtime.core import render` や `from .runtime.http import run_step` として import して使う。
生成時は `runtime/*.py` のソーステキストを選んでフラット化し、`{{RUNTIME_HELPERS}}` に埋め込む。

## 3.4 httpflow/runner.py

- `WorkflowSpec` を受け取り、ステップを順次実行
- 各ステップ実行前にテンプレート展開 (`run_step` 内で実施)
- 実行後に `capture` の結果を変数ストアの `<key>` に保存
- 後続ステップで参照可能にする
- `until` 条件付きポーリング対応
- **責務は「実行順序と store 更新」のみ**。出力整形はすべて `runtime.http.run_step` に委譲

## 3.5 httpflow/generator.py

薄い **emitter**。

- `WorkflowSpec` から step 関数を生成する
- `WorkflowSpec` を解析して必要な `runtime/*.py` を選び、テンプレートへ flatten して埋め込む
- 生成後に `compile()` で構文検証
- **主要な** ランタイム実装文字列を持たない（共通 helper は `httpflow/runtime/*.py` を source-of-truth とする）

## 3.6 httpflow/bashgen/（bash スクリプト生成パッケージ）

`--format bash` 指定時に使用される bash スクリプト生成エンジン。
`httpflow/bash_generator.py` がディスパッチ元となり、`bashgen` パッケージ内の各モジュールに処理を委譲する。

| モジュール | 責務 |
|-----------|------|
| `bashgen/analysis.py` | ワークフロー解析：capture／until／form／template の要否検出 |
| `bashgen/capture.py` | capture 定義 → bash 変数 export コード |
| `bashgen/conditions.py` | until 条件式 → bash `if ... then` コード |
| `bashgen/names.py` | 変数名の正規化（`VAR_<NAME>` 形式） |
| `bashgen/placeholders.py` | `${time.*}` / `${random.*}` 等 → bash コード |
| `bashgen/runtime.py` | mask／uuid／capture_* 等のランタイム helper 関数群の生成 |
| `bashgen/shell.py` | シェルエスケープ・引用符ユーティリティ |
| `bashgen/steps.py` | 各 step 関数のコード生成 |
| `bashgen/script.py` | スクリプト全体の組み立て |

## 3.7 httpflow/cli.py

- `argparse` で `-f`, `-v`, `-s`, `-q`, `--pretty-json`, `--no-mask`, `--blank-line` をパース
- `-v key=value` を複数回受け取り `vars` 名前空間に格納
- `config.load()` で `WorkflowSpec` を読み込み、`runner.run()` / `generator.generate()` に渡す
- `generate` サブコマンドは `--format python`（既定）または `--format bash` を選択可能
- 例外をキャッチして非ゼロ終了コードで終了
