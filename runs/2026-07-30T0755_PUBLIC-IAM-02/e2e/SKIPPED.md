# 実環境で確認できなかった範囲と、その理由・代替検証・残存リスク

## 1. TTS（`/api/tts` と `/api/health` の `ok: true`）

- **状況**: 受け入れ E2E 39項目のうち 4 件が TTS 起因で FAIL（`capability: tts` / `TTS: 音声合成` /
  `TTS: health が実合成の結果を反映` / それに引きずられた `/api/health` 全体）。
- **未実施の理由**: 配備先 `us-chicago-1` の OCI Speech `list_voices` が**テナンシ管理者の
  ユーザープリンシパルでも HTTP 500** を返す（3回連続で再現）。`ca-toronto-1` は 404（未提供）、
  `us-phoenix-1` はテナンシ未購読で 401。管理者権限でも失敗する以上、**この構成の IAM 不足では
  説明できない**（原因が OCI 側の障害なのか、テナンシ固有の状態なのかまでは特定していない）。
  リージョン購読の追加はテナンシ全体に効く不可逆な変更なので実施しない。
- **代替検証**: OCI SDK で `list_voices` を3リージョンへ直接実行し、権限ではなくサービス側の問題で
  あることを切り分けた。JetUse 側の縮退（`/api/tts` が 503 + 理由、health が `unavailable` + ヒント、
  他機能は無影響）は設計どおり動作している。
- **残存リスク**: **本タスクの対象（IAM 分割構成）で TTS が動くことは示せていない**。
  ただし TTS の権限は runtime policy の `manage ai-service-speech-family` 1文で、同じ文で動く
  Speech の STT 系（`リアルタイムSTT: セッション作成` / `議事録: 音声登録`）は PASS しているため、
  IAM 起因の可能性は低い。TTS が提供されているリージョンで再確認すること。
- **案内への反映**: 受け入れ判定を「`tts` 以外の capability がすべて `ok`」とし、TTS を使う構成でのみ
  `/api/health ok:true` と合成成功を条件に加えるよう
  `docs/setup/public-deploy-dedicated-compartment.md` §5 に明記した。

## 2. 最小ポリシー（2文）だけでの apply 通し実行

- apply は6文（`inspect compartments` / `inspect tenancies` / `read objectstorage-namespaces` +
  コンパートメント3文）を持つ状態で実施した。
- **代替検証**: plan は `inspect tenancies` + コンパートメント3文だけで成功、
  destroy は `inspect tenancies` + `manage all-resources` の**2文だけ**で 181 リソースを削除できた。
- **残存リスク**: apply 固有の API が別のテナンシスコープ権限を要求する可能性は残る（plan と destroy が
  同じ data source と同じコンパートメント資源を扱うため、可能性は低い）。

## 3. ADB ウォレット取得経路の直接確認

- `db.py::_wallet_bytes` はコンテナ起動時に `get_namespace()` を呼ぶ。namespace 権限を外した状態で
  **コンテナを再起動して bootstrap をやり直す**試験は実施していない。
- **代替検証**: 当該権限を一度も持たない新規プリンシパルで `GetNamespace` が成功することを確認した
  （`necessity.md` 2-3）。同じ API を同じ条件で呼ぶため、再起動でも成功すると判断した。

## 4. コンソール（ブラウザ）からのデプロイ経路

- Stack 作成・変数入力・plan/apply/destroy はすべて OCI CLI で実施した（認可経路は同一）。
- **残存リスク**: コンソール画面でコンパートメントを選択・表示するために
  `inspect compartments in tenancy` が本当に要るかは実測していない。案内では「推奨」として記載。

## 5. Semantic Store（SQL Search）

- `semstore_ocid` 未設定の既定構成で検証した（DBチャットは Select AI 経路で PASS）。
- Semantic Store を使う構成での分割IAM検証は対象外（チケットの「実施しないこと」に明記）。
