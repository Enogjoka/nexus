// Replace observatory/static with this project's out/ (the static export).
// Equivalent to:  rm -rf ../static && cp -R out ../static
import { cpSync, existsSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const web = join(dirname(fileURLToPath(import.meta.url)), "..");
const out = join(web, "out");
const target = join(web, "..", "static");

if (!existsSync(join(out, "index.html"))) {
  console.error("out/index.html missing: run `npm run build` first.");
  process.exit(1);
}
rmSync(target, { recursive: true, force: true });
cpSync(out, target, { recursive: true });
console.log(`published ${out} -> ${target}`);
