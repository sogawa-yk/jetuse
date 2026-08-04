# 実環境で回さなかった経路と、その代替検証・残存リスク

## 1. TTS の第1候補失敗 → us-phoenix-1 フォールバック

- **未実施の理由**: 検証テナンシ（DEPLOYTEST）は `ap-osaka-1` / `us-chicago-1` / `ca-toronto-1` を購読し、
  デプロイ先の `us-chicago-1` で TTS が提供されているため、実配備ではフォールバックが発火しない。
  発火させるには、TTS 未提供リージョン（大阪）へアプリ一式を配備し直すか、テナンシを
  `us-phoenix-1` に追加購読する必要がある。リージョン購読はテナンシ全体に効く不可逆な変更のため実施しない。
- **代替検証**:
  - 実サービスへの直接呼び出しで、リージョンごとの可否が `synthesize_speech` と `list_voices` で
    一致することを実測（`us-chicago-1` = 200 / `ap-osaka-1` = 404 / `ca-toronto-1` = 404）。
  - フォールバックの分岐自体は単体テストで固定
    （`test_falls_back_to_next_region_when_unavailable` /
    `test_resolved_region_stays_first_but_keeps_other_candidates` /
    `test_tts_probe_caches_and_falls_back_across_regions`）。
- **残存リスク**: 大阪デプロイでの「大阪404 → Phoenix成功」の**実配備**での通し確認は未実施。
  大阪へ配備する機会に `POST /api/tts` と `GET /api/health` の `tts.region` を確認すること。

## 2. ORA-28007（DBブートストラップ）の**修正前**の再現と**修正後**の解消

- 再現は実環境で確認済み（`us-chicago-1` の検証スタックで、イメージ更新によるコンテナ再作成時に
  `ALTER USER ... IDENTIFIED BY <同じ値>` が `ORA-28007` になり、bootstrap が
  「ADB 未準備」を繰り返して `migrate()` に到達しない状態を観測）。
- 修正後は、同一スタックで**コンテナを3回作り直しても** `/api/health` の
  `dbchat.select_ai.ok` が `true` になり、`status` が `ok` になることを確認した。
- **残存リスク**: なし（同一経路を実環境で通している）。

## 3. `enable_dynamic_group=false` / `enable_runtime_policy=false` など IAM 分割パターン

- 本検証はユーザー指示どおり「テナンシ管理者が既定値でデプロイする」経路に絞った。
- 既存 IAM を流用する分割パターンは未実施。ただし本差分の IAM 変更は runtime policy の
  statement 追加のみで、分割時に事前作成する statement 一覧は
  `docs/setup/public-iam-requirements.md` に追記済み。
- **残存リスク**: 事前作成済み policy を使う利用者が、追加された
  `generative-ai-response` / `generative-ai-conversation` を手で足さないと同じ 404 に当たる。
  ドキュメントとトラブルシュート表で明示している。

## 4. デモスコープ（`/api/demos/{demo_id}/...`）の実操作

- **未実施の理由**: E2E は通常経路（`/api/rag/files` / `/api/chat/stream`）を通しており、
  デモスコープのアップロード・チャットは UI から到達するまでにデモ定義の作成が要るため
  今回のシナリオに含めなかった。
- **本差分での変更**: API Gateway に `/api/demos/{p*}` の read_timeout=300 ルートを追加した
  （通常経路の `/api/rag/*` と同じ理由・同じパターン。通常経路側は実機で 504 解消を確認済み）。
- **残存リスク**: ルート追加自体は `terraform validate` と apply 成功までで、デモスコープの
  初回アップロードが 300 秒ルートに載ることの**実測**は未実施。

## 5. 受け入れた残存リスク（修正しない判断）

- **バケット掃除の best-effort 性**: destroy 時の cleanup は `on_failure = continue` で、各 CLI 失敗も
  警告を出して継続する。ここで destroy 全体を止めると「掃除に一度失敗したら二度と壊せない」に
  なるため。失敗時は警告がログに残り、後続のバケット削除エラー（409）で気づける。
- **オブジェクト名に `|` や `"` を含む場合**: multipart 一覧の解析が壊れうる。JetUse が書くオブジェクト名
  （file id / ファイル名ベース）では発生しないが、利用者が手で置いた特殊名では abort し損ねる。
  その場合はコンソールから該当アップロードを中断してから destroy する。
- **UserPasswordChanger の応答喪失**: 履歴違反は推測で成功扱いにしない方針のため、この稀なケースでは
  apply が失敗する。復旧はスタック変数 `demo_password_version` を変えて再 apply（エラーメッセージにも記載）。
