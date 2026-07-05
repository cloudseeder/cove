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
