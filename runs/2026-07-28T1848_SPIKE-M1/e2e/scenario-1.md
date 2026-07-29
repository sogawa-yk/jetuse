# シナリオ1（③ の版フィルタ）— PASS

実環境: 共有 loop ADB `jetuse-loop-adb` / ap-osaka-1 / Oracle AI Database 26ai 23.26.3.1.0 /
スキーマ `JETUSE_SPIKE_M1`。モックなし。

## 実行コマンド

```
PYTHONPATH=spikes/spike_m1 .venv/bin/python spikes/spike_m1/method_c_own_index.py
```

生ログ全文: `method-c-own-index.log`

## 期待

架空チャンク 10 件（旧版 3 件）を実 ADB へ投入し、`WHERE current_version = 'Y'` を付けた
ベクタ検索で旧版が 1 件も返らないこと。対照としてフィルタ無しでは旧版が返ること。

## 実結果

投入: `投入結果 (current_version, 件数): [('N', 3), ('Y', 7)]`

| 検索 | ヒット（上位5） | 旧版ヒット |
|---|---|---|
| A: フィルタ無し | c01, **c08**, **c09**, c05, c03 | **2 件** `['c08','c09']` |
| B: `WHERE current_version='Y'` | c01, c05, c03, c02, c07 | **0 件** `[]` |
| C: `… AND kind='constraint'` | c05, c07, c06 | 0 件 |

スクリプトの判定行:

```
A(フィルタ無し) 旧版ヒット: ['c08', 'c09']
B(版フィルタ)   旧版ヒット: []
PASS: 版フィルタ 1 本の SQL で旧版を完全排除
```

検索は 1 本の SQL（クエリ埋め込み → メタデータ絞り込み → ベクタ類似検索）で完結している。
SQL 全文は生ログ ③-4 節に貼ってある。

## 但し書き

10 行しかないため実行計画は `TABLE ACCESS STORAGE FULL` + `filter("CURRENT_VERSION"='Y')` で、
HNSW 索引は使われていない。フィルタが効いていることの証明にはなるが、
大規模データでの索引併用は本シナリオでは検証していない（SKIPPED.md 参照）。
