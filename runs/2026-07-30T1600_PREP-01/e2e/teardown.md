# 検証用リソースの片付け（実施済み）

E2E が使った資源はすべて削除した。削除は**所有を照合してから**行う（照合できなければ何も消さない）。

## 1. OCI 側（`spikes/prep01/teardown.py --yes`）

削除より前に、①登録簿の Vector Store の**実名**が `jetuse-spike-prep01-<run>` と一致すること
②アップロード済みファイル名がすべて `jetuse-spike-prep01-` 接頭辞であること、を確認している
（不一致なら 1 件も消さずに中止する）。

```
削除対象
  file ee486231-… jetuse-spike-prep01-サンプル在庫連携API仕様書.xlsx (file-kix-045…)
  file f0cea26d-… jetuse-spike-prep01-サンプル在庫連携API仕様書.xlsx (file-kix-bed…)
  vector store vs_kix_qc717…（名前: jetuse-spike-prep01-e48127 — 照合済み）
  削除: ee486231-… -> True
  削除: f0cea26d-… -> True
  Vector Store 削除: jetuse-spike-prep01-e48127
```

RAG ファイルの削除は `rag.delete_file`（アプリと同じ経路）なので、Vector Store 内のファイル・
Files API のファイル・Object Storage の原本・ADB のチャンクが同時に消える。

## 2. ADB スキーマ（`spikes/ragm02/teardown.py --yes`）

run 固有スキーマを、所有台帳（USER_ID / 作成時刻 / run 固有マーカーの 3 点）と照合してから削除。

```
接続先確認: jetuse-loop-adb / compartment=dev（承認済み）
台帳と一致（USER_ID / 作成時刻 / マーカーの 3 点）
削除対象のオブジェクト: [('INDEX', 38), ('INDEX PARTITION', 2), ('TABLE', 21), ('TABLE PARTITION', 1)]
DROP USER JETUSE_PREP01_E48127 CASCADE
  ACL: DROP USER で同時に消えた（残 0 件）
  削除後の再照会: ユーザー 0 件 / ACL 0 件（どちらも 0 が期待値）
  ローカルの認証資材を削除: secrets.json, ledger.json, schema.txt, wallet/
```

**ADB は増やしていない**（共有 loop ADB にスキーマだけ足して消した）。
既存リソース（VCN `develop` / インスタンス `dev` / バケット）には触れていない。
