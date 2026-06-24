#!/usr/bin/env node
/**
 * Redwood トークン抽出スクリプト (UI-01 / docs/ui/plan.md Phase 1)
 *
 * oj-redwood.css から `--oj-*: 値;` をすべて収集する。
 * - 同名変数の「最初の定義」をライトテーマ、2回目以降の再定義をダークテーマとして別管理
 *   (後勝ちで上書きしない。ダーク内では後の再定義が勝つ)
 * - `rgb(var(--oj-palette-neutral-rgb-0))` のような多段 var() 参照は実値へ再帰解決
 * - 出力:
 *   - src/styles/tokens.css   … :root=ライト / [data-theme="dark"]=ダーク(再定義分のみ)
 *   - docs/ui/tokens-report.md … 主要トークンの一覧表
 *
 * 使い方: node scripts/extract-redwood-tokens.mjs <path-to-oj-redwood.css>
 */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, resolve as resolvePath } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolvePath(__dirname, '..');
const repoRoot = resolvePath(webRoot, '../..');

const cssPath = process.argv[2];
if (!cssPath) {
  console.error('usage: node extract-redwood-tokens.mjs <oj-redwood.css>');
  process.exit(1);
}

let css = readFileSync(cssPath, 'utf8');
// コメント除去(値の誤検出防止)
css = css.replace(/\/\*[\s\S]*?\*\//g, '');

// ---- 収集: --oj-* 宣言を括弧/引用符を考慮してスキャン ----
const light = new Map(); // 最初の定義
const dark = new Map(); // 2回目以降の再定義(ダーク内は後勝ち)
const occurrences = new Map(); // name -> 定義回数

const declRe = /(--oj-[A-Za-z0-9_-]+)\s*:/g;
let m;
while ((m = declRe.exec(css)) !== null) {
  const name = m[1];
  // 値の終端を探す: 括弧深度0かつ引用符外の ';' または '}'
  let i = declRe.lastIndex;
  let depth = 0;
  let quote = null;
  let value = '';
  for (; i < css.length; i++) {
    const ch = css[i];
    if (quote) {
      if (ch === quote && css[i - 1] !== '\\') quote = null;
      value += ch;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      value += ch;
      continue;
    }
    if (ch === '(') depth++;
    if (ch === ')') depth--;
    if ((ch === ';' || ch === '}') && depth <= 0) break;
    value += ch;
  }
  declRe.lastIndex = i;
  value = value.replace(/\s+/g, ' ').trim();
  if (!value) continue;

  const n = (occurrences.get(name) ?? 0) + 1;
  occurrences.set(name, n);
  if (n === 1) {
    light.set(name, value);
  } else {
    dark.set(name, value); // ダーク内は後勝ち
  }
}

// ---- 再帰解決 ----
function lookup(name, theme) {
  if (theme === 'dark' && dark.has(name)) return dark.get(name);
  return light.get(name);
}

function resolveValue(value, theme, stack = new Set()) {
  // var(--name) / var(--name, fallback) を再帰的に展開
  let out = '';
  let i = 0;
  while (i < value.length) {
    const idx = value.indexOf('var(', i);
    if (idx === -1) {
      out += value.slice(i);
      break;
    }
    out += value.slice(i, idx);
    // var( ... ) の対応括弧を探す
    let depth = 1;
    let j = idx + 4;
    for (; j < value.length && depth > 0; j++) {
      if (value[j] === '(') depth++;
      if (value[j] === ')') depth--;
    }
    const inner = value.slice(idx + 4, j - 1);
    const commaAt = (() => {
      let d = 0;
      for (let k = 0; k < inner.length; k++) {
        if (inner[k] === '(') d++;
        if (inner[k] === ')') d--;
        if (inner[k] === ',' && d === 0) return k;
      }
      return -1;
    })();
    const refName = (commaAt === -1 ? inner : inner.slice(0, commaAt)).trim();
    const fallback = commaAt === -1 ? null : inner.slice(commaAt + 1).trim();

    let replacement;
    if (stack.has(refName)) {
      replacement = `/* circular:${refName} */`;
    } else {
      const refValue = lookup(refName, theme);
      if (refValue !== undefined) {
        stack.add(refName);
        replacement = resolveValue(refValue, theme, stack);
        stack.delete(refName);
      } else if (fallback !== null) {
        replacement = resolveValue(fallback, theme, stack);
      } else {
        replacement = `var(${refName})`; // 未解決はそのまま残す
      }
    }
    out += replacement;
    i = j;
  }
  return normalizeColors(out);
}

// rgb(49,45,42) → #312d2a に正規化(可読性のため)。rgba/alpha付きはそのまま
function normalizeColors(value) {
  return value
    .replace(
      /\brgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)/g,
      (_, r, g, b) => {
        const hex = (n) => Number(n).toString(16).padStart(2, '0');
        return `#${hex(r)}${hex(g)}${hex(b)}`;
      },
    )
    // hex色varをrgba()に入れ子にした定義の救済: rgba(#rrggbb, a) → rgba(r, g, b, a)
    .replace(
      /\brgba\(\s*#([0-9a-fA-F]{6})\s*,/g,
      (_, hex) =>
        `rgba(${parseInt(hex.slice(0, 2), 16)}, ${parseInt(hex.slice(2, 4), 16)}, ${parseInt(hex.slice(4, 6), 16)},`,
    );
}

const lightResolved = new Map();
for (const [name, value] of light) lightResolved.set(name, resolveValue(value, 'light'));
const darkResolved = new Map();
for (const [name, value] of dark) darkResolved.set(name, resolveValue(value, 'dark'));

// ---- tokens.css 出力 ----
const sortedLight = [...lightResolved.keys()].sort();
const sortedDark = [...darkResolved.keys()].sort();

let tokensCss = `/*\n * Redwood Design System トークン (oj-redwood.css から自動抽出・解決済み)\n * 生成: packages/web/scripts/extract-redwood-tokens.mjs — 手動編集禁止\n * ライト=ファイル内最初の定義 / ダーク=再定義分のみ\n */\n:root {\n`;
for (const name of sortedLight) tokensCss += `  ${name}: ${lightResolved.get(name)};\n`;
tokensCss += `}\n\n[data-theme="dark"] {\n`;
for (const name of sortedDark) tokensCss += `  ${name}: ${darkResolved.get(name)};\n`;
tokensCss += `}\n`;

const tokensCssPath = resolvePath(webRoot, 'src/styles/tokens.css');
mkdirSync(dirname(tokensCssPath), { recursive: true });
writeFileSync(tokensCssPath, tokensCss);

// ---- tokens-report.md 出力 ----
function table(names) {
  const rows = names
    .filter((n) => lightResolved.has(n))
    .map((n) => {
      const l = lightResolved.get(n);
      const d = darkResolved.has(n) ? darkResolved.get(n) : '(ライトと同じ)';
      return `| \`${n}\` | \`${l}\` | \`${d}\` |`;
    });
  if (rows.length === 0) return '_(該当なし)_\n';
  return ['| 変数 | ライト | ダーク |', '|---|---|---|', ...rows].join('\n') + '\n';
}

function pick(re) {
  return sortedLight.filter((n) => re.test(n));
}

const neutralNames = pick(/^--oj-palette-neutral-rgb-\d+$/).sort((a, b) => {
  const num = (s) => Number(s.match(/(\d+)$/)[1]);
  return num(a) - num(b);
});
const brandNames = pick(/^--oj-palette-brand-rgb-\d+$/).sort((a, b) => {
  const num = (s) => Number(s.match(/(\d+)$/)[1]);
  return num(a) - num(b);
});

const report = `# Redwood トークン抽出レポート (UI-01)

- 抽出元: \`@oracle/oraclejet\` \`dist/css/redwood/oj-redwood.css\`
- 抽出数: ライト ${lightResolved.size} 変数 / ダーク(再定義) ${darkResolved.size} 変数
- 出力: \`packages/web/src/styles/tokens.css\`
- ルール: ファイル内最初の定義=ライト、2回目以降=ダーク。\`var()\` 多段参照は実値へ再帰解決し、\`rgb(R,G,B)\` は hex に正規化

## Brand パレット (--oj-palette-brand-rgb-*)

${table(brandNames)}

## Neutral パレット (--oj-palette-neutral-rgb-*)

確認ポイント: **neutral-170 がヘッダー用ダーク色 (#312D2A 系) として抽出できているか**

${table(neutralNames)}

## Danger / Warning / Success / Info パレット

${table(pick(/^--oj-palette-(danger|warning|success|info)-rgb-\d+$/))}

## テキスト色 (core text)

${table(pick(/^--oj-core-text-color/))}

## 背景・サーフェス色 (core bg / neutral色適用先)

${table(pick(/^--oj-core-(bg-color|neutral)/))}

## ボーダー・分割線

${table(pick(/(divider-color|border-color)/).slice(0, 30))}

## Border radius

${table(pick(/border-radius/))}

## Box shadow

${table(pick(/box-shadow/))}

## Typography

${table(pick(/^--oj-typography/))}

## リンク・フォーカス等のセマンティック色 (抜粋)

${table(pick(/^--oj-core-(link|focus|drag|overlay|brand)/))}
`;

const reportPath = resolvePath(repoRoot, 'docs/ui/tokens-report.md');
writeFileSync(reportPath, report);

console.log(`light vars: ${lightResolved.size}`);
console.log(`dark vars (redefined): ${darkResolved.size}`);
console.log(`wrote: ${tokensCssPath}`);
console.log(`wrote: ${reportPath}`);
// 確認ポイントの即時チェック
const n170 = lightResolved.get('--oj-palette-neutral-rgb-170');
console.log(`neutral-170 (light) = ${n170 ?? 'NOT FOUND'}`);
