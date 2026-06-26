"""マーケット公開(PLG-05 / D7)の route 共通ヘルパ。

usecases / agents 両 route から呼ぶ。設定(発行者鍵・トークン・レジストリURL)は
`jetuse_core.settings` 経由で読み、export→署名→publish を `jetuse_core.plugins.publisher`
に委譲する。ドメイン例外を HTTP ステータスへ写像する(設定欠如=503、検証/競合=元ステータス)。
"""

from typing import Any

from fastapi import HTTPException

from jetuse_core.plugins.publisher import (
    PublisherConfig,
    PublisherConfigError,
    PublishError,
    publish_definition,
)
from jetuse_core.settings import get_settings


def publish_entity(
    *, kind: str, definition: dict[str, Any], entity_id: str, version: str
) -> dict[str, Any]:
    """定義をマーケットへ公開し、登録結果(id/version/kind/publisher/entry)を返す。

    設定が未設定なら 503(運用者が .env を設定するまで機能無効)。publish 失敗は元の HTTP
    ステータス(422=manifest/署名不正・無署名、409=既存版、401/403=認証/認可)を保つ。
    """
    config = PublisherConfig.from_settings(get_settings())
    try:
        return publish_definition(
            kind=kind, definition=definition, entity_id=entity_id, version=version,
            config=config,
        )
    except PublisherConfigError as e:
        # 発行者設定が未整備。運用者の設定待ち(機能未構成)= 503。
        raise HTTPException(status_code=503, detail=str(e)) from e
    except PublishError as e:
        # レジストリが返したステータスを尊重する(なければ 502=上流エラー)。
        raise HTTPException(status_code=e.status or 502, detail=str(e)) from e
