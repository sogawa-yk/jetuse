"""レジストリ操作のドメイン例外。

サービス層(service.py)が送出し、API 層(app.py)が HTTP ステータスへ写像する。
ドメイン層は HTTP/FastAPI に依存しないため、CLI・別フロントからも同じ意味で再利用できる。
"""

from __future__ import annotations


class RegistryError(Exception):
    """レジストリ操作の基底例外。"""


class RegistryValidationError(RegistryError):
    """入力(manifest・公開鍵・署名)が仕様に適合しない。HTTP 400/422 相当。"""


class RegistryAuthError(RegistryError):
    """発行者**認証**の失敗(未知/不正トークン)。HTTP 401 相当。"""


class RegistryForbiddenError(RegistryError):
    """発行者**認可**の失敗(認証は通ったが publisher 不一致でなりすまし)。HTTP 403 相当。

    認証(RegistryAuthError=401)と区別し、API 層は例外型でステータスを決める(メッセージ文字列に
    依存しない=文言変更・多言語化でステータスが変わらない)。"""


class RegistryNotFoundError(RegistryError):
    """要求されたプラグイン/版が存在しない。HTTP 404 相当。"""


class RegistryConflictError(RegistryError):
    """既存の版に対する再 publish 等の競合。版は不変(immutable)。HTTP 409 相当。"""


class RegistryStorageError(RegistryError):
    """保存層の内部不整合(index に在るのに成果物が欠落 等)。HTTP 500 相当。

    「未登録(404)」と区別する。index にエントリがあるのに成果物が読めないのは利用者起因では
    なく保存層の破損/手動削除であり、404 で隠さず内部エラーとして表面化させる。"""
