"""サンプルデータ生成+箱への投入(SP3-04 / specs/19 §6)の単体テスト。

fake LLM(builder_data._llm)・fake datasets / fake rag(モジュール属性差し替え)・
fake demo_lease で次を検証する:
- §6.1: 型・件数のサーバ側検証 + 有界再試行 → 失敗(NUMBER の NaN/桁超過含む — 検証は
  信頼境界)。
- §6.2: 文書 ≤64KB・索引完了の有界待機。
- §6.3: 同名置換(生成成功後に削除 → 再作成)。
- §6.4: owner キー = demo_<id>(ctx.namespace と同一導出)固定。
- §8.2: LLM 生成はリース外、箱に書く区間だけ mutation リース。
- create_dataset(column_types=...) がプラン列型を DDL・値変換へ反映(既存投入経路の
  後方互換拡張)。
"""

import contextlib
import types
from datetime import date
from decimal import Decimal

import pytest

import jetuse_core.builder_data as bd
from jetuse_core import datasets, demo_lease

DEMO_ID = "11111111-2222-3333-4444-555555555555"
NS = f"demo_{DEMO_ID}"

TABLE = {
    "name": "equipment", "title": "設備台帳", "rows": 3,
    "columns": [
        {"name": "equipment_id", "type": "VARCHAR2(10 CHAR)", "description": "設備ID"},
        {"name": "failure_count", "type": "NUMBER(3,1)", "description": "故障回数"},
        {"name": "installed_on", "type": "DATE", "description": "設置日"},
    ],
}
DOC = {"filename": "manual.md", "title": "保全マニュアル", "outline": "安全注意 / 日常点検"}

PLAN = {
    "plan_version": 1, "title": "設備保全デモ", "description": "保全業務のデモ",
    "capabilities": ["dbchat", "rag.search"],
    "screens": [{"id": "home", "title": "ホーム",
                 "blocks": [{"type": "dbchat", "title": "照会"}]}],
    "data": {"tables": [TABLE], "documents": [DOC]},
}

GOOD_CSV = (
    "equipment_id,failure_count,installed_on\n"
    "P-101,2.5,2024-04-01\n"
    "P-102,0,2025-01-15\n"
    "C-201,5,2023-11-30\n"
)
GOOD_MD = "# 保全マニュアル\n\n## 安全注意\n作業前に電源を遮断する。\n"
USAGE = {"input_tokens": 10, "output_tokens": 20}


class FakeDatasets:
    """create/delete/list の呼び出しを記録する fake(既存 datasets 契約の形だけ再現)。"""

    def __init__(self):
        self.existing = []
        self.created = []
        self.deleted = []
        self.ready = True
        self.list_owners = []

    def list_datasets(self, owner):
        self.list_owners.append(owner)
        return self.existing

    def create_dataset(self, owner, display_name, data, model=None, warmup=True,
                       lease=None, column_types=None):
        self.created.append({"owner": owner, "display_name": display_name,
                             "data": data, "warmup": warmup, "lease": lease,
                             "column_types": column_types})
        return {"id": f"ds-{len(self.created)}", "table_name": "T", "ready": self.ready,
                "display_name": display_name, "columns": [], "row_count": 3}

    def delete_dataset(self, owner, ds_id, lease=None):
        self.deleted.append({"owner": owner, "ds_id": ds_id, "lease": lease})
        return True


class FakeRag:
    def __init__(self):
        self.existing = []
        self.added = []
        self.deleted = []
        self.statuses = {}  # id -> refresh のたびに消費する status 列(既定 completed)
        self.list_owners = []

    def list_files(self, owner):
        self.list_owners.append(owner)
        rows = [dict(r) for r in self.existing]
        rows += [{"id": a["id"], "filename": a["filename"], "status": "processing",
                  "oci_file_id": f"oci-{a['id']}"} for a in self.added]
        return rows

    def add_file(self, owner, filename, content, lease=None):
        fid = f"f-{len(self.added) + 1}"
        self.added.append({"id": fid, "owner": owner, "filename": filename,
                           "content": content, "lease": lease})
        return {"id": fid, "filename": filename, "status": "processing",
                "bytes": len(content)}

    def delete_file(self, owner, file_id):
        self.deleted.append({"owner": owner, "file_id": file_id})
        return True

    def refresh_statuses(self, owner, files):
        self.list_owners.append(owner)
        for f in files:
            seq = self.statuses.get(f["id"])
            f["status"] = seq.pop(0) if seq else "completed"
        return files


LEASE = object()  # 伝播の同一性のみ検証(実リースは E2E)


@pytest.fixture
def fakes(monkeypatch):
    ds, rg = FakeDatasets(), FakeRag()
    llm = {"outputs": [], "prompts": []}
    leases = []

    def _llm(prompt):
        llm["prompts"].append(prompt)
        return llm["outputs"].pop(0), dict(USAGE)

    @contextlib.contextmanager
    def mutation(demo_id):
        leases.append({"demo_id": demo_id,
                       "llm_calls_at_acquire": len(llm["prompts"])})
        yield LEASE

    monkeypatch.setattr(bd, "datasets", ds)
    monkeypatch.setattr(bd, "rag", rg)
    monkeypatch.setattr(bd, "_llm", _llm)
    monkeypatch.setattr(bd, "demo_lease", types.SimpleNamespace(mutation=mutation))
    monkeypatch.setattr(bd, "RAG_WAIT_INTERVAL_S", 0)
    return ds, rg, llm, leases


def provision(plan=PLAN):
    return bd.provision_data(DEMO_ID, plan)


# --- §6.1 表データ: 型・件数のサーバ側検証 + 有界再試行 → 失敗 ---


def test_success_first_try(fakes):
    ds, rg, llm, _ = fakes
    llm["outputs"] = [GOOD_CSV, GOOD_MD]
    out = provision()
    assert [c["display_name"] for c in ds.created] == ["equipment"]
    assert ds.created[0]["data"].decode() == GOOD_CSV.strip()  # fence 除去で末尾改行は落ちる
    assert ds.created[0]["lease"] is LEASE
    assert ds.created[0]["column_types"] == [
        "VARCHAR2(10 CHAR)", "NUMBER(3,1)", "DATE"]  # プラン列型を既存経路へ反映
    assert [a["filename"] for a in rg.added] == ["manual.md"]
    assert out["datasets"][0]["id"] == "ds-1"
    assert out["documents"][0]["id"] == "f-1"
    assert out["replaced"] == 0
    assert out["usage"] == {"input_tokens": 20, "output_tokens": 40}  # 全呼び出し合算


def test_type_mismatch_feeds_back_then_succeeds(fakes):
    ds, _, llm, _ = fakes
    bad = GOOD_CSV.replace("P-102,0,", "P-102,多数,")  # NUMBER 列に非数値
    llm["outputs"] = [bad, GOOD_CSV, GOOD_MD]
    provision()
    assert len(ds.created) == 1
    assert "failure_count" in llm["prompts"][1]  # 検証エラーがフィードバックされる


def test_type_mismatch_exhausts_retries_then_fails(fakes):
    ds, _, llm, _ = fakes
    bad = GOOD_CSV.replace("2024-04-01", "令和6年4月")  # DATE 列に非ISO
    llm["outputs"] = [bad] * bd.MAX_ATTEMPTS
    with pytest.raises(bd.DataProvisionError, match="installed_on") as ei:
        provision()
    assert len(llm["prompts"]) == bd.MAX_ATTEMPTS  # 有界(それ以上呼ばない)
    assert ds.created == []
    assert ei.value.usage == {"input_tokens": 30, "output_tokens": 60}  # 失敗分も合算


def test_number_nan_and_infinity_rejected(fakes):
    _, _, llm, _ = fakes
    llm["outputs"] = [GOOD_CSV.replace("P-102,0,", "P-102,NaN,")] * bd.MAX_ATTEMPTS
    with pytest.raises(bd.DataProvisionError, match="failure_count"):
        provision()


def test_number_precision_overflow_rejected(fakes):
    _, _, llm, _ = fakes
    # NUMBER(3,1): 整数部は 2 桁まで。100 は ORA-01438 相当 → 検証で落とす
    llm["outputs"] = [GOOD_CSV.replace("P-102,0,", "P-102,100,")] * bd.MAX_ATTEMPTS
    with pytest.raises(bd.DataProvisionError, match="NUMBER\\(3,1\\)"):
        provision()


def test_number_precision_checked_after_scale_rounding(fakes):
    """review-2 F004: NUMBER(3,1) の 99.99 は丸め後 100.0 = ORA-01438 → 検証で落とす。"""
    _, _, llm, _ = fakes
    llm["outputs"] = [GOOD_CSV.replace("P-102,0,", "P-102,99.99,")] * bd.MAX_ATTEMPTS
    with pytest.raises(bd.DataProvisionError, match="NUMBER\\(3,1\\)"):
        provision()


def test_number_scale_rounding_within_bounds_accepted(fakes):
    ds, _, llm, _ = fakes
    # 99.94 は丸め後 99.9 で NUMBER(3,1) に収まる(過剰拒否しない)
    llm["outputs"] = [GOOD_CSV.replace("P-102,0,", "P-102,99.94,"), GOOD_MD]
    provision()
    assert len(ds.created) == 1


def test_integer_only_precision_rounding():
    """NUMBER(3) の 999.9 は丸め後 1000 → 不合格(review-2 F004 の境界)。"""
    assert bd._check_value("999.9", "NUMBER(3)") is not None
    assert bd._check_value("999.4", "NUMBER(3)") is None  # 丸め後 999 は合格
    assert bd._check_value("0.0005", "NUMBER(2,5)") is None  # s > p も Oracle 準拠で判定


def test_row_count_mismatch_retries(fakes):
    _, _, llm, _ = fakes
    short = "\n".join(GOOD_CSV.splitlines()[:3]) + "\n"  # 2行(期待3行)
    llm["outputs"] = [short] * bd.MAX_ATTEMPTS
    with pytest.raises(bd.DataProvisionError, match="行数"):
        provision()


def test_header_mismatch_retries(fakes):
    _, _, llm, _ = fakes
    renamed = GOOD_CSV.replace("equipment_id", "machine_id")
    llm["outputs"] = [renamed] * bd.MAX_ATTEMPTS
    with pytest.raises(bd.DataProvisionError, match="ヘッダ"):
        provision()


def test_varchar_length_overflow_retries(fakes):
    _, _, llm, _ = fakes
    long = GOOD_CSV.replace("P-101", "P" * 11)  # VARCHAR2(10 CHAR) 超過
    llm["outputs"] = [long] * bd.MAX_ATTEMPTS
    with pytest.raises(bd.DataProvisionError, match="equipment_id"):
        provision()


def test_csv_parse_error_is_bounded_retry(fakes):
    _, _, llm, _ = fakes
    huge_field = '"' + "x" * 200_000 + '",1,2024-01-01\n'  # csv.field_size_limit 超過
    llm["outputs"] = [GOOD_CSV.splitlines()[0] + "\n" + huge_field] * bd.MAX_ATTEMPTS
    with pytest.raises(bd.DataProvisionError, match="CSV 解析エラー"):
        provision()


def test_csv_code_fence_is_stripped(fakes):
    ds, _, llm, _ = fakes
    llm["outputs"] = [f"```csv\n{GOOD_CSV}```", GOOD_MD]
    provision()
    assert ds.created[0]["data"].decode().startswith("equipment_id,")


# --- §6.2 文書: Markdown 生成 → 既存 upload 関数。≤64KB・索引完了待ち ---


def test_document_over_64kb_retries_then_fails(fakes):
    _, rg, llm, _ = fakes
    llm["outputs"] = [GOOD_CSV] + ["あ" * (bd.DOC_MAX_BYTES // 3 + 1)] * bd.MAX_ATTEMPTS
    with pytest.raises(bd.DataProvisionError, match="manual.md"):
        provision()
    assert rg.added == []


def test_empty_document_retries_then_succeeds(fakes):
    _, rg, llm, _ = fakes
    llm["outputs"] = [GOOD_CSV, "", GOOD_MD]
    provision()
    assert rg.added[0]["content"].decode() == GOOD_MD.strip()
    assert rg.added[0]["lease"] is LEASE


def test_rag_indexing_waited_until_completed(fakes):
    _, rg, llm, _ = fakes
    llm["outputs"] = [GOOD_CSV, GOOD_MD]
    rg.statuses = {"f-1": ["processing", "completed"]}
    provision()  # processing → completed を待って正常終了
    assert rg.statuses["f-1"] == []


def test_rag_indexing_failure_raises(fakes):
    _, rg, llm, _ = fakes
    llm["outputs"] = [GOOD_CSV, GOOD_MD]
    rg.statuses = {"f-1": ["failed"]}
    with pytest.raises(bd.DataProvisionError, match="索引化に失敗"):
        provision()


def test_rag_indexing_timeout_raises(fakes, monkeypatch):
    _, rg, llm, _ = fakes
    monkeypatch.setattr(bd, "RAG_WAIT_TIMEOUT_S", 0)
    llm["outputs"] = [GOOD_CSV, GOOD_MD]
    rg.statuses = {"f-1": ["processing"] * 100}
    with pytest.raises(bd.DataProvisionError, match="完了しません"):
        provision()


def test_dbchat_warmup_not_ready_raises(fakes):
    ds, _, llm, _ = fakes
    ds.ready = False
    llm["outputs"] = [GOOD_CSV, GOOD_MD]
    with pytest.raises(bd.DataProvisionError, match="dbchat"):
        provision()


# --- §6.3 冪等置換: 同名は「生成成功後に 外部先行削除 → 再作成」 ---


def test_same_name_dataset_replaced(fakes):
    ds, _, llm, _ = fakes
    ds.existing = [
        {"id": "old-1", "display_name": "equipment", "table_name": "T1",
         "columns": [], "row_count": 1},
        {"id": "old-2", "display_name": "other", "table_name": "T2",
         "columns": [], "row_count": 1},
    ]
    llm["outputs"] = [GOOD_CSV, GOOD_MD]
    out = provision()
    assert [d["ds_id"] for d in ds.deleted] == ["old-1"]  # 同名のみ削除(other は残す)
    assert ds.deleted[0]["lease"] is LEASE
    assert len(ds.created) == 1
    assert out["replaced"] == 1


def test_same_filename_document_replaced(fakes):
    _, rg, llm, _ = fakes
    rg.existing = [
        {"id": "rf-1", "filename": "manual.md", "status": "completed",
         "oci_file_id": "oci-rf-1"},
        {"id": "rf-2", "filename": "keep.md", "status": "completed",
         "oci_file_id": "oci-rf-2"},
    ]
    llm["outputs"] = [GOOD_CSV, GOOD_MD]
    out = provision()
    assert [d["file_id"] for d in rg.deleted] == ["rf-1"]
    assert len(rg.added) == 1
    assert out["replaced"] == 1


# --- F002: 失敗は DataProvisionError(usage 込み)へ正規化。制御例外は型を保つ ---


def test_external_failure_normalized_with_usage(fakes):
    """rag.add_file の外部失敗も DataProvisionError(usage 込み)になる(review-2 F002)。"""
    _, rg, llm, _ = fakes
    llm["outputs"] = [GOOD_CSV, GOOD_MD]

    def boom(owner, filename, content, lease=None):
        raise RuntimeError("store not visible")

    rg.add_file = boom
    with pytest.raises(bd.DataProvisionError, match="store not visible") as ei:
        provision()
    assert ei.value.usage == {"input_tokens": 20, "output_tokens": 40}  # 生成2回分
    assert isinstance(ei.value.__cause__, RuntimeError)


def test_llm_comm_failure_normalized_with_usage(fakes, monkeypatch):
    llm_calls = []

    def flaky(prompt):
        llm_calls.append(prompt)
        if len(llm_calls) == 2:
            raise ConnectionError("upstream down")
        return GOOD_CSV, dict(USAGE)

    monkeypatch.setattr(bd, "_llm", flaky)
    with pytest.raises(bd.DataProvisionError, match="upstream down") as ei:
        provision()
    assert ei.value.usage == {"input_tokens": 10, "output_tokens": 20}  # 成功1回分


def test_demo_gone_control_exception_passes_through(fakes, monkeypatch):
    """DemoGoneError は型を保って伝播(「demo が消えた」と「生成失敗」を区別 — F002)。"""
    _, _, llm, _ = fakes

    @contextlib.contextmanager
    def gone(demo_id):
        raise demo_lease.DemoGoneError(demo_id)
        yield  # contextmanager の形を保つ(到達しない)

    monkeypatch.setattr(bd, "demo_lease",
                        types.SimpleNamespace(mutation=gone))
    llm["outputs"] = [GOOD_CSV, GOOD_MD]
    with pytest.raises(demo_lease.DemoGoneError):
        provision()


def test_generation_failure_leaves_existing_dataset(fakes):
    """生成が検証を通らない限り既存の同名 dataset を消さない(置換は生成成功後)。"""
    ds, _, llm, _ = fakes
    ds.existing = [{"id": "old-1", "display_name": "equipment", "table_name": "T1",
                    "columns": [], "row_count": 1}]
    llm["outputs"] = ["broken"] * bd.MAX_ATTEMPTS
    with pytest.raises(bd.DataProvisionError):
        provision()
    assert ds.deleted == []


# --- §6.4 隔離 / §8.2 リース ---


def test_owner_key_fixed_to_namespace(fakes):
    ds, rg, llm, _ = fakes
    ds.existing = [{"id": "old-1", "display_name": "equipment", "table_name": "T1",
                    "columns": [], "row_count": 1}]
    rg.existing = [{"id": "rf-1", "filename": "manual.md", "status": "completed",
                    "oci_file_id": "oci-rf-1"}]
    llm["outputs"] = [GOOD_CSV, GOOD_MD]
    provision()
    owners = (
        ds.list_owners + rg.list_owners
        + [c["owner"] for c in ds.created] + [d["owner"] for d in ds.deleted]
        + [a["owner"] for a in rg.added] + [d["owner"] for d in rg.deleted]
    )
    assert owners and set(owners) == {NS}


def test_llm_generation_happens_outside_lease(fakes):
    """§8.2: LLM 呼び出しはリースを跨がない(全生成がリース取得前に完了している)。"""
    _, _, llm, leases = fakes
    llm["outputs"] = [GOOD_CSV, GOOD_MD]
    provision()
    assert [lo["demo_id"] for lo in leases] == [DEMO_ID]
    assert leases[0]["llm_calls_at_acquire"] == 2  # 表1+文書1 の生成が取得前に済んでいる


def test_tables_only_plan_skips_rag(fakes):
    ds, rg, llm, _ = fakes
    plan = {**PLAN, "capabilities": ["dbchat"],
            "data": {"tables": [TABLE], "documents": []}}
    llm["outputs"] = [GOOD_CSV]
    out = provision(plan)
    assert len(ds.created) == 1 and rg.added == []
    assert out["documents"] == []


def test_warmup_only_on_last_table(fakes):
    ds, _, llm, _ = fakes
    t2 = {**TABLE, "name": "sensor", "title": "センサ"}
    plan = {**PLAN, "capabilities": ["dbchat"],
            "data": {"tables": [TABLE, t2], "documents": []}}
    llm["outputs"] = [GOOD_CSV, GOOD_CSV]
    provision(plan)
    assert [c["warmup"] for c in ds.created] == [False, True]


# --- create_dataset(column_types=...): プラン列型の物理反映(既存投入経路の拡張) ---


class _RecCur:
    def __init__(self):
        self.calls = []
        self.rows = None

    def execute(self, sql, **kw):
        self.calls.append(sql)

    def executemany(self, sql, seq):
        self.calls.append(sql)
        self.rows = seq

    def fetchone(self):
        return (0,)

    def fetchall(self):
        return []


class _RecConn:
    def __init__(self, cur):
        self._cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self._cur

    def commit(self):
        pass


@pytest.fixture
def dataset_cur(monkeypatch):
    cur = _RecCur()
    monkeypatch.setattr(datasets, "connect", lambda: _RecConn(cur))
    monkeypatch.setattr(datasets.vpd, "integrity_gate", lambda: None)
    monkeypatch.setattr(datasets, "owner_key_gate", lambda: None)
    monkeypatch.setattr(datasets, "require_lease_for", lambda owner, lease: None)
    monkeypatch.setattr(datasets, "_ensure_meta", lambda cur: None)
    monkeypatch.setattr(datasets, "reconcile_creating",
                        lambda owner, cur=None: 0)
    monkeypatch.setattr(datasets, "_rebuild_profile",
                        lambda owner, cur, model=None: [])
    return cur


def test_create_dataset_applies_explicit_column_types(dataset_cur):
    out = datasets.create_dataset(
        "dev-user", "equipment", GOOD_CSV.encode(),
        column_types=["VARCHAR2(10 CHAR)", "NUMBER(3,1)", "DATE"], warmup=False)
    ddl = next(s for s in dataset_cur.calls if s.startswith("CREATE TABLE"))
    assert '"EQUIPMENT_ID" VARCHAR2(10 CHAR)' in ddl
    assert '"FAILURE_COUNT" NUMBER(3,1)' in ddl
    assert '"INSTALLED_ON" DATE' in ddl
    assert dataset_cur.rows[0] == ["P-101", 2.5, date(2024, 4, 1)]  # 型どおりの値変換
    assert out["row_count"] == 3


def test_create_dataset_rejects_bad_column_types(dataset_cur):
    with pytest.raises(ValueError, match="許可されていない列型"):
        datasets.create_dataset(
            "dev-user", "x", GOOD_CSV.encode(),
            column_types=["VARCHAR2(10 CHAR)", "NUMBER; DROP TABLE t", "DATE"])
    with pytest.raises(ValueError, match="一致しません"):
        datasets.create_dataset(
            "dev-user", "x", GOOD_CSV.encode(), column_types=["DATE"])
    assert not any(s.startswith("CREATE TABLE") for s in dataset_cur.calls)


@pytest.mark.parametrize("bad", [
    "VARCHAR2(9999 CHAR)", "VARCHAR2(0 CHAR)", "NUMBER(99)", "NUMBER(0)",
    "NUMBER(10,99)", "DATE\n",
])
def test_create_dataset_rejects_out_of_bounds_types(dataset_cur, bad):
    """review-2 F005: 上限は builder_design のプラン検証と同一(単一の正を共有)。"""
    with pytest.raises(ValueError, match="許可され"):
        datasets.create_dataset(
            "dev-user", "x", b"a,b,c\n1,2,3\n",
            column_types=["DATE", bad, "NUMBER"])


def test_explicit_number_binds_decimal_exactly(dataset_cur):
    """review-2 F003: 明示 NUMBER は Decimal のまま bind(2^53 超でも桁落ちしない)。"""
    big = "9007199254740993"  # float に通すと 9007199254740992 に落ちる値
    csv_text = f"id,val\nA,{big}\n"
    datasets.create_dataset(
        "dev-user", "nums", csv_text.encode(),
        column_types=["VARCHAR2(10 CHAR)", "NUMBER(38)"], warmup=False)
    assert dataset_cur.rows[0] == ["A", Decimal(big)]
    assert str(dataset_cur.rows[0][1]) == big


def test_explicit_number_rejects_nan_infinity_values(dataset_cur):
    for v in ("NaN", "Infinity"):
        with pytest.raises(ValueError, match="数値でない"):
            datasets.create_dataset(
                "dev-user", "nums", f"id,val\nA,{v}\n".encode(),
                column_types=["VARCHAR2(10 CHAR)", "NUMBER"], warmup=False)
