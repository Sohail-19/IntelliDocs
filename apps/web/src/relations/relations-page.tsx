import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useId, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import type {
  Citation,
  RelationListStatus,
  StoredDocumentRelation,
} from '../../../../contracts/core-api.v1.js';
import { useAuth } from '../auth.js';
import { errorMessage } from '../documents/document-state.js';
import { CitationChips, CitationPanel } from '../search/citation-panel.js';
import { EmptyState, ErrorState, LoadingState } from '../states.js';
import './relations.css';

const types = {
  supersedes: 'Supersedes',
  amends: 'Amends',
  withdraws: 'Withdraws',
  conflicts_with: 'Conflicts with',
  references: 'References',
} satisfies Record<StoredDocumentRelation['relation_type'], string>;

const signals = {
  explicit_reference: 'Explicit reference',
  both_quotes_verified: 'Quotes verified on both sides',
  date_order_consistent: 'Consistent date order',
  recognized_cue: 'Recognized wording',
} satisfies Record<keyof NonNullable<StoredDocumentRelation['confidence_breakdown']>, string>;

const percent = new Intl.NumberFormat(undefined, { style: 'percent', maximumFractionDigits: 1 });

type RelationChange =
  | { action: 'confirm' }
  | { action: 'dismiss'; reason: string }
  | { action: 'type'; type: StoredDocumentRelation['relation_type'] };

function Passage({
  label,
  filename,
  citations,
  open,
}: {
  label: string;
  filename: string;
  citations: Citation[];
  open: (citation: Citation) => void;
}) {
  return (
    <section className="relation-passage" aria-label={label}>
      <h3>{label}</h3>
      <p className="relation-filename">{filename}</p>
      {citations.map((citation, index) => (
        <div key={`${citation.char_start}:${index}`}>
          <blockquote>{citation.quote}</blockquote>
          <CitationChips citations={[citation]} open={open} />
        </div>
      ))}
      {citations.length === 0 && <p className="document-muted">No verified passage available.</p>}
    </section>
  );
}

function RelationCard({
  relation,
  open,
  onReview,
}: {
  relation: StoredDocumentRelation;
  open: (citation: Citation) => void;
  onReview: (relation: StoredDocumentRelation) => void;
}) {
  const { client, sessionId } = useAuth();
  const cache = useQueryClient();
  const heading = useId();
  const [draft, setDraft] = useState(relation.relation_type);
  const [dismissing, setDismissing] = useState(false);
  const [reason, setReason] = useState('');
  const breakdown = relation.confidence_breakdown;
  const hasEvidence = relation.source_citations.length > 0 && relation.target_citations.length > 0;
  const change = useMutation({
    mutationFn: (command: RelationChange) => {
      switch (command.action) {
        case 'confirm':
          return client.confirmRelation(relation.id);
        case 'dismiss':
          return client.dismissRelation(relation.id, command.reason);
        case 'type':
          return client.changeRelationType(relation.id, command.type);
      }
    },
    onSuccess: async ({ relation: updated }) => {
      cache.setQueryData(['relations', sessionId, 'detail', relation.id], updated);
      setDraft(updated.relation_type);
      setDismissing(false);
      onReview(updated);
      await Promise.all([
        cache.invalidateQueries({ queryKey: ['relations', sessionId] }),
        cache.invalidateQueries({ queryKey: ['documents', sessionId] }),
        cache.invalidateQueries({ queryKey: ['task-feed', sessionId] }),
      ]);
    },
  });

  return (
    <article className="document-card relation-card" aria-labelledby={heading}>
      <header className="relation-card-heading">
        <div>
          <h2 id={heading}>{types[relation.relation_type]}</h2>
          <p>{relation.scope === 'clauses' ? 'Specific clauses' : 'Whole document'}</p>
        </div>
        <span className="work-status">{relation.status}</span>
      </header>
      <div className="relation-passages">
        <Passage
          label="Newer document"
          filename={relation.source_filename ?? relation.source_doc_id}
          citations={relation.source_citations}
          open={open}
        />
        <Passage
          label="Older document"
          filename={relation.target_filename ?? relation.target_doc_id}
          citations={relation.target_citations}
          open={open}
        />
      </div>
      <dl className="relation-confidence">
        <div>
          <dt>Confidence</dt>
          <dd>{percent.format(relation.confidence)}</dd>
        </div>
        {breakdown &&
          (Object.keys(signals) as (keyof typeof signals)[]).map((key) => (
            <div key={key}>
              <dt>{signals[key]}</dt>
              <dd>{percent.format(breakdown[key])}</dd>
            </div>
          ))}
      </dl>
      {relation.status === 'proposed' && (
        <div className="relation-review">
          {!hasEvidence && <p>Confirmation requires a verified passage from each document.</p>}
          <form
            className="relation-type-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (relation.relation_type !== draft && !change.isPending)
                change.mutate({ action: 'type', type: draft });
            }}
          >
            <label className="field">
              Relation type
              <select
                value={draft}
                onChange={(event) => setDraft(event.target.value as typeof draft)}
                disabled={change.isPending}
              >
                {Object.entries(types).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <button
              className="secondary-button"
              type="submit"
              disabled={change.isPending || relation.relation_type === draft}
            >
              Save type
            </button>
          </form>
          <div className="work-actions">
            <button
              className="primary-button"
              type="button"
              disabled={change.isPending || !hasEvidence || draft !== relation.relation_type}
              onClick={() => change.mutate({ action: 'confirm' })}
            >
              Confirm relation
            </button>
            {!dismissing && (
              <button
                className="secondary-button"
                type="button"
                disabled={change.isPending}
                onClick={() => {
                  change.reset();
                  setDismissing(true);
                }}
              >
                Dismiss relation
              </button>
            )}
          </div>
          {dismissing && (
            <form
              className="relation-decision-form"
              onSubmit={(event) => {
                event.preventDefault();
                if (reason.trim() && !change.isPending)
                  change.mutate({ action: 'dismiss', reason: reason.trim() });
              }}
            >
              <label className="field">
                Dismissal reason
                <textarea
                  required
                  maxLength={2000}
                  rows={3}
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                />
              </label>
              <div className="work-actions">
                <button
                  className="secondary-button"
                  type="submit"
                  disabled={change.isPending || !reason.trim()}
                >
                  Dismiss with reason
                </button>
                <button
                  className="text-link"
                  type="button"
                  disabled={change.isPending}
                  onClick={() => setDismissing(false)}
                >
                  Cancel
                </button>
              </div>
            </form>
          )}
          {change.isError && (
            <p className="document-row-error" role="alert">
              {errorMessage(change.error, 'The relation could not be updated.')}
            </p>
          )}
          {change.isPending && <p role="status">Saving relation…</p>}
        </div>
      )}
      {relation.review_reason && (
        <p className="document-muted">Review reason: {relation.review_reason}</p>
      )}
    </article>
  );
}

export function RelationsPage() {
  const { id } = useParams();
  const { client, sessionId } = useAuth();
  const [status, setStatus] = useState<RelationListStatus>('proposed');
  const [notice, setNotice] = useState('');
  const [citation, setCitation] = useState<Citation>();
  const query = useQuery({
    queryKey: ['relations', sessionId, 'list', status],
    queryFn: () => client.relations(status),
    enabled: !id,
    retry: false,
  });
  const detail = useQuery({
    queryKey: ['relations', sessionId, 'detail', id],
    queryFn: async () => (await client.relation(id ?? '')).relation,
    enabled: !!id,
    retry: false,
  });
  const active = id ? detail : query;
  const items = id ? (detail.data ? [detail.data] : []) : (query.data?.relations ?? []);
  return (
    <div className="relations-page">
      {id && (
        <Link className="document-back" to="/relations">
          Back to relations
        </Link>
      )}
      <header>
        <h1>{id ? 'Document relation' : 'Relations'}</h1>
      </header>
      {!id && (
        <div className="work-tabs" role="group" aria-label="Relation status">
          {(['proposed', 'confirmed'] as const).map((item) => (
            <button
              type="button"
              key={item}
              aria-pressed={status === item}
              onClick={() => {
                setStatus(item);
                setNotice('');
              }}
            >
              {item === 'proposed' ? 'Proposed' : 'Confirmed'}
            </button>
          ))}
        </div>
      )}
      {notice && <p role="status">{notice}</p>}
      {active.isPending && <LoadingState label="Loading relations…" />}
      {active.isError && (
        <ErrorState
          title="Relations could not load"
          description={errorMessage(active.error, 'Check your connection.')}
          action={
            <button
              className="secondary-button"
              type="button"
              onClick={() => void active.refetch()}
            >
              Try again
            </button>
          }
        />
      )}
      {active.isSuccess && !items.length && (
        <EmptyState
          title={status === 'proposed' ? 'No proposed relations' : 'No confirmed relations'}
        />
      )}
      {items.map((relation) => (
        <RelationCard
          key={relation.id}
          relation={relation}
          open={setCitation}
          onReview={(updated) => {
            if (updated.status === 'confirmed' && !id) setStatus('confirmed');
            setNotice(
              updated.status === 'proposed'
                ? 'Relation type saved.'
                : updated.status === 'confirmed'
                  ? 'Relation confirmed.'
                  : 'Relation dismissed.',
            );
          }}
        />
      ))}
      {citation && <CitationPanel citation={citation} onClose={() => setCitation(undefined)} />}
    </div>
  );
}
