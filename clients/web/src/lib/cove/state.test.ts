/**
 * AppState delegation smokes — Phase 1 (v0.4.68).
 *
 * We're not testing the full AppState (that's covered by the manual
 * end-to-end smoke against LWCCOA + Brooks hubs). These tests pin the
 * behavioural invariant that consumer .svelte files depend on: every
 * per-hub `app.xxx` surface delegates through `app.hub`, and returns a
 * safe default when there's no hub.
 *
 * Constructing a real AppState in node touches the vault (IndexedDB not
 * available) — the initial `refreshVaultStatus` call errors out but is
 * caught internally by the constructor's Promise chain. That's fine
 * for our purposes; the delegation logic we're testing doesn't depend
 * on that path.
 */
import { describe, expect, test, vi } from 'vitest';
import { HubConnection } from './hub.svelte';
import { AppState } from './state.svelte';

describe('AppState delegation (Phase 1)', () => {
  test('with hub === null, per-hub getters return safe defaults', () => {
    const app = new AppState();
    expect(app.hub).toBeNull();
    expect(app.entries).toEqual([]);
    expect(app.threads).toEqual([]);
    expect(app.inboxRows).toEqual([]);
    expect(app.members).toEqual([]);
    expect(app.revoked).toEqual([]);
    expect(app.myAttestation).toBeNull();
    expect(app.manifest).toBeNull();
    expect(app.client).toBeNull();
    expect(app.replyOpen).toBeNull();
    expect(app.thread).toBe('annual-meeting');
    expect(app.view).toBe('messages');
    expect(app.threadStatus.kind).toBe('idle');
    expect(app.authStatus.kind).toBe('unauthenticated');
    expect(app.isBoardMember).toBe(false);
    expect(app.hasCapability('admin')).toBe(false);
    expect(app.newThreadDialog).toBeNull();
  });

  test('after wiring a hub, getters return hub state', () => {
    const app = new AppState();
    const hub = new HubConnection(app);
    hub.thread = 'general';
    hub.entries = [{ entry: { id: 'sha256:aa' }, seq: 0 } as any];
    hub.myAttestation = { display_name: 'Alice', role: 'board' } as any;
    app.hub = hub;

    expect(app.hub).toBe(hub);
    expect(app.thread).toBe('general');
    expect(app.entries.length).toBe(1);
    expect(app.myAttestation?.display_name).toBe('Alice');
    // isBoardMember: board falls back to default caps map → has 'admin'.
    expect(app.isBoardMember).toBe(true);
  });

  test('per-hub method calls delegate through to the hub', () => {
    const app = new AppState();
    const hub = new HubConnection(app);
    const spy = vi.spyOn(hub, 'openReplyPanel');
    app.hub = hub;

    const ve = { entry: { id: 'sha256:bb' } } as any;
    app.openReplyPanel(ve);

    expect(spy).toHaveBeenCalledWith(ve);
    expect(app.replyOpen).toBe(ve);
  });

  test('reset() disposes the hub and clears it', () => {
    const app = new AppState();
    const hub = new HubConnection(app);
    const disposeSpy = vi.spyOn(hub, 'dispose');
    app.hub = hub;

    app.reset();
    expect(disposeSpy).toHaveBeenCalledOnce();
    expect(app.hub).toBeNull();
    expect(app.entries).toEqual([]);
    expect(app.authStatus.kind).toBe('unauthenticated');
  });
});
