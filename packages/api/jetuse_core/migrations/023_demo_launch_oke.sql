-- BE-01: デモ起動の実 OKE 配備配線。demo_launch に配備メタ（非秘密のみ）を追加する。
-- launch が build_deploy_spec→render→kubectl apply まで進んだとき、配備した namespace と
-- 配備状態（validated=dry-run 検証済み / deployed=実 apply 済み）・クラスタ内 URL・注入トークンの
-- 失効時刻（非秘密メタ）を記録し、デモ削除 API（namespace 撤去）や棚卸しに使う。
-- すべて NULL 許容で追加し、**後方互換** を保つ（OKE 配備 OFF の従来 launch は NULL のまま）。
-- 短期トークン本体・Vault OCID は **保存しない**（描画側の secret 分離を保つ。失効時刻のみ）。
-- 冪等性: migrate.py が schema_migrations に version を記録し再適用はスキップする。
-- 単文（セミコロン終端）。ADD は複数列を 1 文でまとめる（コメント内にセミコロンを置かない）。

ALTER TABLE demo_launch ADD (
  namespace VARCHAR2(63),
  deploy_status VARCHAR2(32),
  cluster_url VARCHAR2(512),
  token_expires_at VARCHAR2(64)
)
