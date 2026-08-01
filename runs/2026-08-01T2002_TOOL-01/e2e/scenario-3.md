# シナリオ3(否定) — 内部を向く URL の登録が拒否される

**確かめたこと**: この機能は SSRF の入口になりうる。登録の時点で https 以外・内部メタデータ・
ループバック・私有レンジ・URL 埋め込みの認証情報を **400 で拒否**し、1 件も保存しない。
判定は `mcp_servers.validate_url` と同じ経路(`jetuse_shared.webtools.assert_public_host`)で、
名前解決の結果が私有/ループバック/リンクローカル/予約/マルチキャストなら弾く(fail-closed)。

| 種別 | URL | HTTP | 応答 |
|---|---|---|---|
| インスタンスメタデータ | `https://169.254.169.254/opc/v2/instance/` | 400 | blocked address: 169.254.169.254 -> 169.254.169.254 |
| メタデータ(旧IP形式) | `https://169.254.169.254/latest/meta-data/` | 400 | blocked address: 169.254.169.254 -> 169.254.169.254 |
| ループバック | `https://127.0.0.1:8000/internal` | 400 | blocked address: 127.0.0.1 -> 127.0.0.1 |
| localhost 名前解決 | `https://localhost/internal` | 400 | blocked address: localhost -> ::1 |
| 私有レンジ | `https://10.0.0.10/admin` | 400 | blocked address: 10.0.0.10 -> 10.0.0.10 |
| 平文 http | `http://example.com/api` | 400 | ツールのURLはhttpsである必要があります |
| URL に認証情報 | `https://user:pass@example.com/api` | 400 | URLに認証情報を含めることはできません(秘密はVaultへ) |

登録後のツール一覧: ['lookup_inventory', 'echo_with_secret', 'echo_without_secret']
- 1 件も保存されていない: **True**

> 実行時にも同じ検証を通す(登録後に DNS が内部へ向いても止まる)。
> 単体テスト `test_execution_revalidates_host` で固定。

判定: **PASS**
