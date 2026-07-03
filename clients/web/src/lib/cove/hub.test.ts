/**
 * HubConnection unit tests — Phase 1 (v0.4.68) smokes.
 *
 * Cover the four properties that make the extraction safe:
 *   1. Fresh HubConnection has the expected default state.
 *   2. dispose() clears reactive state + disposes the underlying Client.
 *   3. Two HubConnection instances do NOT share state — the shadow-copy
 *      shape (hubUrl/sessionToken/client) is fully per-instance, ready
 *      for the Phase 2 hubs Map without cross-talk.
 *   4. Nested-field writes on newThreadDialog land and read back —
 *      validates the delegator-getter approach for `app.newThreadDialog
 *      .name = 'x'` from ThreadList.svelte:144.
 *
 * We don't drive authenticate() end-to-end here — that's covered by
 * client.test.ts (which is unchanged after this refactor). This file
 * pins the extraction shape.
 */
import { describe, expect, test, vi } from 'vitest';
import { HubConnection } from './hub.svelte';
import type { AppState } from './state.svelte';

/** Minimal AppState stub — HubConnection only reaches back for these
 *  fields (see hub.svelte.ts constructor + methods). */
function mockApp(overrides: Partial<AppState> = {}): AppState {
  return {
    route: 'inbox',
    inTauri: false,
    inPWA: false,
    rootKeysPresent: false,
    refreshRootKeychain: vi.fn(),
    ...overrides,
  } as unknown as AppState;
}

describe('HubConnection', () => {
  test('fresh instance has expected default state', () => {
    const hub = new HubConnection(mockApp());
    expect(hub.authStatus.kind).toBe('unauthenticated');
    expect(hub.thread).toBe('annual-meeting');
    expect(hub.threadStatus.kind).toBe('idle');
    expect(hub.entries).toEqual([]);
    expect(hub.inboxRows).toEqual([]);
    expect(hub.threads).toEqual([]);
    expect(hub.members).toEqual([]);
    expect(hub.revoked).toEqual([]);
    expect(hub.myAttestation).toBeNull();
    expect(hub.manifest).toBeNull();
    expect(hub.client).toBeNull();
    expect(hub.replyOpen).toBeNull();
    expect(hub.view).toBe('messages');
    expect(hub.pendingQueue).toEqual([]);
    expect(hub.newThreadDialog).toBeNull();
  });

  test('dispose() clears reactive state + client', () => {
    const hub = new HubConnection(mockApp());
    // Seed some state as if we'd been mid-session.
    hub.entries = [{ entry: { id: 'sha256:aa' }, seq: 0 } as any];
    hub.threadStatus = { kind: 'error', message: 'boom' };
    hub.replyOpen = { entry: { id: 'sha256:bb' } } as any;
    const disposed = vi.fn();
    hub.client = { dispose: disposed } as any;
    hub.dispose();
    expect(hub.entries).toEqual([]);
    expect(hub.threadStatus.kind).toBe('idle');
    expect(hub.replyOpen).toBeNull();
    expect(hub.authStatus.kind).toBe('unauthenticated');
    expect(hub.client).toBeNull();
    expect(disposed).toHaveBeenCalledOnce();
  });

  test('two instances do not share state (Phase 2 isolation)', () => {
    const app = mockApp();
    const a = new HubConnection(app);
    const b = new HubConnection(app);

    a.thread = 'a-thread';
    a.entries = [{ entry: { id: 'sha256:aa' }, seq: 0 } as any];
    a.authStatus = { kind: 'authenticated', pubkey: 'aaa' };

    // b is unaffected.
    expect(b.thread).toBe('annual-meeting');
    expect(b.entries).toEqual([]);
    expect(b.authStatus.kind).toBe('unauthenticated');

    b.thread = 'b-thread';
    b.authStatus = { kind: 'authenticated', pubkey: 'bbb' };

    // a still has its own values.
    expect(a.thread).toBe('a-thread');
    expect(a.authStatus.kind).toBe('authenticated');
    expect(a.authStatus.kind === 'authenticated'
      && a.authStatus.pubkey).toBe('aaa');
  });

  test('openNewThreadDialog + nested field write round-trip', () => {
    const hub = new HubConnection(mockApp());
    hub.openNewThreadDialog();
    expect(hub.newThreadDialog).not.toBeNull();
    expect(hub.newThreadDialog!.name).toBe('');

    // Nested-field write — the pattern used at ThreadList.svelte:144.
    // Validates that mutations to the returned $state object persist.
    hub.newThreadDialog!.name = 'test-me';
    expect(hub.newThreadDialog!.name).toBe('test-me');

    hub.closeNewThreadDialog();
    expect(hub.newThreadDialog).toBeNull();
  });

  test('openReplyPanel + closeReplyPanel', () => {
    const hub = new HubConnection(mockApp());
    const ve = { entry: { id: 'sha256:cc' } } as any;
    hub.openReplyPanel(ve);
    expect(hub.replyOpen).toBe(ve);
    hub.closeReplyPanel();
    expect(hub.replyOpen).toBeNull();
  });

  test('setView writes view + toggles app.route to thread', () => {
    const app = mockApp({ route: 'inbox' });
    const hub = new HubConnection(app);
    hub.setView('admin');
    expect(hub.view).toBe('admin');
    expect(app.route).toBe('thread');
  });

  test('isBoardMember reflects hasCapability("admin")', () => {
    const hub = new HubConnection(mockApp());
    // No manifest, no myAttestation → no caps.
    expect(hub.isBoardMember).toBe(false);
    hub.myAttestation = { role: 'board' } as any;
    // Falls back to DEFAULT_CAPABILITIES_BY_ROLE (board → admin).
    expect(hub.isBoardMember).toBe(true);
    hub.myAttestation = { role: 'member' } as any;
    expect(hub.isBoardMember).toBe(false);
  });

  test('two-hub switchThread isolation', async () => {
    // Phase 2 regression cover: switchThread on one HubConnection must
    // not disturb another's thread/entries. Uses mock clients whose
    // sync() is a no-op so we can drive switchThread without a network.
    const app = mockApp({ route: 'inbox' });
    const a = new HubConnection(app, 'https://a.example');
    const b = new HubConnection(app, 'https://b.example');
    // Minimal client stub — switchThread calls sync/resetHighWater on it.
    const stubClient = () => ({
      sync: vi.fn().mockResolvedValue([]),
      resetHighWater: vi.fn(),
      dispose: vi.fn(),
      latestSth: vi.fn().mockReturnValue(null),
      fetchSth: vi.fn().mockResolvedValue({}),
      postReceipt: vi.fn().mockResolvedValue(0),
    });
    a.client = stubClient() as any;
    b.client = stubClient() as any;
    a.thread = 'a-thread-initial';
    b.thread = 'b-thread-initial';
    a.entries = [{ entry: { id: 'sha256:aa', thread: 'a-thread-initial' }, seq: 0 } as any];

    await a.switchThread('a-thread-2');

    expect(a.thread).toBe('a-thread-2');
    expect(a.entries).toEqual([]);
    // b is untouched.
    expect(b.thread).toBe('b-thread-initial');
  });
});
