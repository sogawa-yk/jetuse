---
id: ER-0005
title: Word / PowerPoint を取り込めるようにする
status: parked
size: M
source: 気づき
created: 2026-08-04
ticket: 
pr: 
---

## ひとことで

提案書や設計書の多くは Word / PowerPoint だが、いまは取り込めない。

## 何ができないか

取り込みは PDF / テキスト / Excel / 画像に対応しているが、**Word と PowerPoint は未対応**。

## 根拠

`rag.ALLOWED_EXTENSIONS` に含まれていない。マネージド側も Office 形式を受け付けない
（実測: `Unsupported file type 'docx'`）ため、**Excel と同じく抽出してから渡す**必要がある。

## どう直すか

Excel（シート・セル範囲）やスキャン文書（ページ）と同様に、
**出典として意味のある単位**（Word なら見出し / PowerPoint ならスライド番号）で分割する。

## やらない場合の代償

顧客資料の多くが Word / PowerPoint なので、**デモを作れない案件が出る**。
