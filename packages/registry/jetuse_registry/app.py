"""中央レジストリの FastAPI アプリ(読取公開 / publish 認証)。

ドメイン層(`RegistryService`)を HTTP に写像する薄い層。エンドポイント:

  GET  /registry/plugins                                  一覧(list)
  GET  /registry/plugins/search?q=&kind=&tag=             検索(search)
  GET  /registry/plugins/{namespace}/{name}?version=      取得(get: entry+manifest)
  GET  /registry/plugins/{namespace}/{name}/download?version=  成果物 DL(manifest JSON)
  GET  /registry/publishers/keys?publisher=               発行者公開鍵の取得(取込側の署名検証用)
  POST /registry/publishers/keys                          公開鍵登録(発行者認証)
  POST /registry/plugins                                  publish(発行者認証＋署名検証)
  GET  /registry/plugins/{namespace}/{name}/ratings       評価集計の取得(MKT-02)
  POST /registry/plugins/{namespace}/{name}/ratings       評価登録(発行者認証。MKT-02)
  POST /registry/plugins/{namespace}/{name}/lifecycle     版ライフサイクル変更(所有発行者。MKT-02)

MKT-02: 保存層を ADB μService へ昇格(評価/DL 数/版ライフサイクル/DB 検索)。読取・publish は
PLG-04 と後方互換。yanked 版の明示取得は 410、拡張(評価/ライフサイクル)は ADB 限定・未対応は 501。

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
from pydantic import BaseModel, Field, StrictInt

from .errors import (
    RegistryAuthError,
    RegistryConflictError,
    RegistryError,
    RegistryForbiddenError,
    RegistryGoneError,
    RegistryNotFoundError,
    RegistryStorageError,
    RegistryUnsupportedError,
    RegistryValidationError,
)
from .service import RegistryService


class RegisterKeyBody(BaseModel):
    public_key_id: str = Field(alias="publicKeyId")
    public_key: str = Field(alias="publicKey")

    model_config = {"populate_by_name": True}


class RatingBody(BaseModel):
    # StrictInt: JSON の true/false や数値文字列を 1/0 等へ強制せず 422 にする(score は厳密に整数)。
    score: StrictInt
    comment: str = ""

    model_config = {"populate_by_name": True}


class LifecycleBody(BaseModel):
    version: str
    state: str

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
    def list_plugins(
        svc: Svc,
        include_yanked: Annotated[bool, Query(alias="includeYanked")] = False,
    ) -> dict[str, Any]:
        return {"plugins": svc.list_plugins(include_yanked=include_yanked)}

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
        include_yanked: Annotated[bool, Query(alias="includeYanked")] = False,
    ) -> dict[str, Any]:
        return {"plugins": svc.search(q, kind=kind, tag=tag, include_yanked=include_yanked)}

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
        except RegistryGoneError as e:
            # yank 済み版の明示取得。配布停止を 410 で明示する(MKT-02)。
            raise HTTPException(status_code=410, detail=str(e)) from e
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
        except RegistryGoneError as e:
            raise HTTPException(status_code=410, detail=str(e)) from e
        except RegistryStorageError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return Response(
            content=data,
            media_type="application/json",
            headers={
                "X-Plugin-Id": entry.id,
                "X-Plugin-Version": entry.version,
                "X-Plugin-Sha256": entry.sha256,
                # MKT-02: ADB バックエンドは加算後の累計 DL 数を返す(index バックエンドは 0 据置)。
                "X-Plugin-Download-Count": str(entry.download_count),
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

    @app.get("/registry/plugins/{namespace}/{name}/ratings")
    def get_ratings(namespace: str, name: str, svc: Svc) -> dict[str, Any]:
        # 評価集計の取得は読取系として無認証で公開する(件数・平均・直近コメント)。
        try:
            return svc.get_ratings(f"{namespace}/{name}")
        except RegistryNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except RegistryUnsupportedError as e:
            raise HTTPException(status_code=501, detail=str(e)) from e

    @app.post("/registry/plugins/{namespace}/{name}/ratings", status_code=201)
    def add_rating(
        namespace: str,
        name: str,
        body: RatingBody,
        svc: Svc,
        authorization: Auth = None,
    ) -> dict[str, Any]:
        token = _bearer_token(authorization)
        try:
            return svc.rate_plugin(token, f"{namespace}/{name}", body.score, body.comment)
        except RegistryAuthError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e
        except RegistryNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except RegistryValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except RegistryUnsupportedError as e:
            raise HTTPException(status_code=501, detail=str(e)) from e

    @app.post("/registry/plugins/{namespace}/{name}/lifecycle")
    def set_lifecycle(
        namespace: str,
        name: str,
        body: LifecycleBody,
        svc: Svc,
        authorization: Auth = None,
    ) -> dict[str, Any]:
        token = _bearer_token(authorization)
        try:
            return svc.set_lifecycle(token, f"{namespace}/{name}", body.version, body.state)
        except RegistryForbiddenError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except RegistryAuthError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e
        except RegistryNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except RegistryValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except RegistryUnsupportedError as e:
            raise HTTPException(status_code=501, detail=str(e)) from e

    return app
