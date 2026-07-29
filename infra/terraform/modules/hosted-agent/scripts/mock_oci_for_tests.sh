#!/bin/sh
# PORT-03 スクリプトの経路確認用モック。MOCK_CASE で応答を切り替える。
uri=""; method=""
while [ $# -gt 0 ]; do
  case "$1" in --target-uri) uri="$2"; shift 2;; --http-method) method="$2"; shift 2;; *) shift;; esac
done
case "$MOCK_CASE" in
  foreign)  # 同名だが他人のアプリが1件
    case "$uri" in
      *"displayName="*) echo '{"data":{"items":[{"id": "ocid1.generativeaihostedapplication.oc1..foreign","lifecycleState": "ACTIVE"}]}}';;
      *hostedApplications/*) echo '{"data":{"id": "ocid1.generativeaihostedapplication.oc1..foreign","lifecycleState": "ACTIVE","freeformTags":{"jetuse-owner":"jetuse:someone-else"}}}';;
    esac;;
  owned_second)  # 先頭が他人・2件目が自分（head -1 だけ見ると取り違える）
    case "$uri" in
      *"displayName="*) echo '{"data":{"items":[{"id": "ocid1.generativeaihostedapplication.oc1..foreign","lifecycleState": "ACTIVE"},{"id": "ocid1.generativeaihostedapplication.oc1..mine","lifecycleState": "ACTIVE"}]}}';;
      *..foreign) echo '{"data":{"id": "ocid1.generativeaihostedapplication.oc1..foreign","lifecycleState": "ACTIVE","freeformTags":{"jetuse-owner":"jetuse:someone-else"}}}';;
      *..mine) echo '{"data":{"id": "ocid1.generativeaihostedapplication.oc1..mine","lifecycleState": "ACTIVE","freeformTags":{"jetuse-owner":"jetuse:p03","jetuse-config":"FP"}}}';;
    esac;;
  list_fail)  # 一覧が権限エラー
    echo "ServiceError: NotAuthorizedOrNotFound status: 404" >&2; exit 1;;
esac
