# TOOL-01 実環境 E2E サマリ

実行環境: 共有 loop ADB の run 固有スキーマ `JETUSE_TOOL01_68D006` / dev コンパートメント。
架空の業務 API(Object Storage + PAR)・Vault・OCI Generative AI(gpt-oss-120b)は実物。
JetUse 側に業務ロジックは持たせていない(渡す口だけ)。

- PASS — シナリオ1(外部ツールをエージェントが自分で呼ぶ)
- PASS — シナリオ2(Vault 経由の秘密が届く・平文を残さない)
- PASS — シナリオ3(否定: 内部を向く URL の登録が拒否される)
- PASS — シナリオ4(タイムアウトが失敗として伝わる)
- PASS — シナリオ5(サイズ上限超過が失敗として伝わる)
- PASS — シナリオ6(圧縮された巨大応答も上限で止まる)

## 検証で作った資源(すべて `jetuse-spike-tool01` 接頭辞)

- Object Storage バケット `jetuse-spike-tool01-68d006`(stock.json / big.json / bomb.json + PAR 3 本)
- Vault 秘密 `jetuse-spike-tool01-apikey`(検証用の使い捨てトークン)
- ADB スキーマ `JETUSE_TOOL01_68D006`

片付けは `spikes/tool01/teardown.py --yes` と `spikes/ragm02/teardown.py --yes`。
