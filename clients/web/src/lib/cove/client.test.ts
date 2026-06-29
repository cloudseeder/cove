/**
 * Client unit tests — mock fetch + WebSocket, run the verification
 * chain against fixtures. The integration with a real hub is exercised
 * by the Python e2e test suite (tests/test_e2e_broadcast.py); here we
 * pin the TS-specific behaviour:
 *
 *   - auth happy path returns a token; auth failure throws
 *     AuthenticationError
 *   - sync runs the §5 verification chain on every entry and returns
 *     VerifiedEntry objects with the ceremony data filled in
 *   - any chain link failure throws VerificationError WITHOUT
 *     advancing the high-water
 *   - WebSocket subscribe verifies pushed entries the same way sync does
 *   - sign-then-post produces canonical id+sig that round-trip through
 *     the wire shape
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';

import fixtures from './fixtures.json';
import { Client, InJSSigner, sigSummary, type Signer } from './client';
import { AuthenticationError, VerificationError } from './errors';
import type { DirectoryManifest, Entry, InclusionProof, STH } from './types';

const sth = fixtures.sth as STH;
const manifest = fixtures.manifest as DirectoryManifest;
const items = fixtures.entries as Array<{ entry: Entry; seq: number; proof: InclusionProof }>;
const alice = fixtures.keypairs.alice;

const HUB = 'http://hub.test';

function mockHub() {
  const sessionToken = 'a'.repeat(64);
  return {
    sessionToken,
    fetch: vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      const u = new URL(url.toString());
      const method = (init?.method ?? 'GET').toUpperCase();
      const auth = (init?.headers as Record<string, string> | undefined)?.authorization;

      if (u.pathname === '/auth/challenge' && method === 'POST') {
        return jsonResp({ nonce: 'd'.repeat(64), expires_at: 9_999_999_999 });
      }
      if (u.pathname === '/auth/verify' && method === 'POST') {
        return jsonResp({
          token: sessionToken,
          pubkey: alice.pub,
          expires_at: 9_999_999_999,
        });
      }
      // Everything below this requires auth.
      if (!auth?.startsWith('Bearer ')) {
        return jsonResp({ error: 'auth_required' }, 401);
      }
      if (u.pathname === '/directory') return jsonResp(manifest);
      if (u.pathname === '/sth') return jsonResp(sth);
      if (u.pathname === '/sync') {
        const since = parseInt(u.searchParams.get('since') ?? '-1', 10);
        const out = items
          .filter((it) => it.seq > since)
          .map((it) => ({ entry: it.entry, seq: it.seq }));
        return jsonResp({ entries: out, thread: u.searchParams.get('thread'), since });
      }
      if (u.pathname === '/proof/inclusion') {
        const id = u.searchParams.get('entry');
        const hit = items.find((it) => it.entry.id === id);
        if (!hit) return jsonResp({ error: 'not_found' }, 404);
        return jsonResp(hit.proof);
      }
      return jsonResp({ error: 'not_found' }, 404);
    }),
  };
}

function jsonResp(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

// ---- auth -----------------------------------------------------------

describe('authenticate', () => {
  test('round-trips challenge → signature → session token', async () => {
    const { fetch, sessionToken } = mockHub();
    const c = new Client({
      hubUrl: HUB, privateKey: alice.priv, publicKey: alice.pub, fetch,
    });
    const tok = await c.authenticate();
    expect(tok).toBe(sessionToken);
    expect(c.authenticated).toBe(true);
  });

  test('throws AuthenticationError on /auth/verify failure', async () => {
    const fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      const u = new URL(url.toString());
      if (u.pathname === '/auth/challenge') {
        return jsonResp({ nonce: 'd'.repeat(64), expires_at: 9_999_999_999 });
      }
      return jsonResp({ error: 'auth_failed', reason: 'unknown identity' }, 401);
    });
    const c = new Client({
      hubUrl: HUB, privateKey: alice.priv, publicKey: alice.pub, fetch: fetchMock,
    });
    await expect(c.authenticate()).rejects.toBeInstanceOf(AuthenticationError);
  });

  test('rejects gated operations before authenticate()', async () => {
    const { fetch } = mockHub();
    const c = new Client({
      hubUrl: HUB, privateKey: alice.priv, publicKey: alice.pub, fetch,
    });
    await expect(c.sync('annual-meeting')).rejects.toBeInstanceOf(AuthenticationError);
  });
});

// ---- sync + verify ---------------------------------------------------

describe('sync', () => {
  test('returns VerifiedEntry objects with ceremony data for every entry', async () => {
    const { fetch } = mockHub();
    const c = new Client({
      hubUrl: HUB, privateKey: alice.priv, publicKey: alice.pub, fetch,
    });
    await c.authenticate();
    const verified = await c.sync('annual-meeting');
    expect(verified.length).toBe(items.length);
    for (const ve of verified) {
      expect(ve.attestation.role).toBe('board');
      expect(sigSummary(ve)).toContain('Board');
      expect(sigSummary(ve)).toContain('inclusion proof position');
    }
  });

  test('advances high-water only after every entry verified', async () => {
    const { fetch } = mockHub();
    const c = new Client({
      hubUrl: HUB, privateKey: alice.priv, publicKey: alice.pub, fetch,
    });
    await c.authenticate();
    expect(c.highWaterFor('annual-meeting')).toBe(-1);
    await c.sync('annual-meeting');
    const maxSeq = Math.max(...items.map((it) => it.seq));
    expect(c.highWaterFor('annual-meeting')).toBe(maxSeq);
    // Second sync returns nothing; high-water unchanged.
    const again = await c.sync('annual-meeting');
    expect(again).toEqual([]);
    expect(c.highWaterFor('annual-meeting')).toBe(maxSeq);
  });

  test('resetHighWater replays a thread from scratch', async () => {
    // Regression: switching parent → branch → parent rendered an empty
    // feed because /sync?since=<high-water> returned zero new entries
    // even though the UI had cleared its in-memory entries. The fix
    // pairs entry-clearing with cursor-clearing.
    const { fetch } = mockHub();
    const c = new Client({
      hubUrl: HUB, privateKey: alice.priv, publicKey: alice.pub, fetch,
    });
    await c.authenticate();
    const first = await c.sync('annual-meeting');
    expect(first.length).toBe(items.length);
    // Without reset: second sync is empty (delta-sync semantics).
    expect(await c.sync('annual-meeting')).toEqual([]);
    // With reset: the same entries come back, ready to render fresh.
    c.resetHighWater('annual-meeting');
    expect(c.highWaterFor('annual-meeting')).toBe(-1);
    const replay = await c.sync('annual-meeting');
    expect(replay.length).toBe(items.length);
  });

  test('throws VerificationError on tampered entry, high-water stays put', async () => {
    const { fetch } = mockHub();
    // Wrap the mock to tamper a body field on the wire — sig will no longer match.
    const tampering = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      const resp = await fetch(url, init);
      const u = new URL(url.toString());
      if (u.pathname !== '/sync') return resp;
      const body = await resp.json();
      body.entries[0].entry.body = 'TAMPERED ON THE WIRE';
      return jsonResp(body);
    });
    const c = new Client({
      hubUrl: HUB, privateKey: alice.priv, publicKey: alice.pub, fetch: tampering,
    });
    await c.authenticate();
    await expect(c.sync('annual-meeting'))
      .rejects.toBeInstanceOf(VerificationError);
    expect(c.highWaterFor('annual-meeting')).toBe(-1);
  });
});

// ---- WebSocket subscribe --------------------------------------------

describe('subscribe', () => {
  class FakeWebSocket {
    static instances: FakeWebSocket[] = [];
    onmessage: ((e: MessageEvent) => void) | null = null;
    onerror: ((e: Event) => void) | null = null;
    onopen: ((e: Event) => void) | null = null;
    onclose: ((e: Event) => void) | null = null;
    url: string;
    closed = false;
    constructor(url: string | URL) {
      this.url = url.toString();
      FakeWebSocket.instances.push(this);
    }
    close() {
      this.closed = true;
    }
    deliver(payload: unknown) {
      this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
    }
  }
  beforeEach(() => {
    FakeWebSocket.instances = [];
  });

  test('verifies pushed entries through the §5 chain and forwards them', async () => {
    const { fetch } = mockHub();
    const c = new Client({
      hubUrl: HUB, privateKey: alice.priv, publicKey: alice.pub,
      fetch, WebSocket: FakeWebSocket as unknown as typeof WebSocket,
    });
    await c.authenticate();
    await c.fetchDirectory();
    await c.fetchSth();

    const received: string[] = [];
    const teardown = c.subscribe('annual-meeting', (ve) => {
      received.push(ve.entry.id!);
    });
    const ws = FakeWebSocket.instances.at(-1)!;
    expect(ws.url).toContain('ws://');
    expect(ws.url).toContain('token=');

    // Deliver the first fixture entry over the WS — should arrive verified.
    ws.deliver({
      type: 'entry',
      entry: items[0].entry,
      seq: items[0].seq,
    });
    // The verify path calls fetch for /proof/inclusion — yield to it.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(received).toEqual([items[0].entry.id]);
    teardown();
    expect(ws.closed).toBe(true);
  });

  test('fetches a fresh STH for every pushed entry (no stale cache)', async () => {
    // Regression: lastSth used to be the fallback in Client.verify when
    // sthArg wasn't passed. That caused 'inclusion proof failed under sth
    // size=N' on every post-sync push because the tree had grown past N by
    // the time the push arrived. The fix: drop the cache from the verify
    // fallback chain; push gets its own fresh fetch per entry.
    const { fetch } = mockHub();
    const c = new Client({
      hubUrl: HUB, privateKey: alice.priv, publicKey: alice.pub,
      fetch, WebSocket: FakeWebSocket as unknown as typeof WebSocket,
    });
    await c.authenticate();
    await c.fetchDirectory();
    await c.fetchSth();
    // Count /sth calls observed so far — the initial fetchSth above.
    const sthCallsBefore = fetch.mock.calls.filter(
      ([url]) => new URL(url.toString()).pathname === '/sth',
    ).length;

    c.subscribe('annual-meeting', () => {});
    const ws = FakeWebSocket.instances.at(-1)!;
    ws.deliver({ type: 'entry', entry: items[0].entry, seq: items[0].seq });
    await new Promise((resolve) => setTimeout(resolve, 0));

    const sthCallsAfter = fetch.mock.calls.filter(
      ([url]) => new URL(url.toString()).pathname === '/sth',
    ).length;
    // The push verify MUST have triggered a fresh /sth fetch. If this
    // assertion regresses, the stale-cache bug is back.
    expect(sthCallsAfter).toBeGreaterThan(sthCallsBefore);
  });

  test('drops pushed entries from other threads silently', async () => {
    const { fetch } = mockHub();
    const c = new Client({
      hubUrl: HUB, privateKey: alice.priv, publicKey: alice.pub,
      fetch, WebSocket: FakeWebSocket as unknown as typeof WebSocket,
    });
    await c.authenticate();
    await c.fetchDirectory();
    await c.fetchSth();
    const received: string[] = [];
    c.subscribe('another-thread', (ve) => received.push(ve.entry.id!));
    const ws = FakeWebSocket.instances.at(-1)!;
    ws.deliver({ type: 'entry', entry: items[0].entry, seq: items[0].seq });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(received).toEqual([]);
  });

  test('v0.4.18: directory_changed push triggers a /directory refetch', async () => {
    // The hub broadcasts directory_changed on /admin/attest + /admin/revoke
    // so connected clients refresh their directoryView without waiting for
    // an entry-driven retry-on-miss. Verify the client honors that contract.
    const { fetch } = mockHub();
    const c = new Client({
      hubUrl: HUB, privateKey: alice.priv, publicKey: alice.pub,
      fetch, WebSocket: FakeWebSocket as unknown as typeof WebSocket,
    });
    await c.authenticate();
    await c.fetchDirectory();
    const dirCallsBefore = fetch.mock.calls.filter(
      ([url]) => new URL(url.toString()).pathname === '/directory',
    ).length;

    c.subscribe('annual-meeting', () => {});
    const ws = FakeWebSocket.instances.at(-1)!;
    ws.deliver({
      type: 'directory_changed',
      manifest_hash: 'sha256:' + 'b'.repeat(64),
    });
    // The handler calls fetchDirectory which roundtrips through fetchImpl.
    await new Promise((resolve) => setTimeout(resolve, 0));

    const dirCallsAfter = fetch.mock.calls.filter(
      ([url]) => new URL(url.toString()).pathname === '/directory',
    ).length;
    expect(dirCallsAfter).toBeGreaterThan(dirCallsBefore);
  });
});

// ---- post + receipt -------------------------------------------------

// ---- Signer abstraction (slice 3) -----------------------------------

describe('Signer abstraction', () => {
  test('Client routes auth + entry signing through the provided Signer', async () => {
    // A spy signer wraps the in-JS signer so we can assert call counts +
    // ensure the canonical bytes that flow to sign() are what verifyEntry
    // accepts on the way back.
    const inner = new InJSSigner(alice.priv);
    const calls: Array<{ bytes: Uint8Array }> = [];
    const spy: Signer = {
      async sign(message) {
        calls.push({ bytes: message });
        return inner.sign(message);
      },
    };
    const fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      const u = new URL(url.toString());
      if (u.pathname === '/auth/challenge') {
        return jsonResp({ nonce: 'd'.repeat(64), expires_at: 9_999_999_999 });
      }
      if (u.pathname === '/auth/verify') {
        return jsonResp({ token: 'a'.repeat(64), pubkey: alice.pub, expires_at: 9_999_999_999 });
      }
      if (u.pathname === '/entries') {
        const body = JSON.parse(init!.body as string);
        return jsonResp({ id: body.id, seq: 0 });
      }
      return jsonResp({ error: 'not_found' }, 404);
    });
    const c = new Client({
      hubUrl: HUB, publicKey: alice.pub, signer: spy, fetch: fetchMock,
    });
    await c.authenticate();
    expect(calls.length).toBe(1);   // signed the auth nonce
    await c.post({
      thread: 't1', author: alice.pub, kind: 'post',
      created_at: '2026-06-15T18:00:00Z',
      parents: [], body: 'via signer', blobs: [], supersedes: null, receipt: null, branch_thread: null,
      id: null, sig: null,
    });
    expect(calls.length).toBe(2);   // signed the entry's canonical content
  });
});

describe('post', () => {
  test('signs an unsigned entry and the wire form matches what verifyEntry accepts', async () => {
    // For this test we only mock the POST /entries call and capture its body.
    const captured: { body?: any } = {};
    const fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      const u = new URL(url.toString());
      if (u.pathname === '/auth/challenge') {
        return jsonResp({ nonce: 'd'.repeat(64), expires_at: 9_999_999_999 });
      }
      if (u.pathname === '/auth/verify') {
        return jsonResp({ token: 'a'.repeat(64), pubkey: alice.pub, expires_at: 9_999_999_999 });
      }
      if (u.pathname === '/entries') {
        captured.body = JSON.parse(init!.body as string);
        return jsonResp({ id: captured.body.id, seq: 0 });
      }
      return jsonResp({ error: 'not_found' }, 404);
    });
    const c = new Client({
      hubUrl: HUB, privateKey: alice.priv, publicKey: alice.pub, fetch: fetchMock,
    });
    await c.authenticate();

    const ev: Entry = {
      thread: 't1', author: alice.pub, kind: 'post',
      created_at: '2026-06-15T18:00:00Z',
      parents: [], body: 'hi', blobs: [], supersedes: null, receipt: null, branch_thread: null,
      id: null, sig: null,
    };
    const seq = await c.post(ev);
    expect(seq).toBe(0);
    expect(captured.body.id).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(captured.body.sig).toMatch(/^[0-9a-f]{128}$/);

    // Round-trip through verifyEntry — this is the wire contract.
    const { verifyEntry } = await import('./verify');
    expect(verifyEntry(captured.body)).toBe(true);
  });
});

// ---- v0.4.17: transparent session refresh ---------------------------
//
// The hub issues short-lived (1h) session tokens. The Client must
// re-authenticate on its own when (a) the local pre-emptive timer fires
// near expiry or (b) an authed request comes back 401 (host slept past
// the timer, hub restarted, etc). Without this, every-morning users saw
// "⚠ not authenticated; call authenticate() first" on wake.

/** Mock hub that issues incrementing session tokens. Pass an `expiresAt`
 *  shape so individual tests can stage near-expiry / pre-expired runs.
 *  Only the latest issued token is accepted on authed paths — older
 *  tokens get 401 (which is what an expired session looks like). */
function mockHubWithRefresh(opts: {
  expiresAtSequence: number[];   // expires_at returned by each /auth/verify
}) {
  let issueIdx = 0;
  let activeToken: string | null = null;
  const issued: string[] = [];
  const fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const u = new URL(url.toString());
    const method = (init?.method ?? 'GET').toUpperCase();
    const authHdr = (init?.headers as Record<string, string> | undefined)?.authorization;

    if (u.pathname === '/auth/challenge' && method === 'POST') {
      return jsonResp({ nonce: 'd'.repeat(64), expires_at: 9_999_999_999 });
    }
    if (u.pathname === '/auth/verify' && method === 'POST') {
      const token = `tok-${issueIdx}-${'x'.repeat(60)}`.slice(0, 64);
      const expiresAt = opts.expiresAtSequence[issueIdx]
        ?? opts.expiresAtSequence[opts.expiresAtSequence.length - 1];
      issued.push(token);
      activeToken = token;
      issueIdx += 1;
      return jsonResp({ token, pubkey: alice.pub, expires_at: expiresAt });
    }
    const bearer = authHdr?.startsWith('Bearer ') ? authHdr.slice(7) : null;
    if (bearer === null || bearer !== activeToken) {
      return jsonResp({ error: 'auth_required', reason: 'invalid or expired token' }, 401);
    }
    if (u.pathname === '/threads') return jsonResp({ threads: [] });
    return jsonResp({ error: 'not_found' }, 404);
  });
  return { fetch, issued: () => issued.slice() };
}

describe('session refresh', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  test('replays an authed request after a 401, using a fresh token', async () => {
    const farFuture = Math.floor(Date.now() / 1000) + 3600;
    const hub = mockHubWithRefresh({ expiresAtSequence: [farFuture, farFuture] });
    const c = new Client({
      hubUrl: HUB, privateKey: alice.priv, publicKey: alice.pub, fetch: hub.fetch,
    });
    await c.authenticate();
    const firstToken = hub.issued()[0];
    // Stale the active token server-side without the client knowing — the
    // hub now thinks our session is gone, but the client still has it.
    // Simulated by clearing the hub's record of the issued token.
    hub.fetch.mockImplementationOnce(async (url, init) => {
      // The very next call gets a 401, exactly as if our session expired.
      const u = new URL(url.toString());
      const authHdr = (init?.headers as Record<string, string> | undefined)?.authorization;
      if (u.pathname === '/threads' && authHdr === `Bearer ${firstToken}`) {
        return jsonResp({ error: 'auth_required', reason: 'expired' }, 401);
      }
      // Fall back to default mock for anything else.
      return hub.fetch.getMockImplementation()!(url, init);
    });

    const threads = await c.fetchThreads();
    expect(threads).toEqual([]);
    // Two tokens issued: the original, plus the silent refresh.
    expect(hub.issued()).toHaveLength(2);
  });

  test('fires onSessionRefreshed only on refresh, not on initial authenticate', async () => {
    const farFuture = Math.floor(Date.now() / 1000) + 3600;
    const hub = mockHubWithRefresh({ expiresAtSequence: [farFuture, farFuture] });
    const onSessionRefreshed = vi.fn();
    const c = new Client({
      hubUrl: HUB, privateKey: alice.priv, publicKey: alice.pub,
      fetch: hub.fetch, onSessionRefreshed,
    });
    await c.authenticate();
    // Initial authenticate must NOT fire the callback — AppState already
    // knows about the first token through the connect() return path.
    expect(onSessionRefreshed).not.toHaveBeenCalled();

    // Force a refresh via 401 retry.
    const firstToken = hub.issued()[0];
    hub.fetch.mockImplementationOnce(async (url, init) => {
      const u = new URL(url.toString());
      const authHdr = (init?.headers as Record<string, string> | undefined)?.authorization;
      if (u.pathname === '/threads' && authHdr === `Bearer ${firstToken}`) {
        return jsonResp({ error: 'expired' }, 401);
      }
      return hub.fetch.getMockImplementation()!(url, init);
    });
    await c.fetchThreads();
    expect(onSessionRefreshed).toHaveBeenCalledTimes(1);
    const [token, expiresAt] = onSessionRefreshed.mock.calls[0]!;
    expect(token).toBe(hub.issued()[1]);
    expect(expiresAt).toBe(farFuture);
  });

  test('pre-emptive timer refreshes ~60s before expiry', async () => {
    vi.useFakeTimers();
    const now = 1_700_000_000;
    vi.setSystemTime(now * 1000);
    // Session expires in 120s — timer should fire at expiresAt - 60s,
    // i.e. 60s from now.
    const hub = mockHubWithRefresh({
      expiresAtSequence: [now + 120, now + 3600],
    });
    const onSessionRefreshed = vi.fn();
    const c = new Client({
      hubUrl: HUB, privateKey: alice.priv, publicKey: alice.pub,
      fetch: hub.fetch, onSessionRefreshed,
    });
    await c.authenticate();
    expect(hub.issued()).toHaveLength(1);
    // Advance just shy of refresh.
    await vi.advanceTimersByTimeAsync(59_000);
    expect(onSessionRefreshed).not.toHaveBeenCalled();
    // Cross the threshold; the refresh fires.
    await vi.advanceTimersByTimeAsync(2_000);
    expect(hub.issued()).toHaveLength(2);
    expect(onSessionRefreshed).toHaveBeenCalledTimes(1);
  });

  test('concurrent 401s share a single refresh (no duplicate re-auths)', async () => {
    const farFuture = Math.floor(Date.now() / 1000) + 3600;
    const hub = mockHubWithRefresh({ expiresAtSequence: [farFuture, farFuture] });
    const c = new Client({
      hubUrl: HUB, privateKey: alice.priv, publicKey: alice.pub, fetch: hub.fetch,
    });
    await c.authenticate();
    const firstToken = hub.issued()[0];
    // Make every call carrying the first token 401 once, like an expired
    // session would. The retry with the second token must succeed.
    const original = hub.fetch.getMockImplementation()!;
    hub.fetch.mockImplementation(async (url, init) => {
      const u = new URL(url.toString());
      const authHdr = (init?.headers as Record<string, string> | undefined)?.authorization;
      if (u.pathname === '/threads' && authHdr === `Bearer ${firstToken}`) {
        return jsonResp({ error: 'expired' }, 401);
      }
      return original(url, init);
    });
    const results = await Promise.all([
      c.fetchThreads(), c.fetchThreads(), c.fetchThreads(),
    ]);
    expect(results.every((r) => Array.isArray(r))).toBe(true);
    // Exactly two tokens ever issued — the initial + ONE shared refresh.
    expect(hub.issued()).toHaveLength(2);
  });

  test('dispose() cancels the pre-emptive refresh timer', async () => {
    vi.useFakeTimers();
    const now = 1_700_000_000;
    vi.setSystemTime(now * 1000);
    const hub = mockHubWithRefresh({ expiresAtSequence: [now + 120, now + 3600] });
    const onSessionRefreshed = vi.fn();
    const c = new Client({
      hubUrl: HUB, privateKey: alice.priv, publicKey: alice.pub,
      fetch: hub.fetch, onSessionRefreshed,
    });
    await c.authenticate();
    c.dispose();
    await vi.advanceTimersByTimeAsync(120_000);
    // No background refresh after the user disconnected.
    expect(hub.issued()).toHaveLength(1);
    expect(onSessionRefreshed).not.toHaveBeenCalled();
  });
});
