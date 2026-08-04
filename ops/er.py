#!/usr/bin/env python3
"""改善要望（ER）を一覧化する。

**ER の目的は「後で実装する項目を、思いついたその場で置いておく」こと。**
いま進めている実装に集中できるように、この一覧は**急かさない**。
既定の状態は `parked`（積んである）で、判断を求める行列ではない。

2 つの出口がある:

- `index`  … `docs/enhance/README.md` の一覧を更新する（**実装者が読む**。リポジトリ内）
- `report` … 概要の HTML を出す（**判断する人が読む**。Obsidian へ置く）

同じ元データ（`docs/enhance/ER-*.md`）から両方を作るので、**ずれない**。
手で一覧を書き換えると必ずずれるので、`index` は自動生成にしてある。

使い方:
    python3 ops/er.py index
    python3 ops/er.py report --out /path/to/ER一覧.html
    python3 ops/er.py list
"""

from __future__ import annotations

import argparse
import datetime
import html
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DIR = ROOT / "docs" / "enhance"

# 表示順。**「積んである」を上に**する（次に何を拾うかを選ぶための一覧なので）
STATUS_ORDER = ["parked", "doing", "done", "dropped"]
STATUS_JA = {"parked": "積んである", "doing": "着手中", "done": "完了", "dropped": "見送り"}
SOURCE_JA = {"実害": "実害あり", "気づき": "気づき", "構想": "構想"}
SIZE_JA = {"S": "小", "M": "中", "L": "大"}


def _front_matter(text: str) -> dict:
    """先頭の `---` ブロックを読む。依存を増やさないため簡易に解く。"""
    m = re.match(r"---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.split("#", 1)[0].rstrip() if not line.strip().startswith("#") else ""
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip()
    return out


def _summary(text: str) -> str:
    """「## ひとことで」の本文を1行で返す。"""
    m = re.search(r"##\s*ひとことで\s*\n+(.+?)(?=\n#|\Z)", text, re.S)
    if not m:
        return ""
    body = " ".join(x.strip() for x in m.group(1).strip().splitlines() if x.strip())
    return re.sub(r"（[^）]*）", "", body).strip()


def load() -> list[dict]:
    items = []
    for p in sorted(DIR.glob("ER-*.md")):
        text = p.read_text(encoding="utf-8")
        fm = _front_matter(text)
        if not fm.get("id"):
            continue
        fm["_path"] = p.relative_to(ROOT).as_posix()
        fm["_summary"] = _summary(text)
        items.append(fm)
    items.sort(key=lambda x: (STATUS_ORDER.index(x.get("status", "parked"))
                              if x.get("status") in STATUS_ORDER else 9,
                              x.get("source") != "実害",   # 実害を先に
                              x.get("id", "")))
    return items


def cmd_list(items: list[dict]) -> None:
    for i in items:
        print(f"{i['id']}  {STATUS_JA.get(i.get('status',''), '?'):<6} "
              f"{SOURCE_JA.get(i.get('source',''), ''):<8} {i.get('title','')}")


def cmd_index(items: list[dict]) -> None:
    rows = ["| ID | 状態 | 種別 | 大きさ | 内容 |", "|---|---|---|---|---|"]
    for i in items:
        link = f"[{i['id']}]({pathlib.Path(i['_path']).name})"
        rows.append(f"| {link} | {STATUS_JA.get(i.get('status',''),'?')} "
                    f"| {SOURCE_JA.get(i.get('source',''),'')} "
                    f"| {SIZE_JA.get(i.get('size',''), i.get('size',''))} "
                    f"| {i.get('title','')} |")
    readme = DIR / "README.md"
    text = readme.read_text(encoding="utf-8")
    block = "<!-- BEGIN INDEX -->\n" + "\n".join(rows) + "\n<!-- END INDEX -->"
    text = re.sub(r"<!-- BEGIN INDEX -->.*?<!-- END INDEX -->", block, text, flags=re.S)
    readme.write_text(text, encoding="utf-8")
    print(f"{readme.relative_to(ROOT)} を更新（{len(items)} 件）")


CSS = """
:root{--bg:#f8fafc;--fg:#0f172a;--mut:#64748b;--bd:#e2e8f0;--card:#fff;
      --ok:#059669;--ng:#dc2626;--warn:#b45309;--acc:#4f46e5}
@media(prefers-color-scheme:dark){:root{--bg:#0b1220;--fg:#e2e8f0;--mut:#94a3b8;
      --bd:#1e293b;--card:#111a2e}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:15px/1.7 system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:28px 22px 60px}
h1{font-size:24px;margin:0 0 6px}
.sub{color:var(--mut);margin:0 0 24px;font-size:14px}
.note{background:var(--card);border:1px solid var(--bd);border-left:4px solid var(--acc);
      border-radius:10px;padding:14px 18px;margin:0 0 26px}
.note p{margin:6px 0}
h2{font-size:16px;margin:30px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--bd)}
.er{background:var(--card);border:1px solid var(--bd);border-radius:11px;
    padding:15px 18px;margin:0 0 11px}
.er .hd{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.er .id{font:600 12px ui-monospace,monospace;color:var(--mut)}
.er .ti{font-weight:600;font-size:15px}
.er .sm{color:var(--mut);font-size:13.5px;margin-top:6px}
.tag{font-size:11px;padding:2px 9px;border-radius:99px;border:1px solid var(--bd);color:var(--mut)}
.tag.harm{color:var(--ng);border-color:var(--ng)}
.tag.size{color:var(--warn);border-color:var(--warn)}
.empty{color:var(--mut);font-size:14px;padding:10px 0}
.sm-lead{color:var(--ng);font-size:13.5px;margin:0 0 10px}
.foot{margin-top:36px;padding-top:14px;border-top:1px solid var(--bd);
      color:var(--mut);font-size:12.5px}
"""


STALL_JA = {
    "review": ("レビューが終わっていない", "**放っておくと欠陥が本番に残る。**"),
    "unshipped": ("PR が出ていない", "レビューは通っているが出荷されていない。"),
    "unknown": ("状態が読めない", "確認が要る。"),
    "pr": ("PR が出ている", "マージの判断待ち。"),
    "parked": ("意図的に止めている", "理由は ER にある。"),
}


def _stalled_section() -> str:
    """止まっている作業。**ER と同じ1ページに出す**（見る場所を1つにするため）。

    実害: 2026-08-04 に、レビュー未完了のまま 6 日放置されていた作業から
    **マージ済みコードの blocker が 2 件**見つかった（他人の資産を消しうるもの）。
    「やりかけ」も取りこぼさないようにする。
    """
    try:
        sys.path.insert(0, str(ROOT / "ops"))
        import stalled  # noqa: PLC0415
        items = stalled.scan()
    except Exception as e:  # noqa: BLE001 - レポート生成を落とさない
        return f'<h2>止まっている作業</h2><p class="empty">確認できませんでした（{html.escape(type(e).__name__)}）。</p>'

    need = [i for i in items if i["status"] in ("review", "unshipped", "unknown")]
    other = [i for i in items if i not in need]
    if not items:
        return '<h2>止まっている作業</h2><p class="empty">ありません。</p>'

    def card(i: dict, warn: bool) -> str:
        label, note = STALL_JA.get(i["status"], (i["status"], ""))
        d = f'{i["days"]}日前' if i.get("days") is not None else "不明"
        tag = f'<span class="tag {"harm" if warn else ""}">{html.escape(label)}</span>'
        return ('<div class="er"><div class="hd">'
                f'<span class="id">{html.escape(i["name"])}</span>'
                f'<span class="ti">{html.escape(i["why"])}</span>{tag}'
                f'<span class="tag">最終更新 {d}</span></div>'
                + (f'<div class="sm">{html.escape(note)}</div>' if note and warn else "")
                + "</div>")

    out = ["<h2>止まっている作業</h2>"]
    if need:
        out.append('<p class="sm-lead">下の項目は<strong>放っておくと問題になります</strong>。</p>')
        out += [card(i, True) for i in need]
    else:
        out.append('<p class="empty">放っておくと問題になるものはありません。</p>')
    out += [card(i, False) for i in other]
    return "".join(out)


def cmd_report(items: list[dict], out: pathlib.Path) -> None:
    """判断する人向けの概要。**詳細はリポジトリ側にある**ので、ここは要点だけ。"""
    groups: dict[str, list[dict]] = {}
    for i in items:
        groups.setdefault(i.get("status", "parked"), []).append(i)

    parts = []
    for st in STATUS_ORDER:
        g = groups.get(st, [])
        if st == "parked":
            head = f"積んである（{len(g)} 件）"
            lead = "<p class='empty'>いまは何もありません。</p>"
        elif st == "doing":
            head = f"着手中（{len(g)} 件）"
            lead = "<p class='empty'>着手中のものはありません。</p>"
        elif st == "done":
            head = f"完了（{len(g)} 件）"
            lead = "<p class='empty'>まだありません。</p>"
        else:
            head = f"見送り（{len(g)} 件）"
            lead = "<p class='empty'>ありません。</p>"
        cards = []
        for i in g:
            tags = []
            if i.get("source") == "実害":
                tags.append('<span class="tag harm">実害あり</span>')
            elif i.get("source"):
                tags.append(f'<span class="tag">{html.escape(SOURCE_JA.get(i["source"], i["source"]))}</span>')
            if i.get("size"):
                tags.append(f'<span class="tag size">{html.escape(SIZE_JA.get(i["size"], i["size"]))}</span>')
            if i.get("pr"):
                tags.append(f'<span class="tag">PR #{html.escape(i["pr"])}</span>')
            cards.append(
                '<div class="er"><div class="hd">'
                f'<span class="id">{html.escape(i.get("id",""))}</span>'
                f'<span class="ti">{html.escape(i.get("title",""))}</span>'
                + "".join(tags) + "</div>"
                + (f'<div class="sm">{html.escape(i.get("_summary",""))}</div>' if i.get("_summary") else "")
                + "</div>")
        parts.append(f"<h2>{head}</h2>" + ("".join(cards) if cards else lead))

    today = datetime.date.today().isoformat()
    doc = f"""<meta charset="utf-8">
<title>JetUse 改善要望（ER）一覧</title>
<style>{CSS}</style>
<div class="wrap">
<h1>JetUse 改善要望（ER）</h1>
<p class="sub">後で実装する項目の置き場 — {today} 時点</p>

<div class="note">
  <p><strong>この一覧は急ぎません。</strong> いま進めている実装に集中していただくためのもので、
  「あとで手を付ける候補」を取りこぼさないように置いてあります。</p>
  <p><strong>次に何をやるか決めるとき</strong>に、ここを眺めて選んでください。
  <strong>「実害あり」は実際に踏んだもの</strong>で、それ以外は気づきや構想です。</p>
  <p>詳細（根拠・直し方・やらない場合の代償）は<strong>リポジトリの
  <code>docs/enhance/</code></strong> にあります。</p>
</div>

{''.join(parts)}

{_stalled_section()}

<div class="foot">
  自動生成: <code>ops/er.py report</code> ／ 元データ: <code>docs/enhance/ER-*.md</code>（{len(items)} 件）<br>
  手で編集しても次の生成で消えます。内容を直すときは元データを編集してください。
</div>
</div>
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    print(f"{out} を生成（{len(items)} 件）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["list", "index", "report"])
    ap.add_argument("--out", default="", help="report の出力先（既定は .obsidian-dir から解決）")
    args = ap.parse_args()

    items = load()
    if args.command == "list":
        cmd_list(items)
    elif args.command == "index":
        cmd_index(items)
    else:
        out = args.out
        if not out:
            marker = ROOT / ".obsidian-dir"
            if not marker.exists():
                print("出力先が決まりません（--out を渡すか .obsidian-dir を置いてください）",
                      file=sys.stderr)
                return 2
            base = pathlib.Path(marker.read_text(encoding="utf-8").strip().splitlines()[0])
            out = base / "_renders" / "JetUse_改善要望一覧.html"
        cmd_report(items, pathlib.Path(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
