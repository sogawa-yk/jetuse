"""生成専用モデルレジストリ(SP3-06 / specs/19 §4.1 F2・§4.5)。

フロント生成(OpenCode バックエンド)で選べる LLM の単一真実源。**共用 MODELS(models.py)とは
分離する** — gpt-5 系は ORASEJAPAN 共有テナンシ限定のため、共用レジストリに足すとチャット UI に
漏れて自テナンシで 404 になる(tasks/SP3-06)。

- api: OpenCode 生成で使う OpenAI 互換経路。**gpt-5 系は全て responses** — codex/pro 系は
  chat/completions 自体が 404(probe 2026-07-08)、chat 対応の gpt-5.5/5.6 系も function tools +
  reasoning_effort を chat/completions が拒否し「use /v1/responses instead」を返す(E2E 実測
  2026-07-08 — OpenCode は常に tool 呼び出しを使う)。chat = 自テナンシ gpt-oss-120b のみ。
- shared: True = ORASEJAPAN 共有テナンシ(課金も共有テナンシ側)。auth プロファイル・
  compartment は環境依存値(.env: GEN_SHARED_PROFILE / GEN_SHARED_COMPARTMENT_OCID —
  コミット禁止)。表の構造はコード・環境依存値は .env(tasks/SP3-06 作業内容1)。
"""

from dataclasses import dataclass
from typing import Literal

GenApi = Literal["chat", "responses"]


@dataclass(frozen=True)
class GenModelDef:
    oci_id: str
    api: GenApi
    region: str          # 推論エンドポイントのリージョン(エンドポイントは region から導出)
    shared: bool = False  # True = ORASEJAPAN 共有テナンシ


GEN_MODELS: dict[str, GenModelDef] = {
    # 既定: 自テナンシ大阪(現行どおり RP/DEFAULT auth)。フル生成実証済み(SP3-03)
    "gpt-oss-120b": GenModelDef("openai.gpt-oss-120b", "chat", "ap-osaka-1"),
    # 施主指定 7 モデル(ORASEJAPAN — 施主指示 2026-07-08。ADR-0023 F2 の拡張)
    "gpt-5.5": GenModelDef("openai.gpt-5.5", "responses", "ap-osaka-1", shared=True),
    "gpt-5.6-luna": GenModelDef(
        "openai.gpt-5.6-luna", "responses", "us-chicago-1", shared=True),
    "gpt-5.6-sol": GenModelDef(
        "openai.gpt-5.6-sol", "responses", "us-chicago-1", shared=True),
    "gpt-5.6-terra": GenModelDef(
        "openai.gpt-5.6-terra", "responses", "us-chicago-1", shared=True),
    "gpt-5.1-codex-mini": GenModelDef(
        "openai.gpt-5.1-codex-mini", "responses", "ap-osaka-1", shared=True),
    "gpt-5.3-codex": GenModelDef(
        "openai.gpt-5.3-codex", "responses", "ap-osaka-1", shared=True),
    "gpt-5.5-pro": GenModelDef(
        "openai.gpt-5.5-pro", "responses", "ap-osaka-1", shared=True),
}

# 既定は自テナンシの gpt-oss-120b(model 未指定でも共有テナンシ設定なしで動く = 後方互換)。
# UI の選択肢は品質重視の curated subset(state.ts — 施主指示 2026-07-09 で gpt-oss-120b/codex 系は
# 選択肢から除外)だが、レジストリ・既定は 8 モデルのまま維持する(署名プロキシ allowlist・
# 既存デモの再生成・非共有フォールバック)。UI の既定は state.ts 側で gpt-5.6-sol。
DEFAULT_GEN_MODEL = "gpt-oss-120b"

# OCI id → 定義(署名プロキシの allowlist・ルーティングの逆引き)
GEN_MODELS_BY_OCI_ID: dict[str, GenModelDef] = {d.oci_id: d for d in GEN_MODELS.values()}


def inference_base_url(region: str) -> str:
    """OpenAI 互換推論エンドポイント(specs/00 未文書仕様1 の DP 形を region から導出)。"""
    return f"https://inference.generativeai.{region}.oci.oraclecloud.com/openai/v1"
