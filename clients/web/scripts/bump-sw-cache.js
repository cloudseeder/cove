#!/usr/bin/env node
/**
 * v0.4.56: rewrite build/sw.js's CACHE constant to include the current
 * package.json version. Runs as a postbuild step after
 * @sveltejs/adapter-static has copied static/sw.js into build/.
 *
 * Why:
 *   The SW's activate handler only deletes caches whose name doesn't
 *   match the running CACHE constant. Prior to v0.4.56 the constant
 *   sat at 'cove-shell-v0.4.29' across 26 releases — the constant
 *   never changed, so activate never fired cleanup, and installed
 *   PWAs accumulated stale asset caches indefinitely. Each release
 *   now gets a distinct cache name via this script.
 *
 * How to update sw.js manually:
 *   Leave the placeholder `const CACHE = 'cove-shell-vDEV';` in
 *   static/sw.js. This script rewrites the copied-to-build/ version
 *   only — the source stays untouched so dev-server behaviour is
 *   unaffected.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const here = new URL('.', import.meta.url);
const pkg = JSON.parse(
  readFileSync(fileURLToPath(new URL('../package.json', here)), 'utf-8'),
);
const swPath = fileURLToPath(new URL('../build/sw.js', here));

const placeholder = /const CACHE = ['"]cove-shell-[^'"]+['"];/;
const replacement = `const CACHE = 'cove-shell-v${pkg.version}';`;

let src;
try {
  src = readFileSync(swPath, 'utf-8');
} catch (err) {
  console.warn(`[bump-sw-cache] build/sw.js not found; skipping (${err.message})`);
  process.exit(0);
}

if (!placeholder.test(src)) {
  console.warn('[bump-sw-cache] no CACHE placeholder found in build/sw.js — skipped');
  process.exit(0);
}

writeFileSync(swPath, src.replace(placeholder, replacement), 'utf-8');
console.log(`[bump-sw-cache] build/sw.js CACHE = 'cove-shell-v${pkg.version}'`);
