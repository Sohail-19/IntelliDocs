import { useMutation, useQuery } from '@tanstack/react-query';
import { Search, SlidersHorizontal } from 'lucide-react';
import { useState } from 'react';
import type { FormEvent } from 'react';
import { useOutletContext } from 'react-router-dom';
import type {
  Answer,
  AuthProfile,
  Citation,
  SearchParameters,
  SearchResult,
} from '../../../../contracts/core-api.v1.js';
import { ApiError } from '../api.js';
import { useAuth } from '../auth.js';
import { AuthorityWarnings } from '../relations/authority.js';
import { EmptyState, ErrorState, LoadingState } from '../states.js';
import { CitationChips, CitationPanel } from './citation-panel.js';
import './search.css';

type Mode = 'search' | 'ask';
type Result = { mode: 'search'; results: SearchResult[] } | { mode: 'ask'; answer: Answer };
type Filters = Omit<SearchParameters, 'query' | 'top_k'>;

function AnswerView({ answer, open }: { answer: Answer; open: (citation: Citation) => void }) {
  if (answer.abstained) {
    return (
      <EmptyState
        title="The answer was withheld"
        description={
          answer.abstain_reason || 'The answer could not be verified against the available sources.'
        }
      />
    );
  }
  // Render verified claims as text nodes. The composed model answer is never a display input.
  const claims = answer.claims.filter(
    (item) => item.status === 'verified' && item.citations.length > 0,
  );
  if (!claims.length) {
    return (
      <EmptyState
        title="No verified claims"
        description="There are no claims with verified citations to display."
      />
    );
  }
  return (
    <section className="search-card answer-view" aria-labelledby="answer-heading">
      <h2 id="answer-heading">Verified answer</h2>
      <ol>
        {claims.map((claim, index) => (
          <li className="answer-claim" key={index}>
            <p>{claim.text}</p>
            <CitationChips citations={claim.citations} open={open} />
          </li>
        ))}
      </ol>
    </section>
  );
}

function ResultList({
  results,
  open,
}: {
  results: SearchResult[];
  open: (citation: Citation) => void;
}) {
  if (!results.length)
    return (
      <EmptyState
        title="No matching passages"
        description="Try a different query or remove a filter."
      />
    );
  return (
    <section className="search-results" aria-labelledby="search-results-heading">
      <h2 id="search-results-heading">
        {results.length} {results.length === 1 ? 'matching passage' : 'matching passages'}
      </h2>
      <ol>
        {results.map((result) => (
          <li className="search-card search-result" key={result.chunk.chunk_id ?? result.chunk.id}>
            <div className="search-result-meta">
              <CitationChips citations={result.citations} open={open} />
              <span>
                Score {result.score.toFixed(3)}
                {result.rerank_score === undefined
                  ? ''
                  : ' · Rerank ' + result.rerank_score.toFixed(3)}
              </span>
            </div>
            <AuthorityWarnings warnings={result.authority_warnings} stale={result.stale} />
            <p>{result.chunk.text}</p>
          </li>
        ))}
      </ol>
    </section>
  );
}

export function SearchPage() {
  const profile = useOutletContext<AuthProfile>();
  const { client, sessionId } = useAuth();
  const canSearch = profile.permissions.includes('documents:search');
  const canAsk = profile.permissions.includes('documents:answer');
  const [preferredMode, setMode] = useState<Mode>('search');
  const mode = !canSearch || (preferredMode === 'ask' && canAsk) ? 'ask' : 'search';
  const [query, setQuery] = useState('');
  const [filters, setFilters] = useState<Filters>({});
  const [language, setAnswerLanguage] = useState('');
  const [selectedCitation, setCitation] = useState<Citation>();
  const options = useQuery({
    queryKey: ['search-options', sessionId],
    queryFn: client.searchOptions,
    enabled: canSearch || canAsk,
    retry: false,
  });
  const answerLanguage = language || options.data?.languages[0] || '';
  const request = useMutation({
    mutationFn: async (input: SearchParameters): Promise<Result> =>
      mode === 'ask'
        ? { mode, answer: await client.answer(input, answerLanguage) }
        : { mode, ...(await client.search(input)) },
  });
  const invalidDates =
    !!filters.doc_date_from && !!filters.doc_date_to && filters.doc_date_from > filters.doc_date_to;

  if (!canSearch && !canAsk) {
    return (
      <ErrorState
        title="Access denied"
        description="Your account does not have access to search or answers."
      />
    );
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!query.trim() || invalidDates || request.isPending || (mode === 'ask' && !answerLanguage))
      return;
    request.mutate({ query: query.trim(), top_k: 10, ...filters });
  }

  function switchMode(nextMode: Mode) {
    if (nextMode === mode) return;
    setMode(nextMode);
    request.reset();
    setCitation(undefined);
  }

  return (
    <div className="search-page">
      <div>
        <h1>Search</h1>
        <p className="page-description">
          Find passages in your documents or ask for a cited answer.
        </p>
      </div>
      <section className="search-card search-form-card" aria-label="Document search">
        {canSearch && canAsk && (
          <div className="search-mode" role="group" aria-label="Query mode">
            <button
              type="button"
              aria-pressed={mode === 'search'}
              disabled={request.isPending}
              onClick={() => switchMode('search')}
            >
              Search
            </button>
            <button
              type="button"
              aria-pressed={mode === 'ask'}
              disabled={request.isPending}
              onClick={() => switchMode('ask')}
            >
              Ask
            </button>
          </div>
        )}
        <form onSubmit={submit}>
          <label className="field search-query">
            {mode === 'ask' ? 'Question' : 'Search query'}
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              required
            />
          </label>
          <fieldset className="search-filters">
            <legend>
              <SlidersHorizontal size={16} aria-hidden="true" /> Filters
            </legend>
            {options.isPending && <p role="status">Loading organization filters…</p>}
            {options.isError && (
              <ErrorState
                title="Filters could not load"
                action={
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => void options.refetch()}
                  >
                    Try again
                  </button>
                }
              />
            )}
            <div className="search-filter-grid">
              <label className="field">
                Department
                <select
                  value={filters.department ?? ''}
                  onChange={(event) => setFilters({ ...filters, department: event.target.value })}
                >
                  <option value="">All departments</option>
                  {options.data?.departments.map((item) => (
                    <option key={item}>{item}</option>
                  ))}
                </select>
              </label>
              <label className="field">
                Document type
                <select
                  value={filters.doc_type ?? ''}
                  onChange={(event) => setFilters({ ...filters, doc_type: event.target.value })}
                >
                  <option value="">All document types</option>
                  {options.data?.document_types.map((item) => (
                    <option key={item}>{item}</option>
                  ))}
                </select>
              </label>
              <label className="field">
                Document language
                <select
                  value={filters.language ?? ''}
                  onChange={(event) => setFilters({ ...filters, language: event.target.value })}
                >
                  <option value="">All languages</option>
                  {options.data?.languages.map((item) => (
                    <option key={item}>{item}</option>
                  ))}
                </select>
              </label>
              <label className="field">
                From date
                <input
                  type="date"
                  value={filters.doc_date_from ?? ''}
                  onChange={(event) =>
                    setFilters({ ...filters, doc_date_from: event.target.value })
                  }
                />
              </label>
              <label className="field">
                To date
                <input
                  type="date"
                  value={filters.doc_date_to ?? ''}
                  onChange={(event) => setFilters({ ...filters, doc_date_to: event.target.value })}
                />
              </label>
            </div>
            {invalidDates && (
              <p className="search-validation" role="alert">
                From date must be on or before To date.
              </p>
            )}
          </fieldset>
          <div className="search-actions">
            {mode === 'ask' && (
              <label className="field">
                Answer language
                <select
                  value={answerLanguage}
                  onChange={(event) => setAnswerLanguage(event.target.value)}
                >
                  {options.data?.languages.map((item) => (
                    <option key={item}>{item}</option>
                  ))}
                </select>
              </label>
            )}
            <button
              className="primary-button"
              type="submit"
              disabled={
                !query.trim() ||
                invalidDates ||
                request.isPending ||
                (mode === 'ask' && !answerLanguage)
              }
            >
              <Search size={18} aria-hidden="true" />{' '}
              {mode === 'ask' ? 'Ask documents' : 'Search documents'}
            </button>
          </div>
        </form>
      </section>
      {request.isPending && (
        <LoadingState
          label={
            mode === 'ask'
              ? 'Checking documents and verifying the answer…'
              : 'Finding matching passages…'
          }
        />
      )}
      {request.isError && (
        <ErrorState
          title={mode === 'ask' ? 'Answer could not load' : 'Search could not load'}
          description={
            request.error instanceof ApiError
              ? request.error.message
              : 'Check your connection and try again.'
          }
        />
      )}
      {request.data?.mode === 'search' && mode === 'search' && (
        <ResultList results={request.data.results} open={setCitation} />
      )}
      {request.data?.mode === 'ask' && mode === 'ask' && (
        <>
          <AuthorityWarnings
            warnings={request.data.answer.authority_warnings}
            stale={request.data.answer.stale_sources}
          />
          <AnswerView answer={request.data.answer} open={setCitation} />
        </>
      )}
      {selectedCitation && (
        <CitationPanel
          key={
            selectedCitation.doc_id +
            ':' +
            selectedCitation.char_start +
            ':' +
            selectedCitation.char_end
          }
          citation={selectedCitation}
          onClose={() => setCitation(undefined)}
        />
      )}
    </div>
  );
}
