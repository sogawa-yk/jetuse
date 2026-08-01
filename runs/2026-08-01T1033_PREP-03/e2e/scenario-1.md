# シナリオ1 — スキャン PDF（テキスト層なし）が検索でヒットする

架空の 2 ページのスキャン PDF `jetuse-spike-prep03-設備点検報告書スキャン.pdf` を**アプリ経路**
（`POST /api/rag/files` → `rag.add_file` → OCR → 各バックエンド）で取り込んだ。

- file_id: `7c3adb2b-4c4a-4adf-abba-a0be6d75d35b` / 取り込み状態: **completed**
- バックエンド別の取り込み状況: `{'vector_store': 'indexed', 'select_ai': 'pending', 'opensearch': 'disabled', 'adb': 'indexed'}`
- このアップロードで走った OCR（**1 回**。マネージド変換と ADB 取り込みで二重に呼ばない）:

```
[
  {
    "engine": "ocr",
    "bytes": 134189,
    "pages": 2,
    "seconds": 3.3,
    "mean_confidence": 0.991
  }
]
```

## 検索（`rag_adb.search` / `current_version='Y'`）

質問: `冷却ポンプの交換部品コードと交換期限は何ですか`

```
7c3adb2b-4c4a-4adf-abba-a0be6d75d35b-1 | sheet=p.2 | cells=L1:L9 | score=0.6831 | 是正処置および交換部品...
7c3adb2b-4c4a-4adf-abba-a0be6d75d35b-0 | sheet=p.1 | cells=L1:L10 | score=0.6132 | 架空商事株式会社...
```

- 本文の語 `BRG-7781` を含むチャンクがヒットする（空白を無視して比較）:
  **True**
- その出典のページ: **p.2**（2 ページ目にしか書いていない語 → 期待 `p.2`）:
  **True**

## 実 API 経路（`POST /api/chat/stream` / `rag_backend="adb"`）

```
交換部品コードはBRG-778、交換期限は2026年6月30日までです。
```

引用（`citations[].source`）:

```
[
  {
    "file_id": "7c3adb2b-4c4a-4adf-abba-a0be6d75d35b",
    "filename": "jetuse-spike-prep03-設備点検報告書スキャン.pdf",
    "score": 0.6831,
    "source": {
      "chunk_id": "7c3adb2b-4c4a-4adf-abba-a0be6d75d35b-1",
      "chunk_no": 1,
      "file": "jetuse-spike-prep03-設備点検報告書スキャン.pdf",
      "version": "11.0",
      "sheet": "p.2",
      "cells": "L1:L9",
      "sha256": "8b1e97193f86",
      "kind": "doc",
      "current_version": "Y",
      "attributes": {
        "source": "upload",
        "ext": "pdf"
      }
    },
    "text": "是正処置および交換部品\n処置区分:\n予防保全(計画停止中に実施)\n交換部品コード:\nBRG - 778 1\n交換期限:\n2026年6月30 日まで\n備考:\n次回点検で振動値の再測定を行う。"
  },
  {
    "file_id": "7c3adb2b-4c4a-4adf-abba-a0be6d75d35b",
    "filename": "jetuse-spike-prep03-設備点検報告書スキャン.pdf",
    "score": 0.6132,
    "source": {
      "chunk_id": "7c3adb2b-4c4a-4adf-abba-a0be6d75d35b-0",
      "chunk_no": 0,
      "file": "jetuse-spike-prep03-設備点検報告書スキャン.pdf",
      "version": "11.0",
      "sheet": "p.1",
      "cells": "L1:L10",
      "sha256": "8b1e97193f86",
      "kind": "doc",
      "current_version": "Y",
      "attributes": {
        "source": "upload",
        "ext": "pdf"
      }
    },
    "text": "架空商事株式会社\n設備点検報告書\n点検日:\n2026年5月14日\n対象設備:\n第2 工場 冷却ポンプ P - 20 4\n点検者:\n保全課 山田\n所見:\n軸受部の振動値が管理上限を超過していた。"
  }
]
```

- 引用のページが構造化された値（`p.N`）で載る: **True** → `['p.2', 'p.1']`
- 引用の出所が**このスキャン PDF だけ**（他文書が混ざっていない）: **True**
  → `['jetuse-spike-prep03-設備点検報告書スキャン.pdf']`
- 回答が本文の `2026年6月30日` に基づく: **True**

### 生成回答が識別子を忠実に写すか（判定条件ではない・隠さず測る）

- この実行で回答が原文の `BRG-7781` を保った: **False**
- **これは実行ごとに揺れる**。同じ入力・同じ手順で 2 回測ったところ、1 回目の回答は
  `BRG-778` と末尾の `1` を落とし、2 回目は一致した。原因は OCR が `BRG - 778 1` と
  空白を挟むこと（`docs/verification/PREP-03.md` §7）。**引用として返るチャンク本文も
  空白入りのまま**（上の `citations[].text`）で、原文そのままではない。ただし空白を除けば
  原文と一致する（＝文字は落ちていない）。生成側がそれをどう読むかで揺れる。
- したがって本タスクが実証したのは「**スキャン文書の本文が検索でヒットし、出典にページ番号が載る**」
  ことであって、「生成回答が識別子まで忠実に写す」ことではない（**それは保証しない**）。
  識別子の忠実性は前処理（OCR 出力の正規化）か生成側（引用の逐語化）の別タスク。→ 残課題

判定: **PASS**
