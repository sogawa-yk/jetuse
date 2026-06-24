# AGT-05 検証レポート: 長期メモリ統合（OCIネイティブ）

日付: 2026-06-11
仕様: specs/11-agents.md [AGT-05] / ADR-0006（Accepted） / 前提調査: SPIKE-10, SPIKE-10b
状態: **実機E2E完了**（イメージ0.14.0、jetuse-dev-projectへ移行済み）

## 経緯（ユーザー指摘が決め手）

SPIKE-10で「長期メモリAPIは存在しない」と誤判定 → **ユーザー指摘「プロジェクト作成時に指定しないと使えない仕様」** → 再調査（SPIKE-10b）で確定:

1. プロジェクトの `long-term-memory-config` は**作成時のみ**（更新は「cannot be modified after project creation」と明示エラー）
2. モデル指定は**名前形式**: extraction=`openai.gpt-oss-120b` ○ / llama・command-a ✗、embedding=`cohere.embed-v4.0` ○ / `embed-multilingual-v3.0` ✗ / OCID ✗
3. 利用方法は **`memory_subject_id` を会話metadataに付与**（公式docs share-memory.htm。responsesのパラメータではない — SPIKE-10の探索が空振りした理由）

## 実施内容

- `jetuse-dev-project` 新設: LTM（extraction=gpt-oss/embedding=embed-v4.0）+ **STM condenser（gpt-oss）** + **retention（会話720h/レスポンス168h）** ※retention設定によりCHAT-09の残課題（保持期間の明示管理）も解消
- アプリ変更は1点: 会話作成時の metadata に `memory_subject_id`=JWT sub
- 移行: 旧プロジェクトのoci_conversation_id 11件をNULL化（次の発話で遅延再作成）、RAGファイル（ユーザーアップロード分含む）をOS原本から新プロジェクトへ再取り込み
- スパイクプロジェクト3件削除（jetuse-spike-project / project2 / memproj）

## 実機E2E（API GW経由、イメージ0.14.0）

| ケース | 結果 |
|---|---|
| 会話Aで事実（コードネーム/締切）→ **別の会話B**で質問 | **15秒後に正答想起**「ひまわり、12月です。」 |
| subject分離（直接API） | 別subjectには漏れない |
| RAG回帰（新プロジェクト） | アップロード→取り込み→検索回答OK |
| 既存機能回帰 | 会話CRUD・チャットE2E OK |

## 残課題（Phase 8ガバナンス）

- 記憶の個別削除・開示API未発見（ADR-0006: subjectローテーション案を保持）

## 追補: 会話削除と長期記憶の関係（2026-06-11、ユーザー観察を契機に系統検証）

実機で確定した挙動:

| テスト | 結果 |
|---|---|
| 事実→源泉会話を削除（中間想起なし）→ 別会話で質問 | **忘却**（削除後120秒まで一貫して想起されず） |
| 事実→会話Bで想起（事実が復唱される）→ 源泉Aのみ削除 | **記憶が残る**（復唱した会話Bが新たな記憶源泉になるため） |

結論: **長期記憶は源泉の会話に紐づき（provenance）、会話削除で派生記憶も消える**。これはCHAT-09（会話削除のOCI同期 — GDPR的要件を意図して実装）が起点となり、OCI側の未文書のカスケード挙動が働いた結果。「忘れられる権利」の主経路として機能する。

注意（ガバナンス上の含意）: 事実が複数会話で言及されている場合、**全ての言及会話を消さないと忘却されない**。完全消去にはユーザーの全会話削除（またはADR-0006のsubjectローテーション案）が必要。
