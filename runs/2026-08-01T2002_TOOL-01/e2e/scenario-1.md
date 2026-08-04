# シナリオ1 — 外部 HTTP ツールをエージェントが自分で呼ぶ

**確かめたこと**: 素の HTTP エンドポイント(架空の在庫 API)を JSON Schema つきで登録し、
`POST /api/chat/stream` の agent 実行に `http_tool_ids` で渡すと、**モデルが自分の判断で
それを呼び**、JetUse がサーバー側で代理実行した結果が最終回答に反映される。

- 相手: Object Storage の PAR(実在の https エンドポイント。JetUse 側に業務ロジックは無い)
- 組込ツールは 1 つも渡していない(`enabled_tools: []`)ので、この答えは登録した外部ツール
  からしか得られない。

## 登録

```
{
  "id": "7985ad44-c526-4952-ace9-56d0065bc88c",
  "name": "lookup_inventory",
  "description": "社内在庫システムに品番を問い合わせ、在庫数・保管倉庫・ロット番号を返す。品番ごとの在庫を聞かれたら必ずこれを使う(社外の情報源では答えられない)",
  "parameters": {
    "type": "object",
    "properties": {
      "part_number": {
        "type": "string",
        "description": "品番(例 JX-7742)"
      }
    },
    "required": [
      "part_number"
    ]
  },
  "url": "https://objectstorage.ap-osaka-1.oraclecloud.com/p/…/n/idqcucnenh88/b/jetuse-spike-tool01-68d006/o/stock.json",
  "method": "GET",
  "auth_header": "Authorization",
  "has_auth": false
}
```

一覧 `GET /api/agent/http-tools`:

```
[
  {
    "id": "7985ad44-c526-4952-ace9-56d0065bc88c",
    "name": "lookup_inventory",
    "description": "社内在庫システムに品番を問い合わせ、在庫数・保管倉庫・ロット番号を返す。品番ごとの在庫を聞かれたら必ずこれを使う(社外の情報源では答えられない)",
    "parameters": {
      "type": "object",
      "properties": {
        "part_number": {
          "type": "string",
          "description": "品番(例 JX-7742)"
        }
      },
      "required": [
        "part_number"
      ]
    },
    "url": "https://objectstorage.ap-osaka-1.oraclecloud.com/p/…/n/idqcucnenh88/b/jetuse-spike-tool01-68d006/o/stock.json",
    "method": "GET",
    "auth_header": "Authorization",
    "has_auth": false
  }
]
```

## 質問

```
品番 JX-7742 の在庫数・保管倉庫・ロット番号を教えてください。
```

## モデルが起こしたツール呼び出し

```
[
  {
    "name": "lookup_inventory",
    "label": "外部API: lookup_inventory",
    "arguments": "{\n  \"part_number\": \"JX-7742\"\n}",
    "call_id": "call_947bb3ac499bf226",
    "status": "running"
  }
]
```

## 代理実行の結果(モデルへ返した内容)

```
[
  {
    "call_id": "call_947bb3ac499bf226",
    "name": "lookup_inventory",
    "preview": "{\"status\": 200, \"body\": \"{\\\"part_number\\\": \\\"JX-7742\\\", \\\"stock_qty\\\": 137, \\\"warehouse\\\": \\\"大阪第2倉庫\\\", \\\"lot\\\": \\\"LOT-2026-0731\\\", \\\"as_of\\\": \\\"2026-08-01\\\"}\"}"
  }
]
```

## 最終回答

```
品番 **JX‑7742** の在庫情報は以下の通りです。

| 項目 | 内容 |
|------|------|
| 在庫数 | **137** 個 |
| 保管倉庫 | **大阪第2倉庫** |
| ロット番号 | **LOT‑2026‑0731** |
| 情報取得日 | 2026‑08‑01 |

ご確認ください。他に何かご質問やご要望がありましたらお知らせください。
```

- ツールが呼ばれた: **True**
- 在庫数 137 と保管倉庫 大阪第2倉庫 が回答に載った: **True**

判定: **PASS**
