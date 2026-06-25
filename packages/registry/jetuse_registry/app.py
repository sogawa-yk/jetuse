"""中央レジストリの FastAPI アプリ(読取公開 / publish 認証)。

ドメイン層(`RegistryService`)を HTTP に写像する薄い層。エンドポイント:

  GET  /registry/plugins                                  一覧(list)
  GET  /registry/plugins/search?q=&kind=&tag=             検索(search)
  GET  /registry/plugins/{namespace}/{name}?version=      取得(get: entry+manifest)
  GET  /registry/plugins/{namespace}/{name}/download?version=  成果物 DL(manifest JSON)
  GET  /registry/publishers/keys?publisher=               発行者公開鍵の取得(取込側の署名検証用)
  POST /registry/publishers/keys                          公開鍵登録(発行者認証)
  POST /registry/plugins                                  publish(発行者認証＋署名検証)

plugin id は PLG-01(`manifest.ID_PATTERN` = `^seg/seg$`)で **ちょうど 2 セグメント**
(`namespace/name`、各 `[a-z0-9]`＋ハイフン)に固定される。よって `{namespace}/{name}` の 2 パス
セグメントで全ての有効な id を表現でき、URL エンコードは不要(>2 階層の id は存在しない)。

認証は `Authorization: Bearer <token>`。読取は無認証。ドメイン例外を HTTP ステータスへ写像する。

注意: 本モジュールは `from __future__ import annotations` を使わない。FastAPI 依存(`Depends`)を
クロージャ内ローカル alias(`Svc`/`Auth`)で表すため、注釈は文字列化せず実体として評価させる必要がある
(文字列化するとモジュールスコープで `Svc` を解決できず query パラメータ扱いになる)。
"""

from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from .errors import (
    RegistryAuthError,
    RegistryConflictError,
    RegistryError,
    RegistryForbiddenError,
    RegistryNotFoundError,
    RegistryStorageError,
    RegistryValidationError,
)
from .service import RegistryService


class RegisterKeyBody(BaseModel):
    public_key_id: str = Field(alias="publicKeyId")
    public_key: str = Field(alias="publicKey")

    model_config = {"populate_by_name": True}


def _bearer_token(authorization: str | None) -> str:
    """Authorization ヘッダから Bearer トークンを取り出す。無ければ 401。"""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization ヘッダが必要")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Authorization は 'Bearer <token>' 形式")
    return token.strip()


def create_app(service: RegistryService) -> FastAPI:
    """与えた `RegistryService` を束ねた FastAPI アプリを返す。

    テストはインメモリ store のサービスを渡し、本番は `storage.build_from_env()` で構築した
    OCI Object Storage サービスを渡す。サービスは DI(クロージャ)で注入する。
    """
    app = FastAPI(title="JetUse Plugin Registry", version="0.1.0")

    def get_service() -> RegistryService:
        return service

    # B008 回避: FastAPI 依存は Annotated に寄せる(api/service/routes と同じ規約)。
    Svc = Annotated[RegistryService, Depends(get_service)]
    # alias を明示し、引数名に依存せず必ず HTTP `Authorization` ヘッダを読む(大小無視)。
    Auth = Annotated[str | None, Header(alias="Authorization")]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/registry/plugins")
    def list_plugins(svc: Svc) -> dict[str, Any]:
        return {"plugins": svc.list_plugins()}

    @app.get("/registry/publishers/keys")
    def get_publisher_keys(
        svc: Svc,
        publisher: Annotated[str, Query()],
    ) -> dict[str, Any]:
        # 取込側(PLG-03)が署名検証に使う発行者公開鍵を取得する読取経路(無認証・公開)。
        # publisher は query param で受ける(PLG-01 の publisher は非空文字列で '/' 等も許すため、
        # パスセグメントに固定すると '/' 入り publisher を表現できない)。
        return {"publisher": publisher, "keys": svc.get_publisher_keys(publisher)}

    @app.get("/registry/plugins/search")
    def search(
        svc: Svc,
        q: Annotated[str | None, Query()] = None,
        kind: Annotated[str | None, Query()] = None,
        tag: Annotated[str | None, Query()] = None,
    ) -> dict[str, Any]:
        return {"plugins": svc.search(q, kind=kind, tag=tag)}

    @app.get("/registry/plugins/{namespace}/{name}")
    def get_plugin(
        namespace: str,
        name: str,
        svc: Svc,
        version: Annotated[str | None, Query()] = None,
    ) -> dict[str, Any]:
        try:
            return svc.get(f"{namespace}/{name}", version)
        except RegistryNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except RegistryStorageError as e:
            # index に在るのに成果物欠落=保存層の不整合。404 で隠さず 500 で表面化。
            raise HTTPException(status_code=500, detail=str(e)) from e

    @app.get("/registry/plugins/{namespace}/{name}/download")
    def download(
        namespace: str,
        name: str,
        svc: Svc,
        version: Annotated[str | None, Query()] = None,
    ) -> Response:
        try:
            data, entry = svc.download(f"{namespace}/{name}", version)
        except RegistryNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except RegistryStorageError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return Response(
            content=data,
            media_type="application/json",
            headers={
                "X-Plugin-Id": entry.id,
                "X-Plugin-Version": entry.version,
                "X-Plugin-Sha256": entry.sha256,
            },
        )

    @app.post("/registry/publishers/keys", status_code=201)
    def register_key(
        body: RegisterKeyBody,
        svc: Svc,
        authorization: Auth = None,
    ) -> dict[str, Any]:
        token = _bearer_token(authorization)
        try:
            return svc.register_public_key(token, body.public_key_id, body.public_key)
        except RegistryAuthError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e
        except RegistryConflictError as e:
            # 鍵 ID の差し替え(不変違反)。
            raise HTTPException(status_code=409, detail=str(e)) from e
        except RegistryValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except RegistryStorageError as e:
            # 保存層/レスポンス形状の異常(etag 取得不可 等)。
            raise HTTPException(status_code=500, detail=str(e)) from e
        except RegistryError as e:
            # index 更新の並行衝突がリトライ上限を超過(基底 RegistryError)。再試行可=503。
            raise HTTPException(status_code=503, detail=str(e)) from e

    @app.post("/registry/plugins", status_code=201)
    def publish(
        manifest: dict[str, Any],
        svc: Svc,
        authorization: Auth = None,
    ) -> dict[str, Any]:
        token = _bearer_token(authorization)
        try:
            return svc.publish(token, manifest)
        except RegistryForbiddenError as e:
            # 認可失敗(publisher 不一致)。例外型でステータスを決める(メッセージ文字列に依存しない)。
            raise HTTPException(status_code=403, detail=str(e)) from e
        except RegistryAuthError as e:
            # 認証失敗(未知/不正トークン)。
            raise HTTPException(status_code=401, detail=str(e)) from e
        except RegistryConflictError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except RegistryValidationError as e:
            # 無署名・署名検証失敗・manifest 不正はいずれも 422(処理不能なエンティティ)。
            raise HTTPException(status_code=422, detail=str(e)) from e
        except RegistryStorageError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        except RegistryError as e:
            # index 更新の並行衝突がリトライ上限を超過(基底 RegistryError)。再試行可=503。
            raise HTTPException(status_code=503, detail=str(e)) from e

    return app
