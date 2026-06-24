"""テスト用の共通フィクスチャ/スタブ。

`fdk`(OCI Functions Development Kit)は本番のFunctionsイメージ(Containerfile.fn)では
インストールされ実機で使われるが、API側のCI/dev環境には入れない方針:
  - fdk は iso8601==0.1.12 を完全固定し、本体依存の oci SDK と衝突しうる。
  - Cython 等のビルド依存まで引き込み、テスト環境を重くする。
fn.router.func が import 時に参照するのは fdk.response.Response のみ(レスポンス整形の薄いラッパ)。
そのため、テスト実行時だけ軽量スタブを sys.modules に注入し、既存のモックテストを成立させる。
スタブは実 fdk.response.Response の公開挙動(body()/SetResponseHeaders 呼び出し)を再現する。
"""

import sys
import types


def _install_fdk_stub() -> None:
    if "fdk" in sys.modules:
        return

    class Response:
        def __init__(self, ctx, response_data: str | bytes | None = None,
                     headers: dict | None = None, status_code: int = 200,
                     response_encoding: str = "utf-8"):
            self.ctx = ctx
            self.status_code = status_code
            self.response_data = response_data if response_data else ""
            self.response_encoding = response_encoding
            ctx.SetResponseHeaders(headers or {}, status_code)

        def status(self):
            return self.status_code

        def body(self):
            return self.response_data

        def body_bytes(self):
            if isinstance(self.response_data, (bytes, bytearray)):
                return self.response_data
            return str(self.response_data).encode(self.response_encoding)

        def context(self):
            return self.ctx

    fdk = types.ModuleType("fdk")
    response = types.ModuleType("fdk.response")
    response.Response = Response
    fdk.response = response
    sys.modules["fdk"] = fdk
    sys.modules["fdk.response"] = response


_install_fdk_stub()
