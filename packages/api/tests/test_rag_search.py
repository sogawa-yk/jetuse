"""BE-04: テナント RAG ストア登録簿の解決と file_search 委譲の単体テスト。

- get_tenant_store_id / register_tenant_store の SQL 解決(実 DB はモックした接続で検証)。
- search() の例外正規化(クライアント生成失敗・抽出失敗も RagSearchError へ)と空ストア時の挙動。
"""

from types import SimpleNamespace

import pytest

from jetuse_core import rag

# テスト用 Generative AI Project OCID(ルートの OCID 検証と整合する形式)。
PROJ_A = "ocid1.generativeaiproject.oc1.ap-osaka-1.aaaaaaaaprojecta"
PROJ_B = "ocid1.generativeaiproject.oc1.ap-osaka-1.bbbbbbbbprojectb"
PROJ_T = "ocid1.generativeaiproject.oc1.ap-osaka-1.tttttttttproj"
PROJ_TENANT = "ocid1.generativeaiproject.oc1.ap-osaka-1.tenanttttproj"


class _FakeCursor:
    def __init__(self, store):
        self.store = store
        self._row = None
        self.executed = []

    def execute(self, sql, **kw):
        self.executed.append((sql, kw))
        s = " ".join(sql.split())
        if s.startswith("SELECT vector_store_id FROM platform_rag_stores"):
            vs = self.store.get(kw["t"])
            self._row = (vs,) if vs else None
        elif s.startswith("MERGE INTO platform_rag_stores"):
            self.store[kw["t"]] = kw["v"]

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, store):
        self.store = store
        self.committed = False

    def cursor(self):
        return _FakeCursor(self.store)

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def fake_db(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr(rag, "connect", lambda: _FakeConn(store))
    return store


def test_get_tenant_store_id_resolves_and_misses(fake_db):
    assert rag.get_tenant_store_id(PROJ_A) is None
    fake_db[PROJ_A] = "vs_kix_A"
    assert rag.get_tenant_store_id(PROJ_A) == "vs_kix_A"
    # 別テナントは別ストア(取り違えない)。
    assert rag.get_tenant_store_id(PROJ_B) is None


def test_register_tenant_store_upserts(fake_db):
    # verify=False で OCI 検証を飛ばし SQL upsert だけを確認(検証は別テストで)。
    rag.register_tenant_store(PROJ_A, "vs_kix_A", verify=False)
    assert fake_db[PROJ_A] == "vs_kix_A"
    rag.register_tenant_store(PROJ_A, "vs_kix_A2", verify=False)  # 再登録=更新
    assert fake_db[PROJ_A] == "vs_kix_A2"


class _IntegrityConn:
    """MERGE で ORA-00001 を起こし、SELECT(再読込)では `resolved` を返す fake 接続。

    BE04-010: 一意制約違反後に register が登録簿を再読込して冪等性を判定する経路を検証する。
    """

    def __init__(self, resolved):
        self.resolved = resolved

    def cursor(self):
        import oracledb

        class _Err:
            code = 1  # ORA-00001

        outer = self

        class _Cur:
            def execute(self, sql, **kw):
                s = " ".join(sql.split())
                if s.startswith("MERGE INTO platform_rag_stores"):
                    raise oracledb.IntegrityError(_Err())
                self._row = (outer.resolved,) if outer.resolved else None

            def fetchone(self):
                return self._row

        return _Cur()

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_register_tenant_store_conflict_maps_to_store_conflict(monkeypatch):
    # ORA-00001 後の再読込が「このテナントは未保有」→ 別テナントが保有 → StoreConflictError。
    monkeypatch.setattr(rag, "connect", lambda: _IntegrityConn(resolved=None))
    with pytest.raises(rag.StoreConflictError):
        rag.register_tenant_store(PROJ_A, "vs_taken", verify=False)


def test_register_tenant_store_concurrent_same_store_is_idempotent(monkeypatch):
    # ORA-00001 後の再読込が「同一テナント=同一ストア」→ 冪等成功(例外なし)。BE04-010。
    monkeypatch.setattr(rag, "connect", lambda: _IntegrityConn(resolved="vs_same"))
    rag.register_tenant_store(PROJ_A, "vs_same", verify=False)  # 例外なし = 冪等成功


def test_register_tenant_store_verifies_before_write(fake_db, monkeypatch):
    # 既定(verify=True)は DB 書込の前にストア実在を検証し、失敗時は DB を更新しない。
    class _Client:
        class vector_stores:
            @staticmethod
            def retrieve(vector_store_id):
                raise _not_found()

    monkeypatch.setattr(rag, "make_cp_client", lambda: _Client())
    with pytest.raises(rag.StoreVerificationError):
        rag.register_tenant_store(PROJ_A, "vs_foreign")
    assert PROJ_A not in fake_db  # 検証失敗で DB は無変更


def test_verify_tenant_store_access_ok(monkeypatch):
    # retrieve は CP(本体 CRUD)エンドポイントのみが提供する(推論側は 404)。make_cp_client を使う。
    captured = {}

    class _Client:
        class vector_stores:
            @staticmethod
            def retrieve(vector_store_id):
                captured["id"] = vector_store_id
                return SimpleNamespace(id=vector_store_id, status="completed")

    monkeypatch.setattr(rag, "make_cp_client", lambda: _Client())
    rag.verify_tenant_store_access(PROJ_T, "vs_ok")
    assert captured["id"] == "vs_ok"


def _not_found():
    # openai.NotFoundError(404) を最小構成で生成(BE04-008 の 404→400 分類テスト用)。
    import httpx
    import openai
    resp = httpx.Response(404, request=httpx.Request("GET", "http://x"))
    return openai.NotFoundError("not found", response=resp, body=None)


def test_verify_tenant_store_access_not_found_is_verification_error(monkeypatch):
    # 404(不存在)= 利用者の入力不正 → StoreVerificationError(ルートは 400。BE04-008)。
    class _Client:
        class vector_stores:
            @staticmethod
            def retrieve(vector_store_id):
                raise _not_found()

    monkeypatch.setattr(rag, "make_cp_client", lambda: _Client())
    with pytest.raises(rag.StoreVerificationError):
        rag.verify_tenant_store_access(PROJ_T, "vs_bogus")


def test_verify_tenant_store_access_upstream_error_is_upstream(monkeypatch):
    # 接続/タイムアウト/認証/5xx 等の一過性障害 → StoreUpstreamError(ルートは 502。BE04-008)。
    class _Client:
        class vector_stores:
            @staticmethod
            def retrieve(vector_store_id):
                raise RuntimeError("connection reset")

    monkeypatch.setattr(rag, "make_cp_client", lambda: _Client())
    with pytest.raises(rag.StoreUpstreamError):
        rag.verify_tenant_store_access(PROJ_T, "vs_x")


def test_verify_tenant_store_access_client_init_failure_is_upstream(monkeypatch):
    # クライアント生成自体の失敗も一過性の上流障害として 502 系へ(400 に倒さない)。
    def boom():
        raise RuntimeError("signer/config init failed")

    monkeypatch.setattr(rag, "make_cp_client", boom)
    with pytest.raises(rag.StoreUpstreamError):
        rag.verify_tenant_store_access(PROJ_T, "vs_x")


@pytest.mark.parametrize("bad_id", [None, "", "other-store"])
def test_verify_tenant_store_access_bad_id_is_verification_error(monkeypatch, bad_id):
    # retrieve 応答の id が欠落/空/不一致なら fail-closed(実在確認できない → 拒否。BE04-009)。
    class _Client:
        class vector_stores:
            @staticmethod
            def retrieve(vector_store_id):
                return SimpleNamespace(id=bad_id)

    monkeypatch.setattr(rag, "make_cp_client", lambda: _Client())
    with pytest.raises(rag.StoreVerificationError):
        rag.verify_tenant_store_access(PROJ_T, "vs_req")


def test_search_empty_store_returns_empty(monkeypatch):
    monkeypatch.setattr(rag, "get_tenant_store_id", lambda t: None)
    # ストア未保有: 委譲せず空ヒット(store_present=False)。
    monkeypatch.setattr(
        rag, "make_inference_client", lambda **kw: pytest.fail("must not delegate")
    )
    out = rag.search(PROJ_A, "q")
    assert out == {"hits": [], "citations": [], "answer": "", "store_present": False}


def test_search_client_init_failure_is_normalized(monkeypatch):
    # クライアント生成失敗も RagSearchError へ正規化(500 にしない / 502 写像の前提)。
    monkeypatch.setattr(rag, "get_tenant_store_id", lambda t: "vs_kix_A")

    def boom(**kw):
        raise RuntimeError("signer/config init failed")

    monkeypatch.setattr(rag, "make_inference_client", boom)
    with pytest.raises(rag.RagSearchError):
        rag.search(PROJ_A, "q")


def test_extract_results_valid_empty_returns_empty():
    # 正常な空検索は file_search_call.results==[] のみ受理(検索は実行されたが 0 件)。
    hits, cites, ans = rag._extract_search_results(_fs([]))
    assert (hits, cites, ans) == ([], [], "")


def test_extract_results_no_file_search_call_raises():
    # tool_choice=required なのに file_search_call が一切無い(output=[] / message のみ)は
    # 「検索未実行/壊れた応答」→ 空に倒さず fail-closed(BE04-R5-005)。
    with pytest.raises(rag.ResponseShapeError):
        rag._extract_search_results(SimpleNamespace(output=[]))
    with pytest.raises(rag.ResponseShapeError):
        rag._extract_search_results(_msg([SimpleNamespace(text="a", annotations=[])]))


def test_extract_results_normal_shape():
    resp = SimpleNamespace(
        output=[
            SimpleNamespace(
                type="file_search_call",
                results=[SimpleNamespace(file_id="f1", filename="a.md", score=0.5, text="x")],
            ),
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        text="ans",
                        annotations=[SimpleNamespace(file_id="f1", filename="a.md")],
                    )
                ],
            ),
        ]
    )
    hits, cites, ans = rag._extract_search_results(resp)
    assert hits[0]["file_id"] == "f1" and ans == "ans" and cites[0]["file_id"] == "f1"


@pytest.mark.parametrize(
    "bad_output",
    [None, "a-string", {"k": "v"}, 123],  # 欠落/文字列/dict/数値 = 想定外スキーマ
)
def test_extract_results_malformed_output_raises(bad_output):
    # output が list でない = 上流スキーマ変更/壊れた応答 → 空に倒さず例外(fail-closed)。
    with pytest.raises(rag.ResponseShapeError):
        rag._extract_search_results(SimpleNamespace(output=bad_output))


def test_extract_results_nonstructured_item_raises():
    # output は list だが item が非構造(文字列) → 例外。
    with pytest.raises(rag.ResponseShapeError):
        rag._extract_search_results(SimpleNamespace(output=["not-an-item"]))


def _fs(results):
    return SimpleNamespace(output=[SimpleNamespace(type="file_search_call", results=results)])


def _msg(content):
    return SimpleNamespace(output=[SimpleNamespace(type="message", content=content)])


@pytest.mark.parametrize(
    "resp",
    [
        _fs("not-a-list"),  # results が list でない
        _fs(None),  # results 欠落(None)は空に倒さず破損扱い(BE04-R5-005)
        _fs([SimpleNamespace(file_id="", score=0.5, text="x")]),  # 空 file_id(偽ヒット)
        _fs([SimpleNamespace(file_id="f1", score="hi", text="x")]),  # 非数値 score
        _fs([SimpleNamespace(file_id="f1", score=float("nan"), text="x")]),  # NaN score
        _fs([SimpleNamespace(file_id="f1", score=float("inf"), text="x")]),  # Inf score
        _fs([SimpleNamespace(file_id="f1", score=True, text="x")]),  # bool は数値扱いしない
        _fs([SimpleNamespace(file_id=None, score=0.5)]),  # file_id 欠落
        _msg("not-a-list"),  # message.content が list でない
        _msg([SimpleNamespace(text="a", annotations="not-a-list")]),  # annotations が list でない
    ],
)
def test_extract_results_malformed_inner_raises(resp):
    # 内側コンテナ/要素の不正形状も空に倒さず ResponseShapeError(fail-closed)。
    with pytest.raises(rag.ResponseShapeError):
        rag._extract_search_results(resp)


def _fs_msg(results, content):
    return SimpleNamespace(output=[
        SimpleNamespace(type="file_search_call", results=results),
        SimpleNamespace(type="message", content=content),
    ])


def test_extract_results_hits_without_answer_raises():
    # ヒットがあるのに回答が空 → 根拠付き回答契約の違反(BE04-002)。
    resp = _fs_msg(
        [SimpleNamespace(file_id="f1", filename="a", score=0.5, text="x")],
        [SimpleNamespace(text="", annotations=[SimpleNamespace(file_id="f1", filename="a")])],
    )
    with pytest.raises(rag.ResponseShapeError):
        rag._extract_search_results(resp)


def test_extract_results_hits_without_citation_raises():
    # ヒット＋回答はあるが引用が無い → 契約違反(BE04-002)。
    resp = _fs_msg(
        [SimpleNamespace(file_id="f1", filename="a", score=0.5, text="x")],
        [SimpleNamespace(text="ans", annotations=[])],
    )
    with pytest.raises(rag.ResponseShapeError):
        rag._extract_search_results(resp)


def test_extract_results_citation_not_in_hits_raises():
    # ヒットに無い file_id を引用 → 上流破損(BE04-002)。
    resp = _fs_msg(
        [SimpleNamespace(file_id="f1", filename="a", score=0.5, text="x")],
        [SimpleNamespace(text="ans", annotations=[SimpleNamespace(file_id="fX", filename="b")])],
    )
    with pytest.raises(rag.ResponseShapeError):
        rag._extract_search_results(resp)


def test_extract_results_dedup_uses_unrounded_max_score():
    # 同一 file_id の重複は**未丸めスコア**で最大を採る(丸め混在の取り違えを防ぐ。BE04-006)。
    # 0.5004(最大)→0.5001 の順。旧実装は丸め済み 0.5 と 0.5001 を比較し誤って後者で上書きした。
    resp = _fs_msg(
        [
            SimpleNamespace(file_id="f1", filename="a", score=0.5004, text="FIRST"),
            SimpleNamespace(file_id="f1", filename="a", score=0.5001, text="SECOND"),
        ],
        [SimpleNamespace(text="ans", annotations=[SimpleNamespace(file_id="f1", filename="a")])],
    )
    hits, cites, ans = rag._extract_search_results(resp)
    assert len(hits) == 1
    assert hits[0]["text"] == "FIRST"  # 未丸め最大(0.5004)が残る
    assert hits[0]["score"] == 0.5     # 応答は末尾で 3 桁丸め


def test_search_malformed_response_normalized_to_error(monkeypatch):
    # 実パーサー経由で壊れた上流応答が RagSearchError へ正規化されること(パーサーは patch しない)。
    monkeypatch.setattr(rag, "get_tenant_store_id", lambda t: "vs_kix_A")

    class _Client:
        class responses:
            @staticmethod
            def create(**kw):
                return SimpleNamespace(output="malformed-not-a-list")

    monkeypatch.setattr(rag, "make_inference_client", lambda **kw: _Client())
    with pytest.raises(rag.RagSearchError):
        rag.search(PROJ_A, "q")


def test_search_happy_resolves_via_tenant_registry(monkeypatch):
    # tenant 登録簿で解決した store id だけが file_search に渡る(呼び出し元は渡さない)。
    seen = {}
    client_kw = {}
    monkeypatch.setattr(rag, "get_tenant_store_id", lambda t: "vs_kix_TENANT")
    monkeypatch.setattr(rag, "resolve_citation_filenames", lambda o, c: c)

    class _Client:
        class responses:
            @staticmethod
            def create(**kw):
                seen.update(kw)
                return SimpleNamespace(output=[])

    def _mk(**kw):
        client_kw.update(kw)
        return _Client()

    monkeypatch.setattr(rag, "make_inference_client", _mk)
    monkeypatch.setattr(rag, "_extract_search_results", lambda r: ([{"file_id": "f1"}], [], "ans"))
    out = rag.search(PROJ_TENANT, "請求書", top_k=3)
    assert out["store_present"] is True
    assert out["hits"] == [{"file_id": "f1"}]
    # file_search に渡るストアは登録簿解決のものだけ。
    assert seen["tools"][0]["vector_store_ids"] == ["vs_kix_TENANT"]
    assert seen["tools"][0]["max_num_results"] == 3
    assert seen["tool_choice"] == "required"
    # OpenAi-Project は tenant(=Project OCID)に固定(Project 単位分離。BE04-007)。
    assert client_kw["project_ocid"] == PROJ_TENANT
    assert client_kw["with_project"] is True
