// 固定 API クライアント(specs/19 §4.3 S3(a))。
// HTTP を発行してよいのは本モジュールのみ。生成コードはここの関数だけを呼ぶ。
// ベースは配信 pathname の `/app` 境界より前 = /api/demos/{id}。SPA サブルート
// (/app/foo/ 等・末尾スラッシュ含む全境界) でも当該デモのスコープ内 API に閉じる
// (codex-review R10-F004 — `new URL("..")` は /app/foo/ で /app に誤解決していた)。
export function deriveBase(pathname) {
  return pathname.replace(/\/app(\/.*)?$/, "");
}
const BASE = deriveBase(window.location.pathname);

// 使用モデルは信頼ビルド相が VITE_DEMO_MODEL 環境変数で焼き込むビルド時定数
// (Vite が import.meta.env.VITE_* を文字列リテラルへ置換)。CSP default-src 'self' は
// インライン script を許さず window.__DEMO_CONFIG__ 方式は黙って既定へ落ちるため不採用
// (ADR-0023 §6・codex-review R9-F008)。
// 値は **API の MODELS レジストリの公開キー(短縮名 "gpt-oss-120b" 等)** であり、OpenCode 生成側の
// OCI モデル id ("openai.gpt-oss-120b") とは別名前空間。ビルド時に MODELS キー + プラン能力
// (chat 系は rag=true 非対応 等)へ検証して焼き込む (codex-review R11-F002。未検証値は 400 になる)。
const MODEL = import.meta.env.VITE_DEMO_MODEL || "gpt-oss-120b";

// 認証(ADR-0023 §3.5): AUTH_REQUIRED=false の Internal プレビュー(dev-user)では HTML・アセット・
// 能力 API がそのまま通る(SP3-03 の実 E2E はこの構成)。AUTH_REQUIRED=true では、親が /app-session で
// 得た一回性コードで /app/?c= を開くと配信ルートが HttpOnly Cookie(app-session)を発行し、以降の
// HTML・アセット・能力 API を Cookie で認可する(require_app_or_user。サーバ側は SP3-03 実装済み・
// 単体契約テスト済み。実トークン・ブラウザ全経路 E2E は SP3-05)。fetch は credentials:"same-origin" で
// Cookie を自動送信し、CSRF 用に固定ヘッダ X-JetUse-App を常時付ける。トークンはバンドルに焼き込まない
// (S4)。setAuth は AUTH オフのプレビュー/将来の別方式用 seam(既定 Cookie 経路では未使用)。
let _auth = null;
export function setAuth(bearerToken) {
  _auth = bearerToken;
}

function headers() {
  const h = { "content-type": "application/json", "x-jetuse-app": "1" };
  if (_auth) h.authorization = `Bearer ${_auth}`;
  return h;
}

async function postJSON(path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    credentials: "same-origin",
    headers: headers(),
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
  return res.json();
}

// SSE を読み、フレームごとに handler(evt) を呼ぶ。data: {"error": ...} は例外に変換し、
// data 行の JSON 破損も握りつぶさず失敗させる(":" コメント行=keep-alive は SSE 仕様どおり無視)。
// [DONE] 未受信の EOF(途中切断)も例外。reader は finally で必ず解放する。
async function postSSE(path, body, onEvent) {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    credentials: "same-origin",
    headers: headers(),
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) throw new Error("接続が途中で切断されました");
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data:")) continue; // SSE コメント(keep-alive)・空行
        const data = line.slice(5).trim();
        if (data === "[DONE]") return;
        const evt = JSON.parse(data); // 破損フレームはそのまま throw(握りつぶさない)
        if (evt.error) throw new Error(String(evt.error));
        onEvent(evt);
      }
    }
  } finally {
    reader.cancel().catch(() => {});
  }
}

/** チャット(SSE)。messages=[{role,content}] の全履歴を毎回送る(会話は保存しない契約)。
 *  systemPrompt はプランの block.system_prompt — system メッセージとして先頭に付与。
 *  戻り値 = 完成した応答文字列。onDelta(delta, full) で逐次描画。 */
export async function chat(messages, onDelta, { systemPrompt, model = MODEL, rag = false } = {}) {
  const msgs = systemPrompt ? [{ role: "system", content: systemPrompt }, ...messages] : messages;
  let full = "";
  await postSSE("/chat", { model, messages: msgs, rag }, (evt) => {
    if (typeof evt.delta === "string") {
      full += evt.delta;
      onDelta?.(evt.delta, full);
    } // citations 等の非 delta フレームは無視してよい(表示は任意)
  });
  return full;
}

/** RAG 検索: デモの箱の文書に閉じた検索付きチャット(SSE)。 */
export function ragSearch(query, onDelta, opts = {}) {
  return chat([{ role: "user", content: query }], onDelta, { ...opts, rag: true });
}

/** DB 照会: 自然言語 → SQL 生成(SSE で data:{"sql"}) → 読取専用実行(JSON)。
 *  戻り値 = {sql, columns, rows, row_count}。onSql(sql) で生成 SQL を先に描画できる。 */
export async function dbChat(question, onSql) {
  let sql = "";
  await postSSE("/dbchat/nl2sql", { question }, (evt) => {
    if (typeof evt.sql === "string") {
      sql = evt.sql;
      onSql?.(sql);
    }
  });
  if (!sql) throw new Error("SQL が生成されませんでした");
  const result = await postJSON("/dbchat/execute", { sql });
  return { sql, ...result };
}
