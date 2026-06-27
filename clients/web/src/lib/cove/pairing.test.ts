/**
 * Pairing payload encoder/decoder + fingerprint tests. v0.4.0.
 *
 * The encoder/decoder is the wire contract for the onboarding link:
 * the requesting device and the admin's Cove client must agree on
 * the URI format so the admin UI can prefill the attestation form
 * from a scanned QR. Round-trip + malformed-input coverage pins it.
 */
import { describe, expect, test } from 'vitest';
import { decodePairingLink, encodePairingLink, fingerprint, qrSvg } from './pairing';

describe('encode/decode pairing link', () => {
  test('round-trips a typical payload', () => {
    const p = {
      hub: 'https://cove.oap.dev',
      pubkey: 'a'.repeat(64),
      name: 'Jane Doe',
    };
    const link = encodePairingLink(p);
    expect(link.startsWith('cove://pair?')).toBe(true);
    const back = decodePairingLink(link);
    expect(back).toEqual(p);
  });

  test('URL-encodes special characters in the name hint', () => {
    const p = {
      hub: 'https://cove.oap.dev',
      pubkey: 'b'.repeat(64),
      name: 'Jane Doe & Co. (Lot 27)',
    };
    const link = encodePairingLink(p);
    expect(decodePairingLink(link).name).toBe(p.name);
  });

  test('rejects a non-Cove URI', () => {
    expect(() => decodePairingLink('https://example.com')).toThrow();
    expect(() => decodePairingLink('cove://something-else?x=1')).toThrow();
  });

  test('rejects missing fields', () => {
    expect(() => decodePairingLink('cove://pair?hub=x&pubkey=' + 'a'.repeat(64)))
      .toThrow(/missing/);
  });

  test('rejects a non-hex pubkey', () => {
    expect(() => decodePairingLink(
      'cove://pair?hub=x&pubkey=NOTHEX&name=Jane',
    )).toThrow(/hex/);
  });

  test('rejects a wrong-length pubkey', () => {
    expect(() => decodePairingLink(
      'cove://pair?hub=x&pubkey=' + 'a'.repeat(63) + '&name=Jane',
    )).toThrow(/hex/);
  });
});

describe('fingerprint', () => {
  test('returns 8 4-char hex blocks separated by dashes', () => {
    const fp = fingerprint('0123456789abcdef' + '0'.repeat(48));
    expect(fp).toBe('0123-4567-89AB-CDEF-0000-0000-0000-0000');
  });

  test('throws on non-hex input', () => {
    expect(() => fingerprint('xyz')).toThrow();
  });

  test('is deterministic for the same pubkey', () => {
    const pk = 'deadbeef'.repeat(8);
    expect(fingerprint(pk)).toBe(fingerprint(pk));
  });
});

describe('qrSvg', () => {
  test('produces an SVG with content for a typical pairing payload', () => {
    const link = encodePairingLink({
      hub: 'https://cove.oap.dev', pubkey: 'a'.repeat(64), name: 'Jane',
    });
    const svg = qrSvg(link);
    expect(svg.startsWith('<svg')).toBe(true);
    // Must contain dark module rects — the QR is non-empty.
    expect(svg.includes('<rect')).toBe(true);
    // Sanity check the viewBox + size match what the caller asked for.
    expect(svg).toContain('viewBox="0 0 256 256"');
  });

  test('respects a custom size', () => {
    const svg = qrSvg('hello', { size: 512 });
    expect(svg).toContain('viewBox="0 0 512 512"');
  });
});
