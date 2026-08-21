-- VID-07 映像本体を **API Gateway に通さずに** 登録するための列(specs/20 §2)。
--
-- ゲートウェイの本文上限は **20 MiB**(2026-08-20 実測)。4K の素材はそこに入らないので、
-- 本体は「発行した書き込み専用 PAR へブラウザが直接 PUT する」経路に変える。
-- その経路は **2 段**(URL を貰う → 上げ終えたら確定する)になるため、
-- 「行はあるが本体はまだ無い」という中間状態が生まれる。それを表す列。
--
-- **`analysis_state` に混ぜない。** あれは「分析がどこまで進んだか」を表す列で、
-- 'uploading' を足すと「分析していないのか、本体がまだ無いのか」が同じ値に潰れる。
-- 既存行は 'ready'(既定値) —— multipart 経路で入った映像は本体が既にある。
--
-- **1 文だけにする**(024 / 025 と同じ理由)。DDL は 1 文ごとに暗黙 commit されるので、
-- 複数文にすると「片方だけ適用され、schema_migrations には記録が無い」状態を作れる。
ALTER TABLE video_assets ADD (
  upload_state VARCHAR2(32) DEFAULT 'ready' NOT NULL
    CONSTRAINT video_assets_upload_ck CHECK (upload_state IN ('uploading', 'ready'))
)
