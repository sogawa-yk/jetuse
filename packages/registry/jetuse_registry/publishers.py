"""発行者認証(publish の認可境界)。

中央レジストリの読取は公開だが、publish と公開鍵登録は「発行者」に限る(comparison §2: publish は
発行者認証＋署名)。本 MVP は Bearer トークン→publisher_id の写像で認証する。トークンは平文で保持
せず sha256 ハッシュで突き合わせ、比較は `hmac.compare_digest` で定数時間にする(トークン推測の
タイミング攻撃を避ける)。

トークン実値・publisher 対応はリポジトリにコミットしない(環境変数 `REGISTRY_PUBLISHER_TOKENS` 等で
注入)。本タスクの責務は「認証の仕組み」であり、IAM/Identity Domain への本格統合はステージ4。
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Protocol, runtime_checkable


def hash_token(token: str) -> str:
    """トークンの sha256(16 進)を返す。保存・比較はこのハッシュで行う(平文を保持しない)。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@runtime_checkable
class PublisherAuthenticator(Protocol):
    """Bearer トークンを publisher_id へ解決する。未知トークンは None。"""

    def authenticate(self, token: str) -> str | None:
        ...


class StaticTokenAuthenticator:
    """トークンハッシュ→publisher_id の静的写像で認証する MVP 実装。

    `token_hashes` は {sha256(token): publisher_id}。平文トークンは保持しない。照合は
    `hmac.compare_digest` で定数時間に行い、未知トークンや空トークンは None を返す。
    """

    def __init__(self, token_hashes: dict[str, str]) -> None:
        # 値は publisher_id。キーは小文字 16 進の sha256。
        self._token_hashes = dict(token_hashes)

    def authenticate(self, token: str) -> str | None:
        if not token:
            return None
        presented = hash_token(token)
        # 全件を compare_digest で当たり、一致しても早期 return しない。これにより成功時の処理時間が
        # 「どのトークンが何番目に一致したか(登録順)」に依存しない(タイミングからの推測を避ける)。
        matched: str | None = None
        for stored_hash, publisher in self._token_hashes.items():
            if hmac.compare_digest(presented, stored_hash):
                matched = publisher
        return matched

    @classmethod
    def from_token_map(cls, token_to_publisher: dict[str, str]) -> StaticTokenAuthenticator:
        """{平文トークン: publisher_id} からハッシュ化して構築する(テスト・初期セットアップ用)。"""
        return cls({hash_token(t): p for t, p in token_to_publisher.items()})

    @classmethod
    def from_env(cls, var: str = "REGISTRY_PUBLISHER_TOKENS") -> StaticTokenAuthenticator:
        """環境変数から構築する。書式は `publisher1:tokenhash1,publisher2:tokenhash2`。

        トークン実値ではなく sha256 ハッシュを設定する(平文をプロセス環境にも置かない)。
        未設定なら空(=全 publish 401)で返す。
        """
        raw = os.environ.get(var, "").strip()
        mapping: dict[str, str] = {}
        for pair in raw.split(","):
            pair = pair.strip()
            if not pair:
                continue
            publisher, _, token_hash = pair.partition(":")
            if publisher and token_hash:
                mapping[token_hash.strip()] = publisher.strip()
        return cls(mapping)
