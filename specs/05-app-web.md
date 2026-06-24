# specs/05 — Phase 1 フロントエンド（APP-02: React SPA骨格）

状態: ドラフト（2026-06-10作成）
仕様参照: specs/00-architecture.md / specs/01-design-system.md（SPIKE-07） / ADR-0004

## [APP-02] React SPA骨格（OCIコンソール風）

### 目的

SPIKE-07ギャラリーのトークン・部品をアプリシェルに昇格し、**OCIコンソールのルック&フィールに似せた**（ユーザー指示 2026-06-10）レイアウト骨格を作る。

### デザイン方針（OCIコンソール風）

- **ダークヘッダ**（ほぼ黒の暖色系 #161513）+ ハンバーガーで開閉する**白い左ナビドロワー** + **ライトグレーの本文背景**（コンテンツは白カード）
- プライマリボタンは**ほぼ黒**（Redwoodのcall-to-action調）、リンク/アクションは控えめなブルー、Oracle Red系はブランドマークに限定
- フラット・細罫線・角丸控えめはSPIKE-07トークンを継承。branding.jsonによるリブランドも維持
- ダークモードは全トークンを反転定義（SPIKE-07の .dark を拡張）

### ルーティング

- **HashRouter採用**。静的ホスティング（API GW+Object Storage）のディープリンク404問題（ADR-0004 検証3）を**ルーティング側で解消**する（GWルート追加・SPAフォールバック不要）
- ルート: `#/`（ホーム=ユースケースカード一覧）/ `#/chat`（空チャット画面）/ `#/settings` / `#/design`（SPIKE-07ギャラリー温存）

### 構成

```
packages/web/src/
  main.tsx          # Router + Providers
  App.tsx           # シェル(Header + SideNav + Outlet)
  theme.css         # OCIコンソール風トークン(ヘッダ・リンク・ナビ追加)
  branding.ts       # 既存(リブランド)
  i18n.tsx          # 最小i18n(ja/en辞書 + useT)。既定ja、localStorage保持
  auth.tsx          # 認証スタブ(VITE_AUTH_REQUIRED=false既定)。INFRA-02でPKCE実装に差替
  components/
    layout.tsx      # Header / SideNav / PageContainer
    gallery.tsx     # 既存部品(流用)
  pages/
    home.tsx        # ユースケースカード一覧(チャット/RAG/議事録/NL2SQL/設定)
    chat.tsx        # 空チャット画面(メッセージ空状態+入力欄。送信はPhase 2 CHAT-03)
    settings.tsx    # テーマ・言語・ブランド切替
```

### 要件

1. ダークモード: トグルで `.dark` 切替、localStorage永続化
2. i18n: ja/en の辞書雛形。言語切替UIは設定ページとヘッダ
3. 認証: `auth.tsx` はAPP-01と同じ思想のフラグ式スタブ（既定で `dev-user`）。ログイン画面はINFRA-02で実装
4. レスポンシブ: ナビは狭幅でオーバーレイドロワー化
5. lint / `npm run build` クリーン（コミット前チェック）

### 完了条件

- [ ] `npm run lint` / `npm run build` クリーン
- [ ] preview（port 4173）でホーム→チャット画面の遷移・ダーク切替・言語切替が動作
- [ ] 静的ホスティング（jetuse-dev-spa）へデプロイし、API GW経由でハッシュルーティング遷移が成立（ADR-0004 検証3の結論として追記）
- [ ]（INFRA-02後）PKCEログイン→チャット画面のE2E

### 成果物

- `packages/web/` 更新一式、`docs/verification/APP-02.md`
- ADR-0004へ「ディープリンク=ハッシュルーティングで解消」を追記

### 禁止事項

- OCID等のコミット（フロントに秘密値は持たせない）
