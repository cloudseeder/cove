/**
 * Error classes — mirror cove.client. Callers should treat each
 * distinctly:
 *   - ClientError: network surprise, malformed response, etc.
 *   - AuthenticationError: challenge-response failure or missing session.
 *   - VerificationError: a §5 chain link failed for an entry; the entry
 *     MUST NOT be displayed as legitimate. Never silently dropped — the
 *     UI surfaces the failure (broken Seal).
 */
export class ClientError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ClientError';
  }
}

export class AuthenticationError extends ClientError {
  constructor(message: string) {
    super(message);
    this.name = 'AuthenticationError';
  }
}

export class VerificationError extends ClientError {
  constructor(message: string) {
    super(message);
    this.name = 'VerificationError';
  }
}

/**
 * The hub rejected a vault PUT because `prev_vault_hash` didn't match
 * the current head. The response carries the current head hash so the
 * client can pull-merge-retry in a single round-trip.
 *
 * Thrown by Client.pushVault on a 409 stale_prev_hash response.
 */
export class StaleVaultError extends ClientError {
  readonly headHash: string;
  readonly serverPubkey: string;
  constructor(message: string, headHash: string, serverPubkey: string) {
    super(message);
    this.name = 'StaleVaultError';
    this.headHash = headHash;
    this.serverPubkey = serverPubkey;
  }
}

/**
 * v0.5.0: the hub rejected a kind='audience' write because the caller
 * isn't authorized to make the requested change under the Option-B rules
 * (server-hub-spec §3.x). `reason` is one of:
 *   - 'not_in_audience' — author isn't a current-audience member; must be
 *     added first before making any change (including as a board member).
 *   - 'removal_requires_manage_audience' — the change removes someone
 *     other than the author, and the author doesn't hold the
 *     `manage_audience` capability (defaults to board + officer).
 *
 * Thrown by HubConnection.setThreadAudience when Client.post returns a
 * 400 with error='rejected' and a recognized audience-governance reason.
 */
export class AudienceGovernanceError extends ClientError {
  readonly reason: 'not_in_audience' | 'removal_requires_manage_audience';
  constructor(reason: 'not_in_audience' | 'removal_requires_manage_audience') {
    super(`audience change rejected: ${reason}`);
    this.name = 'AudienceGovernanceError';
    this.reason = reason;
  }
}

export function isAudienceGovernanceError(e: unknown): e is AudienceGovernanceError {
  return e instanceof AudienceGovernanceError;
}
