import type { components, paths } from '../../../../contracts/intelligence.openapi.v1.js';
import { flag, requestContext } from '../runtime.js';

type Schemas = components['schemas'];
export type Chunk = Schemas['chunk.v1.schema'];
export type IndexRequest = Schemas['index-request.v1.schema'];
export type IndexResult = Schemas['index-result.v1.schema'];
export type IngestRequest = Omit<Schemas['ingest-request.v1.schema'], 'file'> & { file: Blob };
// The generator does not encode conditional requirements or bounded array lengths.
type GeneratedDocument = Schemas['document.v1.schema'];
type GeneratedPage = NonNullable<GeneratedDocument['pages']>[number];
type GeneratedBlock = GeneratedPage['blocks'][number];
type BlockFields = Omit<GeneratedBlock, 'type' | 'bbox' | 'rows'> & {
  bbox: [number, number, number, number];
};
export type Block = BlockFields &
  (
    | { type: 'table'; rows: string[][] }
    | { type: Exclude<GeneratedBlock['type'], 'table'>; rows?: string[][] }
  );
export type Page = Omit<GeneratedPage, 'blocks'> & { blocks: Block[] };
export type Document = Omit<GeneratedDocument, 'pages'> & { pages?: Page[] };
export type ParseResult = Omit<Schemas['parse-result.v1.schema'], 'document' | 'attachments'> & {
  document: Document & { pages: Page[] };
  attachments?: ParseResult[];
};
export type IngestResponse = Omit<Schemas['ingest-response.v1.schema'], 'parse_result'> & {
  parse_result: ParseResult;
};
export type SearchRequest = Schemas['search-request.v1.schema'];
export type SearchResponse = Schemas['search-response.v1.schema'];
export type AnswerRequest = Schemas['answer-request.v1.schema'];
export type Claim = Schemas['claim.v1.schema'];
export type Answer = Schemas['answer.v1.schema'];
export type SummaryRequest = Schemas['summary-request.v1.schema'];
export type Summary = Schemas['summary.v1.schema'];
export type ExtractObligationsRequest = Schemas['extract-obligations-request.v1.schema'];
export type Obligation = Schemas['obligation.v1.schema'];
export type ObligationOwner = Schemas['obligation-owner.v1.schema'];
export type ObligationDeadline = Schemas['obligation-deadline.v1.schema'];
export type ExtractionStats = Schemas['extraction-stats.v1.schema'];
export type ExtractionResult = Schemas['extraction-result.v1.schema'];
export type ExtractObligationsResponse = Schemas['extract-obligations-response.v1.schema'];
export type DetectRelationsRequest = Schemas['detect-relations-request.v1.schema'];
export type DocumentRelation = Schemas['document-relation.v1.schema'];
export type RelationResult = Schemas['relation-result.v1.schema'];

export class IntelligenceError extends Error {
  constructor(
    public readonly status: number,
    public readonly code?: string,
  ) {
    super(`Intelligence service returned ${status}`);
  }
}

export function llmProblem(error: unknown) {
  if (!(error instanceof IntelligenceError)) return undefined;
  if (error.status === 429 && error.code === 'llm_quota')
    return { status: 429, message: 'Daily demo LLM limit reached. Try again after midnight UTC.' };
  if (error.status === 503 && error.code === 'llm_disabled')
    return { status: 503, message: 'LLM features are disabled by the operator.' };
  return undefined;
}

export class IntelligenceClient {
  private readonly baseUrl: string;

  constructor(
    baseUrl = process.env.INTELLIGENCE_URL,
    private readonly fetchImplementation: typeof fetch = fetch,
    private readonly internalKey = process.env.INTELLIDOCS_INTERNAL_KEY ?? '',
  ) {
    if (!baseUrl) {
      throw new Error('INTELLIGENCE_URL is required');
    }
    this.baseUrl = baseUrl.replace(/\/$/, '');
  }

  async health(): Promise<void> {
    const response = await this.fetchImplementation(`${this.baseUrl}/health`);
    await this.assertSuccess(response);
  }

  async ready(): Promise<void> {
    const response = await this.fetchImplementation(
      `${this.baseUrl}/ready`,
      this.internal({
        signal: AbortSignal.timeout(3000),
        redirect: 'error',
      }),
    );
    await this.assertSuccess(response);
  }

  async purgeDemoDocument(id: string, org: string): Promise<void> {
    const response = await this.fetchImplementation(
      `${this.baseUrl}/v1/documents/${encodeURIComponent(id)}?${new URLSearchParams({ org_id: org }).toString()}`,
      this.internal({ method: 'DELETE', signal: AbortSignal.timeout(15000) }),
    );
    await this.assertSuccess(response);
  }

  async ingest(request: IngestRequest, filename = 'upload'): Promise<IngestResponse> {
    const body = new FormData();
    body.append('file', request.file, filename);
    if (request.org_id !== undefined) {
      body.append('org_id', request.org_id);
    }
    if (request.index !== undefined) body.append('index', String(request.index));
    if (request.department !== undefined) body.append('department', request.department);
    if (request.doc_type !== undefined) body.append('doc_type', request.doc_type);
    if (request.doc_date !== undefined) body.append('doc_date', request.doc_date);
    const response = await this.fetchImplementation(
      `${this.baseUrl}/v1/ingest`,
      this.internal({
        method: 'POST',
        body,
      }),
    );
    await this.assertSuccess(response);
    return (await response.json()) as IngestResponse;
  }

  index(request: IndexRequest): Promise<IndexResult> {
    return this.post('/v1/index', request);
  }

  async pdfSource(id: string, org: string): Promise<Response> {
    type SourceParameters = paths['/v1/documents/{doc_id}/source']['get']['parameters'];
    const path: SourceParameters['path'] = { doc_id: id };
    const query: SourceParameters['query'] = { org_id: org };
    const response = await this.fetchImplementation(
      this.baseUrl +
        '/v1/documents/' +
        encodeURIComponent(path.doc_id) +
        '/source?' +
        new URLSearchParams(query).toString(),
      this.internal({}),
    );
    await this.assertSuccess(response);
    return response;
  }

  search(request: SearchRequest): Promise<SearchResponse> {
    return this.post('/v1/search', request);
  }

  answer(request: AnswerRequest): Promise<Answer> {
    return this.llmPost('/v1/answer', request);
  }

  summarize(request: SummaryRequest): Promise<Summary> {
    return this.llmPost('/v1/summarize', request);
  }

  extractObligations(request: ExtractObligationsRequest): Promise<ExtractionResult> {
    return this.llmPost('/v1/extract-obligations', request);
  }

  detectRelations(request: DetectRelationsRequest): Promise<RelationResult> {
    return this.llmPost('/v1/detect-relations', request);
  }

  private async llmPost<TRequest, TResponse>(path: string, body: TRequest): Promise<TResponse> {
    if (!flag('LLM_ENABLED', true)) throw new IntelligenceError(503, 'llm_disabled');
    return this.post(path, body);
  }

  private internal(init: RequestInit): RequestInit {
    if (!this.internalKey) return init;
    const headers = new Headers(init.headers);
    headers.set('X-IntelliDocs-Internal-Key', this.internalKey);
    const context = requestContext.getStore();
    if (context?.id) headers.set('X-Request-ID', context.id);
    if (context?.actor !== undefined) headers.set('X-IntelliDocs-Actor', String(context.actor));
    return { ...init, headers, redirect: 'error' };
  }

  private async post<TRequest, TResponse>(path: string, body: TRequest): Promise<TResponse> {
    const response = await this.fetchImplementation(
      `${this.baseUrl}${path}`,
      this.internal({
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      }),
    );
    await this.assertSuccess(response);
    return (await response.json()) as TResponse;
  }

  private async assertSuccess(response: Response): Promise<void> {
    if (!response.ok) {
      let code: string | undefined;
      if ([413, 429, 503].includes(response.status)) {
        const body = (await response.json().catch(() => null)) as {
          detail?: { code?: unknown };
        } | null;
        if (
          typeof body?.detail?.code === 'string' &&
          ['llm_disabled', 'llm_quota', 'upload_pages'].includes(body.detail.code)
        )
          code = body.detail.code;
      } else await response.body?.cancel();
      throw new IntelligenceError(response.status, code);
    }
  }
}
