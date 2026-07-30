# シナリオ 3（追加）: 画面から xlsx を選べる（RAGM03-002 の是正の確認）

実施日時: 2026-07-31 01:26 JST / 実ブラウザ（Chromium・Playwright MCP）
※ review-4（PASS）の後に、Codex が「xlsx の実選択が未検証」と指摘した点を埋めるために取得した。
   **コードは変更していない**（証跡の追加のみ）。

実行:

```js
document.querySelector('input[type=file]').accept
[...document.querySelectorAll('p')].find(p => p.textContent.includes('対応形式')).textContent
```

実結果:

```json
{"accept": ".pdf,.txt,.md,.xlsx",
 "supportedText": "対応形式: PDF / テキスト / Markdown / Excel（.xlsx。20MBまで）"}
```

API 側の受け口（`jetuse_core.rag.ALLOWED_EXTENSIONS` = pdf / txt / md / xlsx）と一致した。
以前は `accept=".pdf,.txt,.md"` で、サーバが受け付ける xlsx をファイル選択で選べなかった。

**実アップロードまでは未実施**: この作業環境には ADB 接続情報が無く（`SKIPPED.md` 2）、
`POST /api/rag/files` が台帳書き込みで 503 になるため、取り込みの往復は成立しない。
xlsx の実取り込み（セル範囲つき出典が返ること）は PREP-01 が実 ADB で実施・記録済み
（`docs/verification/PREP-01.md`）。

**判定: PASS**（画面の受入形式が API と一致し、xlsx を選べる）
