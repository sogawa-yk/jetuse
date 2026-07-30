# 片付け（この run が作ったものだけを消し、消えたことを再照会で確認した）

```console
$ .venv/bin/python -u runs/2026-07-30T1800_PREP-02/e2e/driver.py teardown
== 片付け ==
  対象 ADB: jetuse-loop-adb / jetuse:dev（OCID 一致）/ AVAILABLE
  所有権の照合: JETUSE_PREP02D770 はこの run が作ったもの（USER_ID・作成時刻・マーカーが一致）
  dropped JETUSE_SPIKE_PREP02D770_IDX
  dropped JETUSE_SPIKE_PREP02D770_IDXX
  dropped JETUSE_RAGIDX_73590213
  dropped JETUSE_SPIKE_PREP02D770_PROF
  dropped JETUSE_SPIKE_PREP02D770_PROFX
  dropped JETUSE_RAG_73590213
  確認: user_tables に JETUSE_SPIKE_PREP02D770_IDX$VECTAB は 0 件
  確認: user_tables に JETUSE_SPIKE_PREP02D770_IDXX$VECTAB は 0 件
  確認: user_cloud_ai_profiles に JETUSE_SPIKE_PREP02D770_PROF は 0 件
  確認: user_cloud_ai_profiles に JETUSE_SPIKE_PREP02D770_PROFX は 0 件
  確認: user_tables に JETUSE_RAGIDX_73590213$VECTAB は 0 件
  確認: user_cloud_ai_profiles に JETUSE_RAG_73590213 は 0 件
  dropped bucket jetuse-spike-prep02d770-rag
  確認: バケット jetuse-spike-prep02d770-rag の存在=False
  DROP 直前の再照合: JETUSE_PREP02D770 はこの run が作ったもの（USER_ID・作成時刻・マーカーが一致）
  dropped user JETUSE_PREP02D770
  dropped user JETUSE_PREP02D770_Q
  確認: ユーザー JETUSE_PREP02D770 の存在=False
  確認: ユーザー JETUSE_PREP02D770_Q の存在=False
done（作ったものはすべて削除され、再照会でも見つからない）
```

## マネージド側（Files API / Vector Store）

シナリオ3 の `rag.add_file` が GenerativeAI プロジェクト側にも箱を作る。これは ADB にも
バケットにも属さないので、名前で同定して個別に消した（**プロジェクト自体は既存のもので、
作っても消してもいない**）。

```console
$ .venv/bin/python  # cleanup-genai.log
vector store: jetuse-rag-prep02-prep02d770 vs_<REDACTED>…
  deleted file file-<REDACTED>…
  deleted file file-<REDACTED>…
  deleted vector store
vector store: jetuse-rag-prep02-prep02d770 vs_<REDACTED>…   ← 1 回目の実行が途中で失敗して残った箱
  deleted vector store
確認: 残存 []
```

ウォレットと検証用スキーマのパスワードファイル（`/tmp/prep02_wallet_*`）も削除した。
残っている `created-resources.json` は run タグと owner 名の記録だけで、実リソースは無い。
