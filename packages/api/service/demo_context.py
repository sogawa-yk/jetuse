"""(user, demo) スコープの継ぎ目(SP1-02 / specs/17 §5)。

所有権検証は信頼境界 — fail-closed。存在しない demo と他人の private demo は
同じ 404 を返す(存在秘匿のため 403 にしない)。
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException

from jetuse_core import demos
from jetuse_core.auth import AuthContext, require_user


@dataclass
class DemoContext:
    demo_id: str
    owner_sub: str
    namespace: str  # RAG・会話の名前空間キー。将来の DB スキーマ名の元(specs/17 §5)
    status: str  # specs/18 §2.3(SP2-01)。deleting は require_demo が 404 済み


def require_demo(
    demo_id: str, user: Annotated[AuthContext, Depends(require_user)]
) -> DemoContext:
    demo = demos.get_demo(demo_id)
    if (
        not demo
        # 解体中の箱への能力呼び出しが lazy 生成で箱を復活させる事故を封じる(specs/18 §2.3)
        or demo["status"] == "deleting"
        or (demo["owner_sub"] != user.subject and demo["visibility"] != "public")
    ):
        raise HTTPException(status_code=404, detail="demo not found")
    return DemoContext(
        demo_id=demo_id, owner_sub=demo["owner_sub"], namespace=f"demo_{demo_id}",
        status=demo["status"],
    )


def require_ready_demo(
    ctx: Annotated[DemoContext, Depends(require_demo)],
) -> DemoContext:
    """能力ルート・/app/ 配信の共通依存(specs/19 §8.1 — SP3-01 で一般化)。

    deleting 404 を「ready 以外 404」へ広げる: provisioning/failed の箱への能力呼び出しが
    lazy 生成と競合する余地を構造的に消す。存在秘匿と同じ 404。demos CRUD メタは対象外
    (所有者は非 ready でも status を見られる — 進行表示・再生成・破棄に必要)。
    """
    if ctx.status != "ready":
        raise HTTPException(status_code=404, detail="demo not found")
    return ctx


def require_demo_owner(
    ctx: Annotated[DemoContext, Depends(require_demo)],
    user: Annotated[AuthContext, Depends(require_user)],
) -> DemoContext:
    """書き込み系は所有者のみ。公開デモの非所有者は閲覧・実行(chat/GET)まで
    (usecases の「公開は取得・実行可、編集・削除は所有者のみ」と同じ規則。SP1-03 REV-002)。"""
    if ctx.owner_sub != user.subject:
        raise HTTPException(status_code=404, detail="demo not found")
    return ctx
