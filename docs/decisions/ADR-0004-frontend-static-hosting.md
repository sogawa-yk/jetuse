# ADR-0004: フロントエンドはAPI Gateway + Object Storageの静的サイトホスティングとする

日付: 2026-06-10
状態: 承認済み（2026-06-10 ユーザー指示・同日承認。経路の実機検証はPhase 1 INFRA-01で実施）

## 決定

React SPAは本家JetUse（CloudFront + S3）と同様に**静的サイトホスティング**で配信する。OCIにはCloudFront相当のCDNサービスがないため、構成は **API Gateway → Object Storage** とする。

- ビルド成果物（`npm run build` の `dist/`）をObject Storageバケットに配置
- 既存のAPI Gateway（ADR-0003でSSE経路として確定済み）に静的配信ルートを追加し、**1つのGWでフロントとAPIを同一オリジン配信**する
  - `/api/{path*}` → FastAPIバックエンド
  - `/{object*}` → Object Storage（HTTPバックエンドでオブジェクトURLへマッピング）
- 同一オリジンになるため**ブラウザ⇔APIのCORS設定が不要**になる（副次メリット）

## 理由

- ユーザー指示（2026-06-10）: AWS版と同じ静的ホスティング方式とし、OCIではAPI GW → Object Storageで代替する
- SPA配信のためだけにコンテナ/ネイティブサーバーを常駐させない（運用・課金の削減）

## INFRA-01での実機検証結果（2026-06-10、詳細は docs/verification/INFRA-01.md）

1. パスマッピング: **成立**。`/{object*}` → PAR基底URL + `${request.path[object]}` で200応答
2. アクセス方式: **非公開バケット + 読取専用PAR（AnyObjectRead, リスト不可）で確定**。注意: `bucket_listing_action="Deny"` をTerraformで明示するとAPIが値を返さず毎applyでPAR再作成（=URL変化）になるため未指定とする
3. ディープリンク: **404になることを実測確認** → **APP-02でハッシュルーティング採用により解消**（`#/chat` 等はクライアント側解決でサーバーリクエストが発生しない。GWルート追加・フォールバック不要）
4. ヘッダ: **オブジェクトメタデータがそのまま返る**。アップロード時の `--content-type`（必要ならCache-Control）指定がデプロイ手順の要件

## 影響

- SPIKE-07の `packages/web/` はそのまま流用（ビルド成果物の置き場が変わるだけ）
- Terraformモジュール（INFRA-01）に「静的サイト配信」モジュールを追加
- CDNキャッシュは無いため、アセットはファイル名ハッシュ（Vite標準）でキャッシュバスティングし、ブラウザキャッシュに委ねる
