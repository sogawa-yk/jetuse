"""AGT-06 の probe 共通部(シカゴの Responses / Chat クライアント)。

登録簿を増やすときは docs/verification/AGT-06.md §9 の手順でこれらを回し、
**測れたものだけ**を `jetuse_core/models.py` に書く。
"""

import base64
import os
import pathlib
import struct
import sys
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages" / "api"))
sys.path.insert(0, str(ROOT / "ops"))

import _adb as adb  # noqa: E402  環境変数 → .env の順で読む(ops と同じ流儀)

REGION = os.environ.get("AGT06_PROBE_REGION", "us-chicago-1")
os.environ["OCI_REGION"] = REGION

import httpx  # noqa: E402
from jetuse_core.oci_auth import httpx_auth  # noqa: E402
from openai import OpenAI  # noqa: E402

COMPARTMENT = adb.env("COMPARTMENT_OCID").strip()
# project はリージョン別。OCID をコードへ焼かない(.env で渡す)
PROJECT = (adb.env("AGT06_CHICAGO_PROJECT_OCID").strip()
           or adb.env("PROJECT_OCID").strip())
BASE_URL = f"https://inference.generativeai.{REGION}.oci.oraclecloud.com/openai/v1"


def client(*, with_project: bool) -> OpenAI:
    """推論クライアント。非 OpenAI モデル(gemini / grok)は OpenAi-Project が必須。"""
    headers = {"CompartmentId": COMPARTMENT, "opc-compartment-id": COMPARTMENT}
    if with_project:
        if not PROJECT:
            raise SystemExit(
                "AGT06_CHICAGO_PROJECT_OCID(または PROJECT_OCID)が未設定です。"
                "project はリージョン別なので、対象リージョンのものを .env に置いてください"
            )
        headers["OpenAi-Project"] = PROJECT
    return OpenAI(api_key="OCI", base_url=BASE_URL,
                  http_client=httpx.Client(auth=httpx_auth(), headers=headers,
                                           timeout=180.0))


def err(e: Exception) -> dict:
    return {"ok": False, "type": type(e).__name__,
            "status": getattr(e, "status_code", None) or getattr(e, "status", None),
            "msg": str(e)[:240]}


def msg(role: str, text: str) -> dict:
    return {"type": "message", "role": role,
            "content": [{"type": "input_text", "text": text}]}


def png(rgb: tuple = (255, 0, 0), size: int = 32) -> bytes:
    """単色 PNG を依存なしで組み立てる。

    **32px 以上にすること。** 1x1 だと Grok が
    `Image dimensions 1x1 are too small` で弾き、画像非対応と誤判定する(実測)。
    """
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def data_url(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode()


# 入れ子引数つきツール(TOOL-03 で対応した形が各モデルで通るかを見る)
NESTED_TOOL = {
    "type": "function", "name": "order_part", "description": "部品を発注する",
    "parameters": {
        "type": "object",
        "properties": {
            "part": {"type": "string", "description": "部品コード"},
            "opts": {"type": "object", "description": "発注オプション",
                     "properties": {"qty": {"type": "integer"},
                                    "rush": {"type": "boolean"}},
                     "required": ["qty"]},
        },
        "required": ["part", "opts"],
    },
}
NESTED_ASK = "部品 JX-7742 を 3 個、至急で発注して。order_part ツールを必ず使うこと。"
