// 鍵レス受け渡し: 期限付き PAR URL への GET/PUT(SP3-08 / ADR-0023 §1)。
// node 同梱 fetch を使う(curl/ca-certificates を足さない — node は自前 CA を持つ)。
// usage: node xfer.mjs get <url> <outfile> | node xfer.mjs put <url> <infile>
import { readFileSync, writeFileSync } from "node:fs";

const [mode, url, file] = process.argv.slice(2);
if (!mode || !url || !file) {
  console.error("usage: xfer.mjs {get|put} <url> <file>");
  process.exit(2);
}
const res = await (mode === "put"
  ? fetch(url, { method: "PUT", body: readFileSync(file) })
  : fetch(url));
if (!res.ok) {
  console.error(`xfer ${mode} failed: HTTP ${res.status}`);
  process.exit(1);
}
if (mode === "get") writeFileSync(file, Buffer.from(await res.arrayBuffer()));
