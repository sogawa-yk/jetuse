"""構成サマリ生成(HBD-05)。合成済みデモ構成 → 顧客提示用の構成サマリ。

HBD-03 の `synth.DemoComposition`(合成済みデモ構成)と HBD-01 の `recommend.Recommendation` を
入力に、フィールドSAが顧客へ提示できる**構成サマリ**を生成する。出典: docs/enhance/
202607-hearing-flow.md §5(推薦構成サマリ=構成図・使うOCIサービス・デモ手順・想定効果)/
202607-demo-platform-plan.md §10「HBD-05」。

設計の要点:

  - **構成図(①)・使うOCIサービス(②)・デモ手順(③)は合成結果から決定的に導出する**(捏造しない)。
    どのデータに何の AI が効くか、どの OCI サービスを使うかは、`DemoComposition.screens` /
    `bindings`(active な組込点)/ `seed`(データ計画)/ `connectors` から機械的に組み立てる。
    GenAI が不在/失敗でも構成図・使用サービス・手順は完全に成立する。
  - **想定効果(④)の文章化だけ GenAI 補助**(§6 の境界 ④「サマリ文章化」)。GenAI 不在/失敗時は
    決定的なテンプレ文へフォールバックする(`impact_source` で出所を明示)。
  - **エクスポート可能**: プリセールス転用の下敷きとして Markdown を同梱する(`markdown`)。

副作用なし。`build_summary()` は DB/GenAI に触れない純関数。GenAI 文章化は呼び出し側が
`summary_narrative()`(別関数)で取得し、`narrative` 引数として渡す(関心の分離)。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .synth import DemoComposition

# --- 語彙: capability の日本語ラベル(顧客提示用) ----------------------------

#: capability → 顧客提示用ラベル。未知語は capability 名をそのまま使う。
CAPABILITY_LABELS: dict[str, str] = {
    "rag.search": "社内文書 RAG 検索（根拠付き QA）",
    "summarize": "要約",
    "classify": "分類・仕分け",
    "draft": "返信ドラフト生成",
    "nl2sql": "自然言語→SQL 照会（NL2SQL）",
    "chart": "集計結果のグラフ化",
    "agent": "業務エージェント",
    "vlm.ocr": "帳票・画像 OCR（VLM）",
    "minutes": "音声議事録生成",
}


def capability_label(capability: str) -> str:
    return CAPABILITY_LABELS.get(capability, capability)


# --- ②使う OCI サービス(固定リファレンス基盤の該当部分) ----------------------

#: capability → 実行に使う OCI サービス(固定リファレンス基盤の該当部分)。
#: 出典: CLAUDE.md「環境の確定事実」(ap-osaka-1 の GenAI / ADB Select AI / Speech 等)。
CAPABILITY_OCI_SERVICES: dict[str, tuple[str, ...]] = {
    "rag.search": (
        "OCI Generative AI（埋め込み + File Search / Vector Store）",
        "OCI Generative AI（Chat 生成）",
    ),
    "summarize": ("OCI Generative AI（Chat 生成）",),
    "classify": ("OCI Generative AI（Chat 生成）",),
    "draft": ("OCI Generative AI（Chat 生成）",),
    "nl2sql": (
        "Oracle Autonomous Database（Select AI / 読取実行）",
        "OCI Generative AI（Chat 生成）",
    ),
    "chart": ("OCI Generative AI（Chat 生成）",),
    "agent": ("OCI Generative AI Agents",),
    "vlm.ocr": (
        "OCI Generative AI（Vision / マルチモーダル）",
        "OCI Document Understanding",
    ),
    "minutes": (
        "OCI Speech（STT / Whisper）",
        "OCI Generative AI（Chat 生成）",
    ),
}

#: connector → OCI サービス。Slack 連携は API Gateway / Functions で受ける(コア)。
CONNECTOR_OCI_SERVICES: dict[str, tuple[str, ...]] = {
    "slack": ("OCI API Gateway", "OCI Functions"),
}

#: どのデモでも使う基盤の該当部分(アプリ/シードデータの永続)。
BASE_OCI_SERVICE = "Oracle Autonomous Database（アプリ定義・業務/シードデータ）"


# --- サブモデル ------------------------------------------------------------


class DiagramFlow(BaseModel):
    """構成図(①)の 1 経路 = 「どのデータに何の AI が効くか」。"""

    model_config = ConfigDict(extra="forbid")

    #: データの出所(データセットのラベル or 利用者入力)。
    data: str
    #: 効く AI 部品(capability)。
    capability: str
    #: capability の顧客提示ラベル。
    capability_label: str
    #: 効く画面のタイトル。
    screen: str
    #: 主役(Q3 由来)経路か。
    highlight: bool
    #: 人間可読の一文(「FAQ → RAG 検索（FAQ 画面）」)。
    line: str


class OciServiceRef(BaseModel):
    """使う OCI サービス(②)1 件と、その用途(どの部品/連携で使うか)。"""

    model_config = ConfigDict(extra="forbid")

    service: str
    #: この OCI サービスを使う理由(capability ラベル/コネクタ/基盤用途)。
    used_for: list[str]


class DemoStep(BaseModel):
    """デモ手順(③)の 1 ステップ。"""

    model_config = ConfigDict(extra="forbid")

    order: int
    title: str
    detail: str


class DemoSummary(BaseModel):
    """構成サマリ(§5)。①構成図 ②使うOCIサービス ③デモ手順 ④想定効果＋エクスポート用 Markdown。"""

    model_config = ConfigDict(extra="forbid")

    sample_app: str
    app_name: str
    ui: str | None
    connectors: list[str]
    highlight: str | None
    seed_strategy: str
    #: ①構成図(どのデータに何の AI が効くか)。
    diagram: list[DiagramFlow]
    #: ②使う OCI サービス(固定リファレンス基盤の該当部分)。
    oci_services: list[OciServiceRef]
    #: ③デモ手順。
    steps: list[DemoStep]
    #: ④想定効果(顧客提示文)。
    impact: str
    #: 想定効果の出所("genai" | "deterministic")。
    impact_source: str
    #: 実行可能(active)な AI 部品。
    active_parts: list[str]
    #: 構成から外した部品(capability → 理由)。プリセールスの注記用。
    excluded: list[dict[str, str]]
    #: プリセールス資料の下敷き(エクスポート用 Markdown)。
    markdown: str


# --- ①構成図 ---------------------------------------------------------------


def _dataset_labels(composition: DemoComposition) -> dict[str, str]:
    """データセット名 → ラベルの対応(構成図のデータ表示用)。"""
    out: dict[str, str] = {}
    for d in composition.seed.datasets:
        name = d.get("name")
        if name:
            out[name] = d.get("label") or name
    return out


def _diagram(composition: DemoComposition) -> list[DiagramFlow]:
    """active な組込点から「どのデータに何の AI が効くか」を決定的に導出する。

    画面ごとに active スロットを辿り、画面のデータセット(あれば)を入力データとする。
    データセットを持たない画面(問い合わせ等)は利用者入力を入力データとする。
    """
    labels = _dataset_labels(composition)
    flows: list[DiagramFlow] = []
    seen: set[tuple[str, str]] = set()
    for screen in composition.screens:
        if screen.dataset:
            data = labels.get(screen.dataset, screen.dataset)
        else:
            data = "利用者の入力・問い合わせ"
        for slot in screen.slots:
            cap = slot["capability"]
            key = (screen.key, cap)
            if key in seen:
                continue
            seen.add(key)
            cap_label = capability_label(cap)
            flows.append(
                DiagramFlow(
                    data=data,
                    capability=cap,
                    capability_label=cap_label,
                    screen=screen.title,
                    highlight=bool(slot.get("highlight")),
                    line=f"{data} → {cap_label}（{screen.title} 画面）",
                )
            )
    # 主役経路を先頭に寄せ、その他は出現順を保つ(安定整列)。
    flows.sort(key=lambda f: (not f.highlight,))
    return flows


# --- ②使う OCI サービス ----------------------------------------------------


def _oci_services(composition: DemoComposition) -> list[OciServiceRef]:
    """active 部品・コネクタ・基盤から、使う OCI サービスを決定的に集約する。"""
    # service → used_for(理由)を順序保持で集約する。
    agg: dict[str, list[str]] = {}

    def _add(service: str, reason: str) -> None:
        bucket = agg.setdefault(service, [])
        if reason not in bucket:
            bucket.append(reason)

    # 基盤(該当部分): アプリ定義・業務/シードデータは常に ADB。
    _add(BASE_OCI_SERVICE, "アプリ定義・業務データの永続")

    for cap in composition.active_parts:
        for svc in CAPABILITY_OCI_SERVICES.get(cap, ()):
            _add(svc, capability_label(cap))
    for connector in composition.connectors:
        for svc in CONNECTOR_OCI_SERVICES.get(connector, ()):
            _add(svc, f"{connector} 連携")

    # サービス名で安定整列(基盤 ADB を先頭に固定)。
    def _sort_key(item: tuple[str, list[str]]) -> tuple[int, str]:
        service = item[0]
        return (0 if service == BASE_OCI_SERVICE else 1, service)

    return [
        OciServiceRef(service=service, used_for=reasons)
        for service, reasons in sorted(agg.items(), key=_sort_key)
    ]


# --- ③デモ手順 -------------------------------------------------------------


def _steps(composition: DemoComposition) -> list[DemoStep]:
    """画面・active 組込点・コネクタからデモ手順を決定的に組み立てる。"""
    steps: list[DemoStep] = []
    app_name = composition.app_name or composition.sample_app or "デモアプリ"
    ui = composition.ui or "chat"
    steps.append(
        DemoStep(
            order=1,
            title=f"デモアプリ「{app_name}」を起動環境で開く",
            detail=f"UI/出力テンプレ: {ui}。シード方針: {composition.seed.note}",
        )
    )

    # 主役→その他の順で、各 active 組込点を実行するステップを並べる。
    diagram = _diagram(composition)
    for flow in diagram:
        steps.append(
            DemoStep(
                order=len(steps) + 1,
                title=f"「{flow.screen}」画面で {flow.capability_label} を実行する"
                + ("（主役 AI 機能）" if flow.highlight else ""),
                detail=(
                    f"入力データ: {flow.data} に "
                    f"{flow.capability_label}（{flow.capability}）が効く"
                ),
            )
        )

    for connector in composition.connectors:
        steps.append(
            DemoStep(
                order=len(steps) + 1,
                title=f"{connector} に結果を連携する",
                detail=f"{connector} コネクタ経由で実行結果を通知/共有する",
            )
        )

    steps.append(
        DemoStep(
            order=len(steps) + 1,
            title="構成サマリをエクスポートして顧客提示資料に転用する",
            detail="本サマリ（構成図・使う OCI サービス・手順・効果）を Markdown で出力する",
        )
    )
    return steps


# --- ④想定効果 -------------------------------------------------------------


#: capability → 想定効果のフレーズ。決定的フォールバック文は **active な部品の効果だけ** を並べる
#: (未組込の機能の効果を顧客提示文に書かない＝捏造しない)。
CAPABILITY_EFFECTS: dict[str, str] = {
    "rag.search": "社内文書からの根拠付き回答で一次対応を高速化",
    "summarize": "長い記録の要約で把握にかかる時間を短縮",
    "classify": "自動分類で仕分け・トリアージを省力化",
    "draft": "返信ドラフト生成で起案の手間を削減",
    "nl2sql": "自然言語の照会で非エンジニアでも必要なデータを取得",
    "chart": "集計結果のグラフ化で示唆を可視化",
    "agent": "業務エージェントで複数手順をまたぐ作業を自動化",
    "vlm.ocr": "帳票・画像の読み取りで手入力作業を削減",
    "minutes": "音声議事録の自動生成で記録作成を省力化",
}


def _deterministic_impact(composition: DemoComposition) -> str:
    """GenAI 不在/失敗時の決定的な想定効果テンプレ文(フォールバック)。

    効果は **active な AI 部品(実際に組み込まれた capability)だけ** から合成する。未組込の機能名・
    効果は出さない(顧客提示文で未実装機能を約束しない=捏造しない)。
    """
    app_name = composition.app_name or composition.sample_app or "本デモ"
    parts = "、".join(capability_label(c) for c in composition.active_parts) or "AI 機能"
    # 主役を先頭にして、active な部品の効果フレーズだけを並べる(未 active の効果は書かない)。
    ordered = [c for c in composition.active_parts]
    if composition.highlight in ordered:
        ordered.remove(composition.highlight)
        ordered.insert(0, composition.highlight)
    effects = [CAPABILITY_EFFECTS[c] for c in ordered if c in CAPABILITY_EFFECTS]
    effect_sentence = (
        f"具体的には、{'、'.join(effects)}できる。" if effects else ""
    )
    return (
        f"「{app_name}」に {parts} を組み込むことで、現場の業務データをそのまま AI で活用できる。"
        f"{effect_sentence}"
        "固定リファレンス基盤（OCI Generative AI / Autonomous Database）上で動くため、"
        "本番展開時もガバナンスを保ったまま拡張できる。"
    )


def impact_prompt(composition: DemoComposition) -> str:
    """想定効果を GenAI で文章化するためのプロンプト(呼び出し側が使用)。"""
    parts = "、".join(capability_label(c) for c in composition.active_parts)
    highlight = capability_label(composition.highlight) if composition.highlight else ""
    return (
        "あなたは OCI のプリセールス担当です。以下のデモ構成について、顧客提示用の"
        "「想定効果」を日本語で簡潔に（3〜4文、箇条書きにしない散文）述べてください。"
        "誇張や未実装機能の約束はせず、組み込んだ AI 部品が業務にどう効くかに絞ってください。\n\n"
        f"- デモアプリ: {composition.app_name or composition.sample_app}\n"
        f"- 主役 AI: {highlight}\n"
        f"- 組み込む AI 部品: {parts}\n"
        f"- 連携: {composition.connectors or 'なし'}\n"
    )


# --- Markdown エクスポート --------------------------------------------------


def summary_to_markdown(summary: DemoSummary) -> str:
    """構成サマリを、プリセールス資料の下敷きになる Markdown へ整形する。"""
    lines: list[str] = []
    lines.append(f"# 構成サマリ: {summary.app_name}（{summary.sample_app}）")
    lines.append("")
    meta = [f"UI/出力: {summary.ui or '-'}", f"シード方針: {summary.seed_strategy}"]
    if summary.connectors:
        meta.append(f"連携: {', '.join(summary.connectors)}")
    lines.append(" / ".join(meta))
    lines.append("")

    lines.append("## ① 構成図（どのデータに何の AI が効くか）")
    if summary.diagram:
        for f in summary.diagram:
            mark = "★ " if f.highlight else "- "
            lines.append(f"{mark}{f.line}")
    else:
        lines.append("- （実行可能な AI 組込点なし）")
    lines.append("")

    lines.append("## ② 使う OCI サービス（固定リファレンス基盤の該当部分）")
    for s in summary.oci_services:
        lines.append(f"- **{s.service}** — {', '.join(s.used_for)}")
    lines.append("")

    lines.append("## ③ デモ手順")
    for step in summary.steps:
        lines.append(f"{step.order}. **{step.title}** — {step.detail}")
    lines.append("")

    lines.append("## ④ 想定効果")
    lines.append(summary.impact)
    lines.append("")

    if summary.excluded:
        lines.append("## 注記（構成から外した AI 部品）")
        for e in summary.excluded:
            lines.append(f"- {e.get('capability')}（{e.get('status')}）: {e.get('reason')}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --- 公開 API: サマリ生成 --------------------------------------------------


def build_summary(
    composition: DemoComposition,
    *,
    narrative: str | None = None,
) -> DemoSummary:
    """合成済みデモ構成から構成サマリを**決定的に**組み立てる(副作用なし)。

    `narrative` を渡せば想定効果(④)にその文章を使う(GenAI 文章化の結果)。None なら決定的な
    テンプレ文へフォールバックする。構成図・OCI サービス・手順は常に決定的に導出する。

    `composition.ok` が False(合成不能)の構成は呼び出し側で弾く前提(本関数は ok 構成専用)。
    """
    if not composition.ok or composition.sample_app is None:
        raise ValueError("build_summary は合成成立済み(ok=True)の構成にのみ使える")

    impact_text = (narrative or "").strip()
    if impact_text:
        impact_source = "genai"
    else:
        impact_text = _deterministic_impact(composition)
        impact_source = "deterministic"

    summary = DemoSummary(
        sample_app=composition.sample_app,
        app_name=composition.app_name or composition.sample_app,
        ui=composition.ui,
        connectors=list(composition.connectors),
        highlight=composition.highlight,
        seed_strategy=composition.seed.strategy,
        diagram=_diagram(composition),
        oci_services=_oci_services(composition),
        steps=_steps(composition),
        impact=impact_text,
        impact_source=impact_source,
        active_parts=list(composition.active_parts),
        excluded=list(composition.excluded),
        markdown="",
    )
    summary.markdown = summary_to_markdown(summary)
    return summary
