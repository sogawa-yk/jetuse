# シナリオ3（回帰）— 通常の xlsx は従来どおり

上限に掛からない通常のブック `jetuse-spike-prep04-サンプル在庫連携API仕様書-通常.xlsx`（複数シート・空シート・行の飛び）。
セル内分割の追加で**既存の挙動が変わっていない**ことを見る。

- 取り込み: file_id `0ec81f8e-3d54-43ca-bdc9-207611be245b` / バックエンド `{'vector_store': 'indexed', 'select_ai': 'pending', 'opensearch': 'disabled', 'adb': 'indexed'}`

## 抽出（`POST /api/extract`）

```
API一覧 | B12:D13 | part=None | エンドポイント	メソッド	説明...
制約 | C5:E5 | part=None | レート制限	600 req/min	超過時は HTTP 429 を返す...
制約 | C40:E40 | part=None | 同時接続数	50	IP 単位で計数する...
```

- チャンクごとに (シート, セル範囲) が異なる（PREP-01 の粒度のまま）: **True**
- 分割していないチャンクに `part` は付かない（既存の鍵は増えていない）: **True**
- 空シート `作業用` はチャンクを作らない: **True**

## 検索と引用

質問: `レート制限は1分あたり何リクエストですか`

```
0ec81f8e-3d54-43ca-bdc9-207611be245b-1 | sheet=制約 | cells=C5:E5 | score=0.6256 | レート制限	600 req/min	超過時は HTTP 429 を返す...
0ec81f8e-3d54-43ca-bdc9-207611be245b-2 | sheet=制約 | cells=C40:E40 | score=0.463 | 同時接続数	50	IP 単位で計数する...
0ec81f8e-3d54-43ca-bdc9-207611be245b-0 | sheet=API一覧 | cells=B12:D13 | score=0.3767 | エンドポイント	メソッド	説明...
```

```
600 req/minです。超過した場合はHTTP 429を返します。
```

- ヒットの (シート, セル範囲) がチャンクごとに異なる: **True** → `[('制約', 'C5:E5'), ('制約', 'C40:E40'), ('API一覧', 'B12:D13')]`
- 回答が現行の レート制限 600 req/min に基づく: **True**
- 引用件数: **5**

判定: **PASS**
