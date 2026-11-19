import type {
  AuthCredentials,
  AuthTokens,
  AuditQuery,
  CoreAdminApi,
  CoreAuthApi,
  CoreDocumentApi,
  CoreSearchApi,
  CoreTaskApi,
  CoreReviewApi,
  CoreRelationApi,
  DocumentUploadFields,
  DocumentUploadReceipt,
  SearchParameters,
  TaskTransition,
  ReviewOverride,
  RelationListStatus,
  StaleDecision,
  StoredDocumentRelation,
  UserChange,
} from '../../../contracts/core-api.v1.js';

type CoreApi = CoreAuthApi &
  CoreDocumentApi &
  CoreSearchApi &
  CoreTaskApi &
  CoreReviewApi &
  CoreRelationApi &
  CoreAdminApi;
type Path = keyof CoreApi;
type Session = {
  id: number;
  tokens: AuthTokens;
  refresh: Promise<void> | null;
};

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

function responseError(status: number, body: unknown) {
  const message =
    typeof body === 'object' && body !== null && 'error' in body && typeof body.error === 'string'
      ? body.error
      : 'The request could not be completed.';
  return new ApiError(status, message);
}

async function fail(response: Response): Promise<never> {
  throw responseError(response.status, await response.json().catch(() => null));
}

function searchQuery(input: SearchParameters, answerLanguage?: string): URLSearchParams {
  const params = new URLSearchParams({ query: input.query, top_k: String(input.top_k) });
  if (answerLanguage) params.set('language', answerLanguage);
  for (const key of [
    'department',
    'doc_type',
    'doc_date_from',
    'doc_date_to',
    'language',
  ] as const) {
    const value = input[key];
    if (value) params.set('filters[' + key + ']', value);
  }
  return params;
}

export function createClient(baseUrl: string, fetcher: typeof fetch = fetch) {
  // Core delivers refresh tokens in JSON. Keep both tokens in this closure only:
  // a reload requires login, and no refresh token is exposed in browser storage.
  let current: Session | null = null;
  let nextId = 0;
  let loginAttempt = 0;
  const listeners = new Set<() => void>();

  function publish(session: Session | null) {
    current = session;
    listeners.forEach((listener) => listener());
  }

  function send<P extends Path>(
    path: P,
    method: CoreApi[P]['method'],
    body: CoreApi[P]['body'],
    accessToken?: string,
    id?: string,
    query?: URLSearchParams,
    obligation?: string,
  ) {
    const headers = new Headers({ Accept: 'application/json' });
    if (body !== undefined && !(body instanceof FormData)) {
      headers.set('Content-Type', 'application/json');
    }
    if (accessToken) headers.set('Authorization', 'Bearer ' + accessToken);
    let endpoint = id === undefined ? path : path.replace(':id', encodeURIComponent(id));
    if (obligation !== undefined)
      endpoint = endpoint.replace(':obligation_id', encodeURIComponent(obligation));
    return fetcher(baseUrl.replace(/\/+$/, '') + endpoint + (query ? '?' + query.toString() : ''), {
      method,
      headers,
      credentials: 'omit',
      ...(body === undefined
        ? {}
        : { body: body instanceof FormData ? body : JSON.stringify(body) }),
    });
  }

  async function decode<P extends Path>(response: Response): Promise<CoreApi[P]['result']> {
    if (!response.ok) await fail(response);
    return (await response.json()) as CoreApi[P]['result'];
  }

  function sessionEnded(): ApiError {
    return new ApiError(401, 'Sign in to continue.');
  }

  async function refresh(session: Session, rejectedToken: string) {
    if (session.tokens.access_token !== rejectedToken) return;
    session.refresh ??= (async () => {
      try {
        const response = await send('/api/auth/refresh', 'POST', {
          refresh_token: session.tokens.refresh_token,
        });
        session.tokens = await decode<'/api/auth/refresh'>(response);
      } catch (error) {
        if (current === session && error instanceof ApiError && error.status === 401) {
          publish(null);
        }
        throw error;
      }
    })().finally(() => {
      session.refresh = null;
    });
    await session.refresh;
  }

  async function authorized<P extends Path>(
    path: P,
    method: CoreApi[P]['method'],
    body: CoreApi[P]['body'],
    id?: string,
    query?: URLSearchParams,
    obligation?: string,
  ) {
    const session = current;
    if (!session) throw sessionEnded();
    const token = session.tokens.access_token;
    let response = await send(path, method, body, token, id, query, obligation);
    if (current !== session) throw sessionEnded();
    if (response.status === 401) {
      await refresh(session, token);
      if (current !== session) throw sessionEnded();
      response = await send(path, method, body, session.tokens.access_token, id, query, obligation);
    }
    if (current !== session) throw sessionEnded();
    if (response.status === 401) publish(null);
    return response;
  }

  return {
    subscribe(this: void, listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    sessionId: () => current?.id ?? 0,

    async login(credentials: AuthCredentials): Promise<void> {
      const attempt = ++loginAttempt;
      const tokens = await decode<'/api/auth/login'>(
        await send('/api/auth/login', 'POST', credentials),
      );
      if (attempt === loginAttempt) publish({ id: ++nextId, tokens, refresh: null });
    },

    async profile(this: void) {
      return decode<'/api/auth/me'>(await authorized('/api/auth/me', 'GET', undefined));
    },

    async uploadOptions(this: void) {
      return decode<'/api/documents/upload-options'>(
        await authorized('/api/documents/upload-options', 'GET', undefined),
      );
    },

    async documents(this: void) {
      return decode<'/api/documents'>(await authorized('/api/documents', 'GET', undefined));
    },

    async document(id: string) {
      return decode<'/api/documents/:id'>(
        await authorized('/api/documents/:id', 'GET', undefined, id),
      );
    },

    async documentObligations(id: string) {
      return decode<'/api/documents/:id/obligations'>(
        await authorized('/api/documents/:id/obligations', 'GET', undefined, id),
      );
    },

    async searchOptions(this: void) {
      return decode<'/api/search/options'>(
        await authorized('/api/search/options', 'GET', undefined),
      );
    },

    async search(input: SearchParameters) {
      return decode<'/api/search'>(
        await authorized('/api/search', 'GET', undefined, undefined, searchQuery(input)),
      );
    },

    async answer(input: SearchParameters, language: string) {
      return decode<'/api/answer'>(
        await authorized('/api/answer', 'GET', undefined, undefined, searchQuery(input, language)),
      );
    },

    async sourcePdf(id: string): Promise<CoreSearchApi['/api/documents/:id/source']['result']> {
      const response = await authorized('/api/documents/:id/source', 'GET', undefined, id);
      if (!response.ok) await fail(response);
      return response.blob();
    },

    async tasks(this: void, scope?: 'stale') {
      return decode<'/api/tasks'>(
        await authorized(
          '/api/tasks',
          'GET',
          undefined,
          undefined,
          scope ? new URLSearchParams({ scope }) : undefined,
        ),
      );
    },
    async relations(status: RelationListStatus) {
      return decode<'/api/relations'>(
        await authorized(
          '/api/relations',
          'GET',
          undefined,
          undefined,
          new URLSearchParams({ status }),
        ),
      );
    },
    async relation(id: string) {
      return decode<'/api/relations/:id'>(
        await authorized('/api/relations/:id', 'GET', undefined, id),
      );
    },
    async confirmRelation(id: string) {
      return decode<'/api/relations/:id/confirm'>(
        await authorized('/api/relations/:id/confirm', 'POST', undefined, id),
      );
    },
    async dismissRelation(id: string, reason: string) {
      return decode<'/api/relations/:id/dismiss'>(
        await authorized('/api/relations/:id/dismiss', 'POST', { reason }, id),
      );
    },
    async changeRelationType(id: string, relation_type: StoredDocumentRelation['relation_type']) {
      return decode<'/api/relations/:id/type'>(
        await authorized('/api/relations/:id/type', 'PATCH', { relation_type }, id),
      );
    },
    async decideStaleness(relationId: string, taskId: number, change: StaleDecision) {
      const response = await authorized(
        '/api/relations/:id/stale-obligations/:obligation_id',
        'PATCH',
        change,
        relationId,
        undefined,
        String(taskId),
      );
      if (!response.ok) await fail(response);
    },
    async audit(filters: AuditQuery) {
      const query = new URLSearchParams();
      for (const [key, value] of Object.entries(filters)) {
        if (value !== undefined && value !== '') query.set(key, String(value));
      }
      return decode<'/api/audit'>(
        await authorized('/api/audit', 'GET', undefined, undefined, query),
      );
    },
    async users(this: void) {
      return decode<'/api/users'>(await authorized('/api/users', 'GET', undefined));
    },
    async changeUser(id: number, change: UserChange) {
      return decode<'/api/users/:id'>(
        await authorized('/api/users/:id', 'PATCH', change, String(id)),
      );
    },
    async replaceUserRoles(id: number, roles: string[]): Promise<void> {
      const response = await authorized('/api/users/:id/roles', 'PUT', { roles }, String(id));
      if (!response.ok) await fail(response);
    },
    async organisation(this: void) {
      return decode<'/api/organisation'>(await authorized('/api/organisation', 'GET', undefined));
    },
    async transitionTask(id: number, change: TaskTransition) {
      return decode<'/api/tasks/:id/status'>(
        await authorized('/api/tasks/:id/status', 'PATCH', change, String(id)),
      );
    },
    async reviewQueue(this: void) {
      return decode<'/api/obligations/review'>(
        await authorized('/api/obligations/review', 'GET', undefined),
      );
    },
    async reviewOptions(this: void) {
      return decode<'/api/obligations/review/options'>(
        await authorized('/api/obligations/review/options', 'GET', undefined),
      );
    },
    async overrideObligation(doc: string, id: string, change: ReviewOverride) {
      return decode<'/api/documents/:id/obligations/:obligation_id/review'>(
        await authorized(
          '/api/documents/:id/obligations/:obligation_id/review',
          'PATCH',
          change,
          doc,
          undefined,
          id,
        ),
      );
    },
    async approveObligation(doc: string, id: string) {
      return decode<'/api/documents/:id/obligations/:obligation_id/review/approve'>(
        await authorized(
          '/api/documents/:id/obligations/:obligation_id/review/approve',
          'POST',
          undefined,
          doc,
          undefined,
          id,
        ),
      );
    },
    async rejectObligation(doc: string, id: string, reason: string) {
      return decode<'/api/documents/:id/obligations/:obligation_id/review/reject'>(
        await authorized(
          '/api/documents/:id/obligations/:obligation_id/review/reject',
          'POST',
          { reason },
          doc,
          undefined,
          id,
        ),
      );
    },

    async retryDocument(id: string): Promise<void> {
      const response = await authorized('/api/documents/:id/index', 'POST', undefined, id);
      if (!response.ok) await fail(response);
    },

    async uploadDocument(file: File, fields: DocumentUploadFields): Promise<DocumentUploadReceipt> {
      const form = new FormData();
      form.append('file', file);
      for (const field of ['department', 'doc_type', 'doc_date'] as const) {
        const value = fields[field];
        if (value) form.append(field, value);
      }
      const response = await authorized('/v1/upload', 'POST', form);
      if (response.status === 502) {
        const body: unknown = await response.json().catch(() => null);
        if (
          typeof body === 'object' &&
          body !== null &&
          'document_id' in body &&
          typeof body.document_id === 'string'
        ) {
          return body as DocumentUploadReceipt;
        }
        throw responseError(response.status, body);
      }
      return decode<'/v1/upload'>(response);
    },

    async logout(): Promise<void> {
      loginAttempt++;
      const session = current;
      publish(null);
      if (!session) return;
      // If logout races rotation, revoke the replacement instead of the already-used token.
      await session.refresh?.catch(() => undefined);
      const response = await send('/api/auth/logout', 'POST', {
        refresh_token: session.tokens.refresh_token,
      });
      if (!response.ok) await fail(response);
    },
  };
}

export type ApiClient = ReturnType<typeof createClient>;
