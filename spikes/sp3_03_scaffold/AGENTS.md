# デモ SPA 生成ルール（JetUse ビルダー）

あなたはこのスキャフォールド上で `demo-plan.json` のデモ画面を実装する。

## 厳守事項

1. **HTTP は `src/api/client.js` の関数（chat / ragSearch / dbChat）だけで行う。**
   `fetch` / `XMLHttpRequest` / `WebSocket` / `EventSource` を直接書かない。
   絶対 URL（http://, https://, //）をコードに書かない。
2. **`src/` 配下だけを編集する。** `package.json`・`package-lock.json`・`vite.config.js`・
   `index.html`・`src/api/client.js`・`node_modules` は変更しない（読み取り専用。信頼ビルド相が原本を使う）。
   依存を追加しない・npm を実行しない。使えるのは同梱の react / react-dom のみ。CSS は素の CSS
   （`src/styles.css` を作成可）。
3. plan の `screens[].blocks[]` を UI にする。block `type` と client 関数の対応:
   - `chat` → `await chat(messages, onDelta, {systemPrompt})`（会話 UI。履歴はメモリ保持で全送信。
     戻り値が完成応答 — 履歴へは戻り値を追加する）
   - `rag.search` → `await ragSearch(query, onDelta)`（検索ボックス + 結果表示）
   - `dbchat` → `await dbChat(question, onSql)`（質問ボックス + 生成 SQL と結果表
     `{sql, columns, rows}` の表示）
   `suggested_prompts` はクリックで入力欄に入るチップとして表示する。
   client 関数は失敗時に throw する — try/catch してエラーメッセージを UI に表示する。
4. UI 言語は日本語。plan の title / description / block title を反映する。
5. **終了条件 = `src/` 配下の実装を書き終えること**。**`npm run build` は実行しない**
   （ビルドは信頼ビルド相が別環境で行う。この環境の node_modules は読み取り専用）。
   構文が妥当な JSX/JS を書く（import は同梱 react / `./api/client.js` / 相対パスのみ）。
6. **各ファイルは完全な内容で書く（既存プレースホルダの import やコメントを残さない）。**
   特に `src/App.jsx` はプレースホルダを**丸ごと置き換える**。**同一ファイルに同じ import を
   二重に書かない**（`import React ...` は 1 ファイルにつき 1 行だけ — 重複は build を壊す）。
