/**
 * Pairing payload encoder/decoder. v0.4.0 onboarding flow.
 *
 * After a device generates its keypair on-device, it shows the keymaster
 * a pairing payload — encoded as both a `cove://pair?…` deep link and
 * a QR code — that the keymaster opens in their (already-authenticated)
 * Cove client. The admin UI parses the payload, prefills the attestation
 * form (name hint, hub URL), and the keymaster signs + posts.
 *
 * The payload carries NO trust. Its job is routing metadata only — the
 * pubkey, the hub URL it should attest against, and the device's self-
 * reported name hint so the keymaster recognizes the request. Trust
 * flows from the channel the QR/link travelled over (in-person scan,
 * verified Signal thread, etc.) — same pattern as Steam Guard /
 * WhatsApp Web pairing.
 */
import qrcode from 'qrcode-generator';

export interface PairingPayload {
  /** Hub origin the device wants to attest against. */
  hub: string;
  /** Member's freshly-generated public key, 64-char hex. */
  pubkey: string;
  /** Self-reported display-name hint. The keymaster overrides this
   *  on attest — it's a UX affordance for the queue, not a binding
   *  claim. */
  name: string;
}

/** Build the `cove://pair?…` deep link form of a pairing payload.
 *  The keymaster's Cove client (or any handler the OS dispatches
 *  cove:// to) parses this back to a PairingPayload. */
export function encodePairingLink(p: PairingPayload): string {
  const params = new URLSearchParams({
    hub: p.hub,
    pubkey: p.pubkey,
    name: p.name,
  });
  return `cove://pair?${params.toString()}`;
}

/** Parse a cove://pair?… deep link; throws on a malformed payload so
 *  the admin UI can surface a clear error rather than silently filling
 *  the form with junk. */
export function decodePairingLink(uri: string): PairingPayload {
  let url: URL;
  try {
    url = new URL(uri);
  } catch {
    throw new Error('Not a valid URI');
  }
  if (url.protocol !== 'cove:') {
    throw new Error(`Not a Cove pairing link: ${uri}`);
  }
  // Tauri's deep-link plugin emits `cove://pair?…` where the host is
  // 'pair'; some other platforms emit `cove:pair?…` where the pathname
  // is 'pair'. Accept both shapes.
  const isPair = url.host === 'pair' || url.pathname === 'pair'
    || url.pathname.endsWith('/pair');
  if (!isPair) {
    throw new Error(`Not a Cove pairing link: ${uri}`);
  }
  const hub = url.searchParams.get('hub');
  const pubkey = url.searchParams.get('pubkey');
  const name = url.searchParams.get('name');
  if (!hub || !pubkey || !name) {
    throw new Error('Pairing link missing hub/pubkey/name');
  }
  if (!/^[0-9a-f]{64}$/.test(pubkey)) {
    throw new Error('Pubkey must be 64-char hex');
  }
  return { hub, pubkey, name };
}

/** Human-readable fingerprint of a pubkey. Eight 4-char hex blocks
 *  separated by `-`. Short enough to read aloud, distinctive enough
 *  to compare visually. The keymaster sanity-checks this against the
 *  one shown on the requesting device when the channel might not be
 *  fully trusted (e.g. screenshot over Slack). For an in-person
 *  scan the QR alone is enough; the fingerprint is a fallback.
 *
 *  16 bytes (128 bits) of collision space against the requester's own
 *  pubkey — plenty for a human-in-the-loop check, and short enough to
 *  read over a phone in ~15 seconds. */
export function fingerprint(pubkey: string): string {
  if (!/^[0-9a-f]{64}$/.test(pubkey)) {
    throw new Error('Pubkey must be 64-char hex');
  }
  const head = pubkey.slice(0, 32);
  const groups: string[] = [];
  for (let i = 0; i < head.length; i += 4) groups.push(head.slice(i, i + 4));
  return groups.join('-').toUpperCase();
}

/** Generate an SVG QR code for an arbitrary payload string. Uses a
 *  battle-tested encoder (qrcode-generator) — hand-rolling QR risks
 *  subtle indexing bugs that produce codes which "work" in some
 *  readers and fail in others. ECC level M is the default tradeoff
 *  for a clean digital display; the lib picks the smallest QR version
 *  that fits. Returns an SVG string the UI inserts via {@html ...}. */
export function qrSvg(payload: string, opts: { size?: number } = {}): string {
  // typeNumber=0 lets the lib auto-pick the smallest version that fits.
  const qr = qrcode(0, 'M');
  qr.addData(payload);
  qr.make();
  // qrcode-generator's createSvgTag emits an SVG element string; we
  // pull module size from the requested px and a quiet zone of 4 modules.
  const size = opts.size ?? 256;
  const count = qr.getModuleCount();
  const modulePx = size / (count + 8);
  const rects: string[] = [];
  for (let y = 0; y < count; y++) {
    for (let x = 0; x < count; x++) {
      if (qr.isDark(y, x)) {
        rects.push(
          `<rect x="${(x + 4) * modulePx}" y="${(y + 4) * modulePx}" `
          + `width="${modulePx}" height="${modulePx}" fill="#0a0a0a"/>`,
        );
      }
    }
  }
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" `
    + `width="${size}" height="${size}">`
    + `<rect width="${size}" height="${size}" fill="#f5f0e8"/>`
    + rects.join('') + '</svg>'
  );
}
