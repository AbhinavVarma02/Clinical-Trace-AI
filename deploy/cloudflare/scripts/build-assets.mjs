// Assemble ./public for Workers Static Assets from the built React workspace (frontend/dist-workspace).
// The workspace keeps its /app/ base so the same build also works when served by FastAPI.
import { cpSync, existsSync, mkdirSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const source = join(root, "..", "..", "frontend", "dist-workspace");
const out = join(root, "public");

if (!existsSync(join(source, "index.html"))) {
  console.error(`Missing ${source}/index.html. Run "npm run build:workspace" in frontend/ first.`);
  process.exit(1);
}
// Clear the contents rather than the folder itself (a running `wrangler dev` watches it).
mkdirSync(out, { recursive: true });
for (const entry of readdirSync(out)) rmSync(join(out, entry), { recursive: true, force: true });
cpSync(source, join(out, "app"), { recursive: true });

const csp = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
  "font-src https://fonts.gstatic.com",
  "img-src 'self' data:",
  "connect-src 'self'",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

writeFileSync(join(out, "_headers"), `/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()
  X-Frame-Options: DENY
  Strict-Transport-Security: max-age=31536000
  Content-Security-Policy: ${csp}
/app/assets/*
  Cache-Control: public, max-age=31536000, immutable
/app/
  Cache-Control: no-cache
/app/index.html
  Cache-Control: no-cache
`);
writeFileSync(join(out, "_redirects"), "/ /app/ 302\n/app /app/ 301\n");
console.log(`public/ assembled from ${source}`);
