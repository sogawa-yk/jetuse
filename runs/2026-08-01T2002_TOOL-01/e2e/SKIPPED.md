# TOOL-01 実環境 E2E — 実施しなかった範囲と理由

tasks/TOOL-01.md の E2E シナリオ 3 本はすべて実施した(scenario-1/2/3)。加えて完了条件の
「タイムアウト・サイズ上限が黙って切り詰められない」を実環境で確かめた(scenario-4/5)。
上限の抜け道になる圧縮応答も実測した(scenario-6)。
以下は**意図的に実施していない範囲**とその理由。無言のスキップはない。

## 1. 配備済みスタック(Container Instance / API Gateway)への再デプロイ

area=api の `deploy_cmd` は `ops/start-adb-if-stopped.sh && python -m jetuse_core.migrate`
であり、この変更は **API のコードと ADB のスキーマ**にしか及ばない(Terraform 変更なし)。
実行したのは共有 loop ADB への実マイグレーション適用(`e2e/deploy.log`)で、E2E は
**実 ADB・実 Vault・実 Object Storage・実 OCI Generative AI** に対して FastAPI アプリを
そのまま起動して実行した(`spikes/prep03/e2e.py` と同じ流儀)。

未実施なのは「ブラウザから配備済み URL を叩く」経路のみ。この機能は**画面を持たない**
(下記 2)ので、その経路で新たに確かめられることが無い。

## 2. SPA(画面)からの登録 UI

タスクの成果物は「ツール登録・実行のモジュール、ルート、テスト」であり、画面は含まれない。
デモ側は `POST /api/agent/http-tools` を直接叩いて登録する。`/api/agent/tools`(ツール選択
UI 用の一覧)には外部 HTTP ツールを**載せていない**(所有者ごとの資源なので別ルート
`GET /api/agent/http-tools` に分けた)。画面が要るなら別タスク。

## 3. 承認モード(`auto_tools=false`)でエージェントが外部ツールを呼ぶ往復

外部 HTTP ツールは既定で `requires_approval=True` なので、承認モードでは
`tool_call: pending_approval` が UI へ出て、承認後に `POST /api/agent/execute-tool` で実行し、
`tool_results` を添えて `/api/chat/stream` を呼び直す。この経路のうち**実行の側**
(`/api/agent/execute-tool` が所有者の外部ツールを解決して代理実行する)は scenario-2/4/5 が
実環境で通している。承認イベントの発火と再開の往復は単体テスト
(`test_http_tools.py::test_stream_agent_executes_external_tool` と既存の
`test_agent.py::test_agent_stream_approval_mode`)で固定した。UI が無い(上記 2)ため
実環境での往復は組めない。承認イベントが `http_tool_id` を返し、承認後の実行が**その id を必須とする**こと
(名前だけの実行は 400 / id が指す行の名前が変わっていれば 409)は
`test_approval_event_carries_tool_id_and_route_honours_it` と
`test_execute_tool_requires_the_approved_tool_id` で固定し、id 必須は実環境でも
確かめている(`scenario-2.md` の末尾)。

## 4. DNS リバインディングの「攻撃側」を実演してはいない

対策は**実装した**(`_pin_target`: 名前解決 1 回 → 返った全アドレスを検証 → その IP へ接続。
Host ヘッダと TLS SNI は元のホスト名)。実環境では、ピン留めした状態で実 Object Storage /
postman-echo に対して**証明書検証込みで通常どおり通信できる**ことを scenario-1/2/4/5 で
確認した(ピン留めが正常系を壊していないことの証跡)。

実演していないのは**攻撃側**——「1 回目の解決で公開 IP、2 回目で内部 IP を返す権威 DNS」を
立てて破られないことを見せる部分。自前の権威 DNS が要り、共有環境では用意できない。
代わりに、解決結果に内部アドレスが 1 つでも混ざれば接続前に止まることを単体テスト
`test_pin_target_rejects_internal_resolution` で、接続先が IP リテラルに差し替わり
Host/SNI が保たれることを `test_connects_to_the_validated_ip` で固定した。

なお `web_fetch` / MCP 側は従来のホスト名検証のままで、この経路だけが強い。共通ライブラリへ
引き上げるかは別タスクの判断(本タスクで既存経路を弱めてはいない)。

## 5. 秘密を第三者エンドポイントへ送っている点(scenario-2 の制約)

「秘密ヘッダが実際に相手へ届いた」ことは、**相手が受け取ったヘッダを見る**以外に実環境で
確かめる方法が無い。手元に「ヘッダ値を検証する自前の公開 https API」が無いため、公開の
エコーサービス(`postman-echo.com`)を使った。送ったのは**この検証のためだけに Vault へ
作った使い捨てトークン**であり、実在の資格情報ではない。証跡にも呼び出し元への応答にも
平文は残らない(JetUse が送った値を `<redacted>` に置換して返すため)。

なお **teardown はこの Vault 秘密を削除しない**(名前が固定で「この run が作った」ことを
証明できず、別 run / 人が管理する同名の秘密を消しうるため — レビュー TOOL-01-004)。
不要になったら人が削除予約する(手順は teardown が出力する)。

## 6. Vault 秘密の「権限剥奪が効くこと」は単体テストどまり

秘密の認可(コンパートメント + freeform タグ `jetuse_tool_owner`)は**登録時と実行時の両方**で
取り直す(review-2 の TOOL-01-003 を受けて実装)。実行時の再確認が効くこと=登録後にタグを
外すと使えなくなることは、単体テスト `test_secret_authorization_is_rechecked_at_execution` で
固定した。実環境で「タグを外す → 実行が落ちる → タグを戻す」まで往復すると、共有の Vault
秘密を一時的に壊れた状態にする(並行する他のループが同じコンパートメントを使う)ため、
実環境では**登録の拒否**(scenario-2 の否定)までを確かめている。
