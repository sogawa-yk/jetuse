# specs/08 — Phase 3 ユースケースエンジン（UC-01〜03）

状態: ドラフト（2026-06-11作成。CP②通過後に着手）
仕様参照: specs/07（チャット基盤を再利用）/ docs/plan.md §5

## 設計原則

**ユースケース = データ**。テンプレート定義（JSON）だけで新ユースケースを増やせることを証明する。
実行経路は既存の `/api/chat/stream` を再利用（ストリーミング・停止・Markdown/Mermaid描画・パラメータ・usage記録をそのまま継承）。

## [UC-01] プロンプトテンプレートエンジン

### テンプレート定義（正とするスキーマ）

```json
{
  "id": "uuid",
  "name": "要約",
  "description": "文章を指定の長さで要約します",
  "icon": "📝",
  "tags": ["文章"],
  "model": "gpt-oss-120b",          // 省略可(既定モデル)
  "fields": [
    {"name": "text", "label": "本文", "type": "textarea", "required": true,
     "placeholder": "要約したい文章"},
    {"name": "length", "label": "長さ", "type": "select",
     "options": ["1行", "3行", "段落"], "default": "3行"}
  ],
  "template": "次の文章を{{length}}で要約してください。\n\n{{text}}"
}
```

- field.type: `text` | `textarea` | `select` | `number` | `url`（urlはUC-02参照: 送信時にサーバーで本文抽出して値を置換）
- 変数は `{{name}}`。未入力のrequiredは送信不可。テンプレート中の未定義変数は空文字
- 置換はクライアント側で行い、完成プロンプトを `/api/chat/stream` に送る（サーバーに実行専用APIを作らない）

### 永続化（migration 004）

`USECASES(id PK, owner_sub NULL=組み込み, name, description, icon, tags VARCHAR2(400)=カンマ区切り, model, definition CLOB=JSON全体, visibility 'private'|'public', created_at, updated_at)`

- 組み込み(UC-02)はDBに置かず **コード同梱データ**（jetuse_core/usecases_builtin.py）。`builtin: true` で返しUIは編集不可。「データで増やせる」証明はUC-03のユーザー作成で行う
- 一覧 = 組み込み + 自分の + 他人のpublic。取得/実行は同条件、編集/削除は所有者のみ

### API

- `GET /api/usecases` （`?tag=`はクライアントフィルタで代替）/ `POST /api/usecases` / `GET|PUT|DELETE /api/usecases/{id}`
- definitionはサーバーでバリデーション（pydantic: fields型・name重複・template存在）

### UI

- ホームのユースケース一覧を動的化（組み込み+保存済み）。`/uc/{id}` で実行ページ
- 実行ページ: フォーム（スキーマから自動生成）→ 実行 → ストリーミング出力（Mdレンダラ再利用）→ コピー/再実行。モデル・temperature上書き可

### 完了条件

- [ ] pytest（バリデーション/認可/CRUD）+ web build
- [ ] 実機: 組み込みテンプレートをフォームから実行しSSE出力。ユーザー作成テンプレートのCRUD+他ユーザー公開可視性

## [UC-02] 標準ユースケース（組み込みテンプレート5種）

1. **要約**: 本文textarea + 長さselect
2. **執筆・校閲**: 本文 + 指示select（校正/リライト/敬語化/英文校正）
3. **翻訳**: 本文 + 言語select（双方向は自動判定に任せる）
4. **Webコンテンツ抽出**: URL入力（type=url）+ 指示。サーバー `POST /api/tools/extract-url` が取得+本文抽出（**SSRF対策必須**: http/httpsのみ・プライベートIP/リンクローカル/メタデータ(169.254.0.0/16)拒否・リダイレクト先も検証・3MB/15s上限）し、抽出テキストを変数値に置換してから実行
5. **ダイアグラム生成**: 内容textarea + 図種select → mermaidコードを出力（CHAT-03bの描画がそのまま効く）

### 完了条件

- [ ] 実機: 5種すべてフォーム→実行→期待形式の出力。extract-urlはSSRF拒否（169.254.169.254等）を実機確認

## [UC-03] ユースケースビルダー

- `/builder`（新規） `/builder/{id}`（編集）: 基本情報 → フィールド編集（追加/削除/並べ替え、type/label/required/options）→ テンプレート編集（変数挿入ボタン）→ **ライブプレビュー**（同一レンダラでフォーム表示）→ 保存（visibility選択）
- 実行ページから所有テンプレートは「編集」導線
- 完了条件: 非開発者がビルダーで新ユースケースを5分で作れること（**Phase 3出口・人間実演**）
