# ADR-0005: 実行基盤はOCI Functions優先、SSEストリーミング経路のみContainer Instances

日付: 2026-06-10
状態: 承認済み（2026-06-10 分割粒度を含めユーザー承認）

## 決定

- **非ストリーミングAPIはOCI Functionsで実装する**（ユーザー指示 2026-06-10）
  - 例: 会話履歴CRUD（ADB）、ファイルアップロード/前処理（docx→テキスト等）、Vector Store管理、STTバッチ起動、TTS生成、設定・ユースケース定義の配信
- **SSEストリーミング系（チャット/エージェント応答）のみContainer Instances（FastAPI）を維持する**
- 両者とも同一API Gatewayの背後に置く（`/api/chat/*` → CI、その他 `/api/*` → Functions）

## 理由 — FunctionsにSSEを載せられない（公式仕様で確定）

1. **応答ストリーミング非対応**: Functionsの応答は実行完了後に一括返却。チャンク/SSE送出の仕組みがない
2. **応答ペイロード上限6MB**（超過時 502 `FunctionInvokeResponseBodyTooLarge`）、リクエストも6MB上限
3. **同期実行タイムアウト最大300秒**（長時間実行はDetached=非同期+response destination方式であり、HTTP応答としては返せない）
4. コールドスタートはTTFT要件（標準モデルで0.8s）と相性が悪い

→ ストリーミングという中核要件に対してFunctionsは技術的に不成立のため、ここだけCIを残すのは「不自然でない」例外と判断。

## 実装方針

- 共通ロジック（IAM署名クライアント生成=`spikes/common.py` 系、ADB接続、JWT検証）は**共有Pythonパッケージ**に切り出し、FunctionsとCIの両方から利用（二重実装の禁止）
- Functionsは応答6MB上限があるため、ファイルダウンロード系はObject StorageのPARを返す設計にする
- JWT検証はAPI GWのオーソライザーFunctionに集約する案をINFRA-02で検討

## 影響

- specs/00のシステム構成図を更新（本ADRとADR-0004を反映）
- INFRA-01のTerraformに Functions Application / OCIRリポジトリ（`jetuse-spike-` → 本番プレフィックスは別途）を追加
- SPIKE-02で実証済みのSSE経路（API GW→CI、ADR-0003）は変更なし
- Phase 1のAPP-01は「共有パッケージ + Functionsハンドラ + CIのFastAPI」の3点構成でスケルトンを切る
