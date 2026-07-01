"""Experience Builder Capability の Provider 実装群 (実装方針 §3.5 / §12.2)。

各 Provider は生成 UI / 新 API から OCI を直叩きせず、既存 jetuse_core の実機検証済み
実装へ委譲する Adapter (ADR-0021 seam)。
"""
