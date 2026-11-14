import type { components } from './openapi.generated.js';

type Schemas = components['schemas'];
export type IngestRequest = Schemas['ingest-request.v1.schema'];
export type IngestResponse = Schemas['ingest-response.v1.schema'];
export type SearchRequest = Schemas['search-request.v1.schema'];
export type SearchResponse = Schemas['search-response.v1.schema'];
export type ExtractObligationsRequest = Schemas['extract-obligations-request.v1.schema'];
export type ExtractObligationsResponse = Schemas['extract-obligations-response.v1.schema'];

export class IntelligenceClient {
  private readonly baseUrl: string;

  constructor(
    baseUrl = process.env.INTELLIGENCE_URL,
    private readonly fetchImplementation: typeof fetch = fetch,
  ) {
    if (!baseUrl) {
      throw new Error('INTELLIGENCE_URL is required');
    }
    this.baseUrl = baseUrl.replace(/\/$/, '');
  }

  async health(): Promise<void> {
    const response = await this.fetchImplementation(`${this.baseUrl}/health`);
    this.assertSuccess(response);
  }

  ingest(request: IngestRequest): Promise<IngestResponse> {
    return this.post('/v1/ingest', request);
  }

  search(request: SearchRequest): Promise<SearchResponse> {
    return this.post('/v1/search', request);
  }

  extractObligations(request: ExtractObligationsRequest): Promise<ExtractObligationsResponse> {
    return this.post('/v1/extract-obligations', request);
  }

  private async post<TRequest, TResponse>(path: string, body: TRequest): Promise<TResponse> {
    const response = await this.fetchImplementation(`${this.baseUrl}${path}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    this.assertSuccess(response);
    return (await response.json()) as TResponse;
  }

  private assertSuccess(response: Response): void {
    if (!response.ok) {
      throw new Error(`Intelligence service returned ${response.status}`);
    }
  }
}
