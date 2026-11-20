import { useMutation, useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import type {
  Citation,
  DocumentRecord,
  DocumentSummary,
  SummaryPreview,
} from '../../../../contracts/core-api.v1.js';
import { useAuth } from '../auth.js';
import { AuthorityWarnings } from '../relations/authority.js';
import { CitationChips, CitationPanel } from '../search/citation-panel.js';
import { EmptyState, ErrorState, LoadingState } from '../states.js';
import { errorMessage } from './document-state.js';

type Point = NonNullable<DocumentSummary['summary']>['points'][number];

function Points({
  points,
  open,
  language,
}: {
  points: Point[];
  open: (citation: Citation) => void;
  language?: string;
}) {
  return (
    <ul className="for-you-points" lang={language}>
      {points.map((point, index) => {
        const suffix = point.due_date ? ' — ' + point.due_date : '';
        const action =
          suffix && point.text.endsWith(suffix) ? point.text.slice(0, -suffix.length) : point.text;
        return (
          <li key={point.obligation_id ?? index}>
            <p>{action}</p>
            {point.due_date && (
              <p className="document-muted">
                Due <time dateTime={point.due_date}>{point.due_date}</time>
              </p>
            )}
            <CitationChips citations={point.citations} open={open} />
          </li>
        );
      })}
    </ul>
  );
}

export function ForYouPanel({ document }: { document: DocumentRecord }) {
  const { client, sessionId, profile } = useAuth();
  const canPreview = profile.data?.permissions.includes('documents:summary-preview');
  const [department, setDepartment] = useState('');
  const [role, setRole] = useState('');
  const [citation, setCitation] = useState<Citation>();
  const ready = document.status === 'indexed' && document.extraction_status !== 'pending';
  const own = useQuery({
    queryKey: [
      'document-summary',
      sessionId,
      document.id,
      document.authority_status,
      document.extraction_status,
    ],
    queryFn: () => client.documentSummary(document.id),
    enabled: ready,
    retry: false,
  });
  const preview = useMutation({
    mutationFn: (selection: SummaryPreview) => client.documentSummary(document.id, selection),
  });
  const previewing = Boolean(canPreview && preview.variables);
  const data = previewing ? preview.data : own.data;
  const options = canPreview ? own.data?.preview_options : undefined;
  const warnings =
    data?.authority_warnings ?? own.data?.authority_warnings ?? document.authority_warnings ?? [];
  const summary = data?.summary;
  const assigned = summary?.points.filter((point) => point.obligation_id !== undefined) ?? [];
  const points = summary?.points.filter((point) => point.obligation_id === undefined) ?? [];
  const loading = previewing ? preview.isPending : own.isPending;
  const error = previewing ? preview.error : own.error;
  const roles = options?.roles.filter(
    (item) => !department || !item.department || item.department === department,
  );

  return (
    <section className="document-card for-you" aria-labelledby="for-you-heading">
      <div className="document-card-heading">
        <h2 id="for-you-heading">For you</h2>
      </div>
      <AuthorityWarnings warnings={warnings} document />
      {ready && options && (
        <form
          className="for-you-preview"
          aria-label="Preview another audience"
          onSubmit={(event) => {
            event.preventDefault();
            if ((!department && !role) || preview.isPending) return;
            setCitation(undefined);
            preview.mutate({ ...(department ? { department } : {}), ...(role ? { role } : {}) });
          }}
        >
          <label className="field">
            Preview department
            <select
              value={department}
              onChange={(event) => {
                const value = event.target.value;
                setDepartment(value);
                const current = options.roles.find((item) => item.name === role);
                if (value && current?.department && current.department !== value) setRole('');
              }}
            >
              <option value="">Any department</option>
              {options.departments.map((name) => (
                <option key={name}>{name}</option>
              ))}
            </select>
          </label>
          <label className="field">
            Preview role
            <select value={role} onChange={(event) => setRole(event.target.value)}>
              <option value="">Any role</option>
              {roles?.map(({ name }) => (
                <option key={name}>{name}</option>
              ))}
            </select>
          </label>
          <button
            type="submit"
            className="secondary-button"
            disabled={preview.isPending || (!department && !role)}
          >
            Preview summary
          </button>
          {previewing && (
            <button
              type="button"
              className="text-link"
              onClick={() => {
                preview.reset();
                setDepartment('');
                setRole('');
                setCitation(undefined);
              }}
            >
              Back to my summary
            </button>
          )}
        </form>
      )}
      {previewing && preview.variables && (
        <p className="document-muted">
          Audience preview ·{' '}
          {[preview.variables.department, preview.variables.role].filter(Boolean).join(' · ')}
        </p>
      )}
      {!ready && <EmptyState title="Summary is waiting for processing" />}
      {ready && loading && <LoadingState label="Loading summary…" />}
      {ready && error && (
        <ErrorState
          title="Summary could not load"
          description={errorMessage(error, 'Try again.')}
          action={
            <button
              className="secondary-button"
              type="button"
              onClick={() => {
                if (previewing && preview.variables) preview.mutate(preview.variables);
                else void own.refetch();
              }}
            >
              Try again
            </button>
          }
        />
      )}
      {ready &&
        !loading &&
        !error &&
        data &&
        (summary?.not_relevant ? (
          <EmptyState
            title="Not relevant to your role"
            {...(summary.relevance_reason === 'no_verified_audience_points'
              ? {}
              : { description: summary.relevance_reason })}
          />
        ) : !assigned.length && !points.length ? (
          <EmptyState
            title={
              warnings.length
                ? 'No current summary points for this version'
                : 'No summary available'
            }
          />
        ) : (
          <>
            {!!assigned.length && (
              <div>
                <h3>{previewing ? 'Assigned obligations' : 'Your obligations'}</h3>
                <Points points={assigned} open={setCitation} />
              </div>
            )}
            {!!points.length && (
              <Points points={points} open={setCitation} language={summary?.language ?? ''} />
            )}
          </>
        ))}
      {citation && <CitationPanel citation={citation} onClose={() => setCitation(undefined)} />}
    </section>
  );
}
