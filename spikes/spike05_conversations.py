"""SPIKE-05: Conversations / Projects / 記憶検証。

  1. Conversations API: 作成 → responses.create(conversation=...) のマルチターン
  2. 会話アイテムの取得（items.list）と構造
  3. Project分離: 別Projectから同じconversationにアクセスできないことの確認
  4. Conversations APIのlist可否、メタデータ

実行: .venv/bin/python spikes/spike05_conversations.py
"""
import json

from common import make_client, make_cp_client, BASE_URL_OPENAI, COMPARTMENT_ID, ENV

MODEL = "openai.gpt-oss-120b"


def section(t):
    print(f"\n{'=' * 70}\n## {t}\n{'=' * 70}", flush=True)


def main():
    client = make_client(timeout=120.0, with_project=True)

    section("1. Conversation 作成")
    conv = client.conversations.create(metadata={"app": "jetuse-spike", "user": "spike05"})
    print(f"[OK] conversation: {conv.id} metadata={conv.metadata}")

    section("2. マルチターン（サーバー側状態保持の確認）")
    r1 = client.responses.create(
        model=MODEL, conversation=conv.id,
        input="私の好きな果物はりんごです。覚えてください。")
    print(f"turn1: {(r1.output_text or '')[:80]}")
    r2 = client.responses.create(
        model=MODEL, conversation=conv.id,
        input="私の好きな果物は何でしたか？一語で答えてください。")
    a2 = r2.output_text or ""
    print(f"turn2: {a2[:80]}")
    print(f"[{'OK' if 'りんご' in a2 else 'NG'}] サーバー側で会話状態が保持されている")

    section("3. 会話アイテム取得（履歴エクスポート可否）")
    try:
        items = client.conversations.items.list(conversation_id=conv.id)
        for it in items.data:
            d = it.model_dump()
            kind = d.get("type")
            role = d.get("role", "-")
            content = str(d.get("content"))[:60]
            print(f"  {kind} role={role} {content}")
        print(f"[OK] items list: {len(items.data)}件")
    except Exception as e:
        print(f"[NG] items list: {type(e).__name__}: {str(e)[:200]}")

    section("4. Conversation list / retrieve / metadata")
    try:
        got = client.conversations.retrieve(conversation_id=conv.id)
        print(f"[OK] retrieve: id={got.id} created_at={got.created_at}")
    except Exception as e:
        print(f"[NG] retrieve: {str(e)[:150]}")
    # list APIはOpenAIにも無い。OCI独自であるか確認
    import httpx
    from oci_genai_auth import OciUserPrincipalAuth
    c = httpx.Client(auth=OciUserPrincipalAuth(),
                     headers={"CompartmentId": COMPARTMENT_ID,
                              "OpenAi-Project": ENV["PROJECT_OCID"]}, timeout=30)
    r = c.get(BASE_URL_OPENAI + "/conversations")
    print(f"GET /conversations (list): {r.status_code} {r.text[:120]}")

    section("5. Project分離の確認（別Projectから同一会話へアクセス）")
    import subprocess
    res = subprocess.run(
        ["oci", "generative-ai", "generative-ai-project", "create",
         "-c", COMPARTMENT_ID, "--display-name", "jetuse-spike-project2",
         "--query", "data.id", "--raw-output"],
        capture_output=True, text=True)
    p2 = res.stdout.strip()
    print(f"project2: {p2[:60]}...")
    import time
    time.sleep(10)
    c2 = httpx.Client(auth=OciUserPrincipalAuth(),
                      headers={"CompartmentId": COMPARTMENT_ID, "OpenAi-Project": p2},
                      timeout=30)
    r = c2.get(f"{BASE_URL_OPENAI}/conversations/{conv.id}")
    isolated = r.status_code == 404
    print(f"[{'OK' if isolated else 'NG'}] 別Projectからのアクセス: {r.status_code} "
          f"{'（分離されている）' if isolated else r.text[:150]}")
    r = c.get(f"{BASE_URL_OPENAI}/conversations/{conv.id}")
    print(f"同一Projectからのアクセス: {r.status_code}")

    section("6. conversation無し（ステートレス）+ previous_response_id 方式")
    try:
        ra = client.responses.create(model=MODEL, input="私の名前は山田です。覚えてください。")
        rb = client.responses.create(model=MODEL, input="私の名前は？一語で。",
                                     previous_response_id=ra.id)
        ab = rb.output_text or ""
        print(f"[{'OK' if '山田' in ab else 'NG'}] previous_response_id方式: {ab[:60]}")
    except Exception as e:
        print(f"[NG] previous_response_id: {type(e).__name__}: {str(e)[:200]}")

    print(f"\nconversation_id={conv.id}（残置）")


if __name__ == "__main__":
    main()
