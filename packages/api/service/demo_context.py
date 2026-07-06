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


def require_demo(
    demo_id: str, user: Annotated[AuthContext, Depends(require_user)]
) -> DemoContext:
    demo = demos.get_demo(demo_id)
    if not demo or (demo["owner_sub"] != user.subject and demo["visibility"] != "public"):
        raise HTTPException(status_code=404, detail="demo not found")
    return DemoContext(
        demo_id=demo_id, owner_sub=demo["owner_sub"], namespace=f"demo_{demo_id}"
    )
