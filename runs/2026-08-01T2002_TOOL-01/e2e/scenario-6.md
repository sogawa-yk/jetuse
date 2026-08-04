# シナリオ6 — 圧縮された巨大応答も上限で止まる

**確かめたこと**: 上限をバイト数で測る以上、「小さく送って大きく展開させる」応答
(圧縮爆弾)で上限判定をすり抜けられてはいけない。JetUse は `Accept-Encoding: identity` で
圧縮を要求せず、Content-Length の申告が上限を超えていれば 1 バイトも読まず、読む場合も
**足す前に**測る。

- 相手: 実 Object Storage 上の `bomb.json`
  (`Content-Encoding: gzip` / 展開後 5,000,000 バイト。送られる量は数 KB)
- 上限: `http_tools.MAX_RESPONSE_BYTES` = 128,000 バイト

応答 `POST /api/agent/execute-tool`: HTTP 400

```
ツール実行に失敗しました: 応答が上限(128000バイト)を超えました
```

判定: **PASS**
