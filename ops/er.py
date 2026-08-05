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


# --- Markdown → HTML の最小変換 -------------------------------------------------
# ER 本文で使う範囲だけを扱う（見出し・段落・箇条書き・表・強調・コード・引用）。
# 依存を増やさないための割り切りで、**汎用の Markdown 変換ではない**。

def _inline(t: str) -> str:
    t = html.escape(t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    return t


def _md(text: str) -> str:
    out, i = [], 0
    lines = text.split("\n")
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue
        if ln.startswith("#"):
            n = len(ln) - len(ln.lstrip("#"))
            out.append(f"<h{min(n+1,4)}>{_inline(ln.lstrip('# ').strip())}</h{min(n+1,4)}>")
            i += 1
        elif ln.lstrip().startswith("|") and i + 1 < len(lines) and set(lines[i+1].replace("|", "").strip()) <= set("-: "):
            hdr = [c.strip() for c in ln.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            th = "".join(f"<th>{_inline(c)}</th>" for c in hdr)
            tb = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{tb}</tbody></table></div>')
        elif ln.lstrip().startswith(("- ", "* ")):
            items = []
            while i < len(lines) and lines[i].lstrip().startswith(("- ", "* ")):
                items.append(f"<li>{_inline(lines[i].lstrip()[2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
        elif ln.lstrip().startswith(">"):
            buf = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                buf.append(lines[i].lstrip().lstrip(">").strip())
                i += 1
            out.append(f"<blockquote>{_inline(' '.join(buf))}</blockquote>")
        else:
            # **1行目は無条件に取り込む。** 段落の継続条件だけでループを回すと、`|` で始まるのに
            # 表として解釈されなかった行（次行が区切りでない＝折り返した表など）で継続条件が
            # 即 false になり、`i` が進まないまま外側 while が回り続けて**永久に固まる**
            # （2026-08-05 の実害: ER-0012 の折り返した表で `ops/er.py report` がハング）。
            buf = [ln.strip()]
            i += 1
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", "|", ">")) \
                    and not lines[i].lstrip().startswith(("- ", "* ")):
                buf.append(lines[i].strip())
                i += 1
            out.append(f"<p>{_inline(' '.join(buf))}</p>")
    return "".join(out)


DETAIL_CSS = """
:root{--bg:#f8fafc;--fg:#0f172a;--mut:#64748b;--bd:#e2e8f0;--card:#fff;
      --ok:#059669;--ng:#dc2626;--warn:#b45309;--acc:#4f46e5}
@media(prefers-color-scheme:dark){:root{--bg:#0b1220;--fg:#e2e8f0;--mut:#94a3b8;
      --bd:#1e293b;--card:#111a2e}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:15.5px/1.85 system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:30px 22px 70px}
.eyebrow{color:var(--mut);font-size:12.5px;letter-spacing:.06em;margin:0 0 6px}
h1{font-size:26px;line-height:1.4;margin:0 0 10px}
.meta{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 26px}
.tag{font-size:11.5px;padding:3px 11px;border-radius:99px;border:1px solid var(--bd);color:var(--mut)}
.tag.harm{color:var(--ng);border-color:var(--ng)}
.lead{background:var(--card);border:1px solid var(--bd);border-left:4px solid var(--acc);
      border-radius:10px;padding:16px 20px;margin:0 0 28px;font-size:16px}
h2{font-size:17px;margin:32px 0 12px;padding-bottom:7px;border-bottom:1px solid var(--bd)}
h3{font-size:15px;margin:22px 0 8px}
p{margin:0 0 14px}
ul{margin:0 0 14px;padding-left:1.3em}
li{margin:5px 0}
code{background:var(--card);border:1px solid var(--bd);border-radius:5px;
     padding:1px 6px;font-size:.88em}
blockquote{margin:0 0 14px;padding:10px 16px;border-left:3px solid var(--warn);
           background:var(--card);border-radius:0 8px 8px 0;color:var(--mut)}
.tw{overflow-x:auto;margin:0 0 16px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{border:1px solid var(--bd);padding:8px 12px;text-align:left;vertical-align:top}
th{background:var(--card);font-weight:600}
.foot{margin-top:44px;padding-top:16px;border-top:1px solid var(--bd);
      color:var(--mut);font-size:12.5px}
"""


def _detail_html(item: dict, body: str) -> str:
    tags = []
    if item.get("source") == "実害":
        tags.append('<span class="tag harm">実害あり</span>')
    elif item.get("source"):
        tags.append(f'<span class="tag">{html.escape(SOURCE_JA.get(item["source"], item["source"]))}</span>')
    if item.get("size"):
        tags.append(f'<span class="tag">大きさ: {html.escape(SIZE_JA.get(item["size"], item["size"]))}</span>')
    tags.append(f'<span class="tag">{html.escape(STATUS_JA.get(item.get("status",""), ""))}</span>')
    if item.get("pr"):
        tags.append(f'<span class="tag">PR #{html.escape(item["pr"])}</span>')

    # 「## ひとことで」は導入として別扱いにする
    lead = item.get("_summary", "")
    rest = re.sub(r"##\s*ひとことで\s*\n+.*?(?=\n##|\Z)", "", body, count=1, flags=re.S)

    return f"""<meta charset="utf-8">
<title>{html.escape(item.get('id',''))} {html.escape(item.get('title',''))}</title>
<style>{DETAIL_CSS}</style>
<div class="wrap">
<p class="eyebrow">JetUse 改善要望 · {html.escape(item.get('id',''))}</p>
<h1>{html.escape(item.get('title',''))}</h1>
<div class="meta">{''.join(tags)}</div>
{f'<div class="lead">{_inline(lead)}</div>' if lead else ''}
{_md(rest.strip())}
<div class="foot">
  自動生成 — 元データ: <code>{html.escape(item.get('_path',''))}</code><br>
  手で編集しても次の生成で消えます。内容を直すときは元データを編集してください。
</div>
</div>
"""


STALL_JA = {
    "review": "レビューが終わっていない",
    "unshipped": "PR が出ていない",
    "unknown": "状態が読めない",
    "pr": "PR が出ている",
    "parked": "意図的に止めている",
}


def _stalled_rows() -> tuple[list[str], list[str]]:
    """止まっている作業。**要注意** と **それ以外** に分けて返す。"""
    try:
        sys.path.insert(0, str(ROOT / "ops"))
        import stalled  # noqa: PLC0415
        items = stalled.scan()
    except Exception:
        return (["| — | 確認できませんでした | — |"], [])
    warn, rest = [], []
    for i in items:
        d = f'{i["days"]}日前' if i.get("days") is not None else "不明"
        row = f'| `{i["name"]}` | {STALL_JA.get(i["status"], i["status"])} | {i["why"]} | {d} |'
        (warn if i["status"] in ("review", "unshipped", "unknown") else rest).append(row)
    return warn, rest


def cmd_report(items: list[dict], outdir: pathlib.Path) -> None:
    """Obsidian へ出す。**index は Markdown・詳細は ER ごとの HTML。**

    index を Markdown にするのは、Obsidian でそのまま読めて**リンクが効く**ため。
    詳細を HTML にするのは、表や強調を整えた形で**説明として読ませたい**ため。
    """
    outdir.mkdir(parents=True, exist_ok=True)

    # 1) 詳細 HTML（ER ごと）
    for i in items:
        body = (ROOT / i["_path"]).read_text(encoding="utf-8")
        body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", body, count=1, flags=re.S)
        (outdir / f'{i["id"]}.html').write_text(_detail_html(i, body), encoding="utf-8")

    # 2) index（Markdown）
    today = datetime.date.today().isoformat()
    parked = [i for i in items if i.get("status") == "parked"]
    doing = [i for i in items if i.get("status") == "doing"]
    done = [i for i in items if i.get("status") in ("done", "dropped")]

    def table(rows: list[dict]) -> list[str]:
        if not rows:
            return ["", "なし。", ""]
        out = ["", "| ID | 内容 | 種別 | 大きさ |", "|---|---|---|---|"]
        for i in rows:
            mark = "**実害**" if i.get("source") == "実害" else SOURCE_JA.get(i.get("source", ""), "")
            out.append(f'| [[{i["id"]}.html\\|{i["id"]}]] | {i.get("title","")} '
                       f'| {mark} | {SIZE_JA.get(i.get("size",""), i.get("size",""))} |')
        out.append("")
        return out

    warn, rest = _stalled_rows()
    md = [
        "# JetUse 改善要望（ER）",
        "",
        f"最終更新: {today} ／ 全 {len(items)} 件",
        "",
        "> [!note] この一覧は急ぎません",
        "> いま進めている実装に集中していただくためのもので、",
        "> **後で手を付ける候補を取りこぼさない**ように置いてあります。",
        "> 次に何をやるか決めるときに眺めて選んでください。",
        "",
        "**ID をクリックすると詳しい説明が開きます。**",
        "「実害」は実際に踏んだもの、それ以外は気づきや構想です。",
        "",
        "## 積んである（次に選ぶならここから）",
        *table(parked),
        "## 着手中",
        *table(doing),
    ]
    if done:
        md += ["## 終わったもの", *table(done)]

    md += [
        "## 止まっている作業",
        "",
        "**やりかけを取りこぼさないための一覧です。** リポジトリの実際の状態から毎回作ります。",
        "",
    ]
    if warn:
        md += ["**下の項目は放っておくと問題になります。**", "",
               "| 作業 | 状態 | 内容 | 最終更新 |", "|---|---|---|---|", *warn, ""]
    else:
        md += ["放っておくと問題になるものはありません。", ""]
    if rest:
        md += ["| 作業 | 状態 | 内容 | 最終更新 |", "|---|---|---|---|", *rest, ""]

    md += [
        "---",
        "",
        f"自動生成: `ops/er.py report` ／ 元データ: `docs/enhance/ER-*.md`（{len(items)} 件）",
        "**手で編集しても次の生成で消えます。** 内容を直すときは元データを編集してください。",
        "",
    ]
    (outdir / "index.md").write_text("\n".join(md), encoding="utf-8")
    print(f"{outdir}/index.md と詳細 {len(items)} 件を生成")



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["list", "index", "report"])
    ap.add_argument("--out", default="", help="report の出力先ディレクトリ（既定は .obsidian-dir/_renders/ER）")
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
            # vault 内で書いてよいのは `_renders/` 配下だけ（ノートは読み取り専用）
            out = base / "_renders" / "ER"
        cmd_report(items, pathlib.Path(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
