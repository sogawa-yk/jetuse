"""Reference Implementation Descriptor(静的 Catalog)。

main の実機検証済み AI 機能を、Builder が選べる機械可読 Descriptor として記述する
(実装方針 §3.6 / §7.1)。MVP は `rag.answer` のみ・静的ファイル＋薄いローダー。
"""

from .catalog import (
    get_capability,
    list_capabilities,
    verify_descriptors,
)

__all__ = ["get_capability", "list_capabilities", "verify_descriptors"]
