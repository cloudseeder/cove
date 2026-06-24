/**
 * Cryptographic primitives — TS counterpart of `cove.crypto`.
 *
 * The wire contract that has to hold across Python and TS:
 *   - sha256_hex(bytes) is identical (sha256 is unambiguous)
 *   - sign(priv, msg) produces an Ed25519 signature that verify(pub, sig, msg) accepts
 *     (Ed25519 is deterministic; both sides use the same algorithm — Python via PyNaCl,
 *      TS via @noble/curves)
 *   - canonicalize(obj) produces byte-identical output to Python's rfc8785.dumps()
 *     (JCS / RFC 8785 — implemented inline below)
 *
 * If any of these drift, signatures stop verifying. The vitest suite pins
 * the contract against fixtures captured from the Python side.
 */
import { ed25519 } from '@noble/curves/ed25519';
import { sha256 } from '@noble/hashes/sha256';
import { bytesToHex, hexToBytes, utf8ToBytes } from '@noble/hashes/utils';

// ---- hashing ----------------------------------------------------------
export function sha256Hex(data: Uint8Array): string {
  return bytesToHex(sha256(data));
}

export function contentId(content: unknown): string {
  return 'sha256:' + sha256Hex(canonicalize(content));
}

// ---- Ed25519 ----------------------------------------------------------
export function sign(privateHex: string, message: Uint8Array): string {
  const sig = ed25519.sign(message, hexToBytes(privateHex));
  return bytesToHex(sig);
}

export function verify(publicHex: string, signatureHex: string, message: Uint8Array): boolean {
  try {
    return ed25519.verify(hexToBytes(signatureHex), message, hexToBytes(publicHex));
  } catch {
    return false;
  }
}

// ---- JCS canonicalization (RFC 8785) ----------------------------------
// Python uses rfc8785.dumps(). For the wire contract, this MUST produce
// byte-identical output. The algorithm is small and deterministic:
//
//   - JSON values are produced per RFC 8785 §3.2 (number formatting)
//     and §3.4 (string escaping rules — exactly JSON.stringify's
//     standard escape set, no \/ escape).
//   - Object keys are sorted lexicographically by UTF-16 code unit
//     order (RFC 8785 §3.2.3) — what JavaScript strings compare as
//     by default.
//   - No insignificant whitespace.

export function canonicalize(value: unknown): Uint8Array {
  return utf8ToBytes(canonicalStringify(value));
}

function canonicalStringify(value: unknown): string {
  if (value === null) return 'null';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') return canonicalNumber(value);
  if (typeof value === 'string') return canonicalString(value);
  if (Array.isArray(value)) return '[' + value.map(canonicalStringify).join(',') + ']';
  if (typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    const keys = Object.keys(obj).sort(); // default string sort = UTF-16 code unit order
    return '{' + keys.map((k) =>
      canonicalString(k) + ':' + canonicalStringify(obj[k])
    ).join(',') + '}';
  }
  throw new Error(`cannot canonicalize value of type ${typeof value}`);
}

function canonicalNumber(n: number): string {
  if (!Number.isFinite(n)) throw new Error('JCS: non-finite numbers not allowed');
  if (Number.isInteger(n)) return n.toString();
  // RFC 8785 §3.2.2.2 defers to ECMAScript's Number toString for non-integers,
  // which is what we want.
  return n.toString();
}

function canonicalString(s: string): string {
  // JCS string encoding is exactly JSON.stringify of a string, minus the
  // optional `/` escape (which JSON.stringify doesn't emit anyway).
  return JSON.stringify(s);
}
