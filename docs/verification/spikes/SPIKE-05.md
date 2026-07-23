# SPIKE-05: Conversations / Projects / 記憶検証

実施日: 2026-06-10 / リージョン: ap-osaka-1 / 実行: `spikes/spike05_conversations.py`

## 目的

Responses APIのConversations（サーバー側会話状態）、Projects（記憶分離）の挙動を実機確認し、「履歴の正はADB、Responses API側状態は実行時コンテキスト」とする設計の妥当性を判定する。

## 結果

| 検証項目 | 結果 |
|---|---|
| `conversations.create`（metadata付き） | ✅ `conv_kix_...` 形式ID。metadataに任意KVを保存可 |
| `responses.create(conversation=...)` マルチターン | ✅ turn1の事実（好きな果物=りんご）をturn2で正答。サーバー側状態保持を確認 |
| `conversations.items.list` | ✅ user/assistantメッセージに加え **reasoningアイテムも取得可**（6件/2ターン） |
| `conversations.retrieve` / `delete` | ✅ |
| `GET /conversations`（一覧） | ❌ 404。**一覧APIは存在しない**（OpenAI互換仕様どおり）。会話IDの管理はアプリ側DB必須 |
| Project分離 | ✅ 別の `GenerativeAiProject` をヘッダ指定すると同一会話へのアクセスは404。**Project単位の完全分離を確認** |
| `previous_response_id` 方式 | ✅ conversationを作らずレスポンスチェーンでも状態引き継ぎ可 |
| 短期記憶圧縮フラグ | metadata `short_term_memory_optimization`（既定 false）が自動付与される。`true` 指定で作成可。圧縮挙動の definitivな確認は長い会話が必要で未実施 |

## 設計判定（ADR-0002案の根拠）

**「履歴の正はADB、Responses API側は実行時コンテキスト」設計は妥当**。理由:

1. **一覧APIがない**: ユーザーの会話一覧・検索・タイトル表示はOCI側だけでは実装不可能。アプリDB（ADB）に会話メタデータを持つことは必須。
2. **itemsは取得可能**: 障害時の突き合わせやエクスポートはOCI側からも可能だが、保持期間・上限が未文書化のため、表示用履歴をOCI側に依存するのは危険。
3. **二重管理のコストは低い**: アプリはストリーミング応答を受信した時点で全文を持っているため、ADBへの保存は追加APIコールなしで可能。
4. **Conversationsを使う価値はある**: マルチターンの再送信（毎回全履歴をinputに詰める方式）が不要になり、入力トークン課金と実装の両方を節約できる。File Search等のツール状態も会話に紐づく。
5. **Projects はテナント分離機構として使える**: ユーザー/ワークスペース単位で `GenerativeAiProject` を分ければ、OCI側でも会話・ファイルがハード分離される。最小構成では「アプリ全体で1 Project」、エンタープライズ構成では「テナントごとに1 Project」の二段構えを提案。

採用方針（specs/00へ反映）:

- ADB: CONVERSATIONS（タイトル、ユーザー、モデル、作成/更新日時、OCI conversation_id）+ MESSAGES（全文、usage、引用）
- OCI Conversations: 推論時のコンテキスト管理に利用。アプリからは conversation_id の対応付けのみ保持
- 会話削除時はADBとOCI両方を削除（GDPR的要件にも対応）

## 残課題

- 会話の保持期間・アイテム数上限の確認（長期運用前に要確認、ドキュメント未記載）
- `short_term_memory_optimization=true` の実際の圧縮挙動（長い会話での検証はPhase 2 CHAT-02で）
- reasoningアイテムの扱い（UIに出すか、トークン課金への影響）

## 残置リソース

- conversation 1件（jetuse-spike-project内）、GenerativeAiProject `jetuse-spike-project2`（分離検証用）
