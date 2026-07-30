# シナリオ3 — 版フィルタの対照

同名ファイル `サンプル在庫連携API仕様書.md` を v1 → v2 の順に取り込んだ（再取り込みで旧チャンクは
`current_version='N'` に落ち、版が上がる）。同じ問いを 2 通りで検索した。

## A: フィルタ無し（対照）

```
fileA_v1-1 | version=2.0 | current=N | cells=L4:L6 | ## 第2章 レート制限
レート制限は1分あたり300リクエスト...
fileA-1 | version=1.0 | current=N | cells=L4:L6 | ## 第2章 レート制限
レート制限は1分あたり600リクエスト...
fileA_v2-1 | version=3.0 | current=Y | cells=L4:L6 | ## 第2章 レート制限
レート制限は1分あたり600リクエスト...
fileA_v1-0 | version=2.0 | current=N | cells=L1:L4 | # サンプル在庫連携API仕様書 v1
## 第1章 在庫照会A...
fileA-0 | version=1.0 | current=N | cells=L1:L4 | # サンプル在庫連携API仕様書 v2
## 第1章 在庫照会A...
fileA_v2-0 | version=3.0 | current=Y | cells=L1:L4 | # サンプル在庫連携API仕様書 v2
## 第1章 在庫照会A...
fileA-2 | version=1.0 | current=N | cells=L6:L7 | ## 第3章 データ保持期間
明細データの保持期間は13か月とす...
fileA_v2-2 | version=3.0 | current=Y | cells=L6:L7 | ## 第3章 データ保持期間
明細データの保持期間は13か月とす...
fileA_v1-2 | version=2.0 | current=N | cells=L6:L7 | ## 第3章 データ保持期間
明細データの保持期間は6か月とする...
```

旧版（`current_version='N'`）のヒット: **6 件** `['fileA_v1-1', 'fileA-1', 'fileA_v1-0', 'fileA-0', 'fileA-2', 'fileA_v1-2']`

## B: `current_version='Y'` で絞り込み

```
fileA_v2-1 | version=3.0 | current=Y | cells=L4:L6 | ## 第2章 レート制限
レート制限は1分あたり600リクエスト...
fileA_v2-0 | version=3.0 | current=Y | cells=L1:L4 | # サンプル在庫連携API仕様書 v2
## 第1章 在庫照会A...
fileA_v2-2 | version=3.0 | current=Y | cells=L6:L7 | ## 第3章 データ保持期間
明細データの保持期間は13か月とす...
```

旧版のヒット: **0 件** / 返った版: `['3.0']`

判定: **PASS**（対照 A で旧版が返り、B で 0 件になること）
