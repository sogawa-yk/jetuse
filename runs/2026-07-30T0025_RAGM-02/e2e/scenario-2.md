# シナリオ2 — 業務表と JOIN したベクタ検索（1 クエリ）

サンプル業務表 `RAGM02_DOC_REGISTRY`（文書管理台帳: 所管部門・機密フラグ）を作り、
チャンク表とベクタ検索を**同じ 1 本の SQL** で結合した。機密扱いの文書は結合条件で落ちる。

```
SELECT c.doc_file, r.owner_dept, r.confidential, c.sheet_name, c.cells,
       ROUND(VECTOR_DISTANCE(c.embedding, :q, COSINE), 4) AS dist
FROM rag_adb_chunks c
JOIN RAGM02_DOC_REGISTRY r ON r.doc_file = c.doc_file
WHERE c.owner_sub = :o AND c.current_version = 'Y' AND r.confidential = 'N'
ORDER BY VECTOR_DISTANCE(c.embedding, :q, COSINE)
FETCH FIRST 5 ROWS ONLY
```

実行結果（doc_file | owner_dept | confidential | sheet | cells | 距離）:

```
サンプル在庫連携API仕様書.md | 情報システム部 | N | 本文 | L1:L4 | 0.4438
サンプル在庫連携API仕様書.md | 情報システム部 | N | 本文 | L4:L6 | 0.5169
サンプル在庫連携API仕様書.md | 情報システム部 | N | 本文 | L6:L7 | 0.5482
```

- 返った文書の所管部門: `['情報システム部']` / 機密フラグ 'Y' の混入: **0 件**

判定: **PASS**

> 業務データ側の条件（機密・所管）で候補を絞ったうえで類似度順に返している。
> マネージド Vector Store では業務表と結合できない（ADR-0020 の比較表）。
