# シナリオ2 — Vault 経由の秘密が認証ヘッダとして届く

**確かめたこと**: 登録時には秘密そのものを渡さず **Vault の OCID だけ**を渡す。代理実行の
瞬間に JetUse が Vault から読み、指定ヘッダに載せて送る。DB にも API 応答にも平文は現れない。

- 秘密: Vault `jetuse-spike-tool01-apikey`(OCID 参照。値はランダム文字列。この証跡に実値は書かない)
- 相手: `https://postman-echo.com/get`(**リクエストヘッダをそのまま返す**公開エンドポイント)

## 登録(認証あり / なしの対照)

```
[
  {
    "id": "bec2e083-6e20-4f57-aa14-66bdf29d4aab",
    "name": "echo_with_secret",
    "description": "ヘッダをそのまま返す検証用API",
    "parameters": {
      "type": "object",
      "properties": {},
      "required": []
    },
    "url": "https://postman-echo.com/get",
    "method": "GET",
    "auth_header": "X-Api-Key",
    "has_auth": true
  },
  {
    "id": "a63f5b5d-7a97-420d-b250-81c7fdbb8796",
    "name": "echo_without_secret",
    "description": "同じAPIを認証なしで登録したもの",
    "parameters": {
      "type": "object",
      "properties": {},
      "required": []
    },
    "url": "https://postman-echo.com/get",
    "method": "GET",
    "auth_header": "Authorization",
    "has_auth": false
  }
]
```

> 応答に `auth_secret_ocid` は出ない(`has_auth` だけ)。`mcp_servers` と同じ流儀。

## 否定: 許可されていない秘密は登録できない

登録者に紐づいていない(freeform タグ `jetuse_tool_owner` が一致しない)既存 Secret の OCID を
指定して登録を試みた。これが通ると、サービスの OCI 権限で読める秘密を利用者が指定した外部 URL
へ送らせられる(confused deputy)。

- 対象: このコンパートメントにある**アプリ運用用の別 Secret**(値は一切読んでいない)
- 結果: HTTP 400

```
{
  "detail": "この秘密の利用が許可されていません(Vault の freeform タグ jetuse_tool_owner に利用者を設定してください)"
}
```

- 拒否された: **True**

## 相手が受け取ったヘッダ(= 呼び出し元へ返った本文)

相手はヘッダをそのまま返すが、JetUse は**送った秘密の実値だけ**を `<redacted>` に
置き換えてから返す。伏せ字が `x-api-key` の位置に現れることは、

1. その値が Vault の秘密と**完全一致していた**(= 正しく届いた)
2. かつ**呼び出し元・モデル・会話履歴に平文が出ない**

の両方を同時に示す。

認証ありツールの応答本文:

```
{
  "host": "postman-echo.com",
  "x-forwarded-proto": "https",
  "accept": "application/json, */*",
  "user-agent": "jetuse/0.1 (agent tool proxy)",
  "x-api-key": "<redacted>",
  "accept-encoding": "gzip, br"
}
```

認証なしツールの応答本文:

```
{
  "host": "postman-echo.com",
  "x-forwarded-proto": "https",
  "user-agent": "jetuse/0.1 (agent tool proxy)",
  "accept-encoding": "gzip, br",
  "accept": "application/json, */*"
}
```

- 秘密が届いた(伏せ字が一致位置に出た): **True**
- 応答本文に平文が出ていない: **True**
- 認証なしでは付かない: **True**

## DB の中身(所有者のツール行)

```
[["echo_with_secret", "X-Api-Key", "ocid1.vaultsecret.oc1.ap-osaka-1.…"], ["echo_without_secret", "Authorization", null], ["lookup_inventory", "Authorization", null]]
```

- DB に平文の秘密が無い: **True**(保持しているのは Vault の OCID だけ)
- API 応答に平文の秘密が無い: **True**

## 承認後の実行は id で名指しする

名前だけで再解決すると、承認待ちの間に同名で別 URL・別 Secret のツールへ差し替えられる。
`http_tool_id` を付けずに実行した結果: HTTP 400(id 必須: **True**)

判定: **PASS**
