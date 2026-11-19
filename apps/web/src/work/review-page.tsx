import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import type {
  AuthProfile,
  Citation,
  ObligationDeadline,
  ObligationOwner,
  ReviewObligation,
  ReviewOptions,
  ReviewOverride,
} from '../../../../contracts/core-api.v1.js';
import { ApiError } from '../api.js';
import { useAuth } from '../auth.js';
import { CitationChips, CitationPanel } from '../search/citation-panel.js';
import { EmptyState, ErrorState, LoadingState } from '../states.js';
import { RejectionForm } from './rejection-form.js';
import './work.css';

type Change =
  | { action: 'override'; fields: ReviewOverride }
  | { action: 'approve' }
  | { action: 'reject'; reason: string };
type ReviewList = { obligations: ReviewObligation[] };
const evidenceLabels = {
  quote_exact: 'Exact quote',
  quote_fuzzy: 'Fuzzy quote',
  owner_resolved: 'Resolved owner',
  deadline_resolved: 'Resolved deadline',
  recognized_cue: 'Recognized cue',
  cross_chunk_agreement: 'Cross-chunk agreement',
} satisfies Record<keyof NonNullable<ReviewObligation['confidence_breakdown']>, string>;

function ownerLabel(owner: ObligationOwner) {
  return (
    [owner.resolved_department, owner.resolved_role].filter(Boolean).join(' · ') ||
    owner.raw_text ||
    '—'
  );
}

function deadlineLabel(deadline: ObligationDeadline) {
  return deadline.normalized_date || deadline.raw_text || '—';
}

function sameObligation(left: ReviewObligation, right: ReviewObligation) {
  return left.doc_id === right.doc_id && left.id === right.id;
}

function optimistic(
  item: ReviewObligation,
  change: Change,
  options: ReviewOptions | undefined,
): ReviewObligation {
  if (change.action !== 'override')
    return { ...item, status: change.action === 'approve' ? 'verified' : 'rejected' };
  const { owner, due_date } = change.fields;
  const originalOwner = item.extracted_owner ?? item.owner;
  const originalDeadline = item.extracted_deadline ?? item.deadline;
  const deadline =
    due_date === undefined
      ? item.deadline
      : {
          ...originalDeadline,
          normalized_date: due_date,
          resolved: due_date !== null,
          is_relative: false,
          anchor_date: null,
        };
  return {
    ...item,
    extracted_owner: originalOwner,
    extracted_deadline: originalDeadline,
    owner: owner
      ? {
          raw_text: originalOwner.raw_text,
          resolved_department:
            owner.department ??
            options?.roles.find((role) => role.name === owner.role)?.department ??
            null,
          resolved_role: owner.role ?? null,
          resolved: true,
        }
      : item.owner,
    deadline,
    ...(due_date === undefined ? {} : { deadline_override: deadline }),
  };
}

function ReviewRow({
  item,
  options,
  locked,
  pending,
  change,
  open,
}: {
  item: ReviewObligation;
  options: ReviewOptions | undefined;
  locked: boolean;
  pending: boolean;
  change: (change: Change) => void;
  open: (citation: Citation) => void;
}) {
  const [department, setDepartment] = useState(item.owner.resolved_department ?? '');
  const [role, setRole] = useState(item.owner.resolved_role ?? '');
  const [dueDate, setDueDate] = useState(item.deadline.normalized_date ?? '');
  const [rejecting, setRejecting] = useState(false);
  const ownerChanged =
    department !== (item.owner.resolved_department ?? '') ||
    role !== (item.owner.resolved_role ?? '');
  const dateChanged = dueDate !== (item.deadline.normalized_date ?? '');
  const edited = ownerChanged || dateChanged;
  const validOwner = !ownerChanged || !!(department || role);
  const originalOwner = item.extracted_owner ?? item.owner;
  const originalDeadline = item.extracted_deadline ?? item.deadline;

  return (
    <tr>
      <th scope="row">
        <span className="work-obligation">{item.action}</span>
        {item.citations.length > 0 && <CitationChips citations={item.citations} open={open} />}
      </th>
      <td>
        <strong>{Math.round(item.confidence * 100)}%</strong>
        {item.confidence_breakdown ? (
          <dl className="work-evidence">
            {Object.entries(evidenceLabels).map(([signal, label]) => (
              <div key={signal}>
                <dt>{label}</dt>
                <dd>
                  {Math.round(
                    100 * (item.confidence_breakdown?.[signal as keyof typeof evidenceLabels] ?? 0),
                  )}{' '}
                  pp
                </dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="work-muted">Breakdown unavailable</p>
        )}
      </td>
      <td className="work-review-field">
        <p className="work-muted">
          <strong>Extracted owner</strong>
        </p>
        <p>{originalOwner.raw_text || ownerLabel(originalOwner)}</p>
        {originalOwner.raw_text && ownerLabel(originalOwner) !== originalOwner.raw_text && (
          <p className="work-muted">{ownerLabel(originalOwner)}</p>
        )}
        <p className="work-muted">Current · {ownerLabel(item.owner)}</p>
        <label className="field">
          Owner department
          <select
            value={department}
            disabled={locked || !options}
            onChange={(event) => {
              const value = event.target.value;
              setDepartment(value);
              const roleDepartment = options?.roles.find(
                (entry) => entry.name === role,
              )?.department;
              if (roleDepartment && roleDepartment !== value) setRole('');
            }}
          >
            <option value="">No department</option>
            {options?.departments.map((name) => (
              <option key={name}>{name}</option>
            ))}
          </select>
        </label>
        <label className="field">
          Owner role
          <select
            value={role}
            disabled={locked || !options}
            onChange={(event) => {
              const value = event.target.value;
              setRole(value);
              const roleDepartment = options?.roles.find(
                (entry) => entry.name === value,
              )?.department;
              if (roleDepartment) setDepartment(roleDepartment);
            }}
          >
            <option value="">No role</option>
            {options?.roles.map(({ name }) => (
              <option key={name}>{name}</option>
            ))}
          </select>
        </label>
      </td>
      <td className="work-review-field">
        <p className="work-muted">
          <strong>Extracted deadline</strong>
        </p>
        <p>{originalDeadline.raw_text || deadlineLabel(originalDeadline)}</p>
        {originalDeadline.raw_text &&
          originalDeadline.normalized_date &&
          originalDeadline.normalized_date !== originalDeadline.raw_text && (
            <p className="work-muted">{originalDeadline.normalized_date}</p>
          )}
        <p className="work-muted">
          Current ·{' '}
          {item.deadline_override
            ? item.deadline.normalized_date || '—'
            : deadlineLabel(item.deadline)}
        </p>
        <label className="field">
          Due date
          <input
            type="date"
            value={dueDate}
            disabled={locked}
            onChange={(event) => setDueDate(event.target.value)}
          />
        </label>
      </td>
      <td>
        {pending && item.status !== 'needs_review' && (
          <span className="work-status" role="status">
            {item.status === 'verified' ? 'Approved' : 'Rejected'}
          </span>
        )}
        {rejecting ? (
          <RejectionForm
            pending={locked}
            onReject={(reason) => change({ action: 'reject', reason })}
            onCancel={() => setRejecting(false)}
          />
        ) : (
          <div className="work-actions">
            <button
              className="secondary-button"
              type="button"
              disabled={locked || !edited || !validOwner}
              onClick={() =>
                change({
                  action: 'override',
                  fields: {
                    ...(ownerChanged
                      ? {
                          owner: {
                            ...(department ? { department } : {}),
                            ...(role ? { role } : {}),
                          },
                        }
                      : {}),
                    ...(dateChanged ? { due_date: dueDate || null } : {}),
                  },
                })
              }
            >
              Save override
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={
                locked ||
                edited ||
                !item.owner.resolved ||
                !(item.owner.resolved_department || item.owner.resolved_role) ||
                item.citations.length === 0
              }
              onClick={() => change({ action: 'approve' })}
            >
              Approve
            </button>
            <button
              className="secondary-button work-danger"
              type="button"
              disabled={locked}
              onClick={() => setRejecting(true)}
            >
              Reject
            </button>
            {pending && <span role="status">Saving…</span>}
          </div>
        )}
      </td>
    </tr>
  );
}

export function ReviewPage() {
  const profile = useOutletContext<AuthProfile>();
  const allowed = profile.permissions.includes('obligations:review');
  const { client, sessionId } = useAuth();
  const cache = useQueryClient();
  const key = ['obligation-review', sessionId];
  const [citation, setCitation] = useState<Citation>();
  const queue = useQuery({
    queryKey: key,
    queryFn: client.reviewQueue,
    enabled: allowed,
    retry: false,
  });
  const options = useQuery({
    queryKey: ['review-options', sessionId],
    queryFn: client.reviewOptions,
    enabled: allowed,
    retry: false,
  });
  const mutation = useMutation({
    mutationFn: async ({ item, change }: { item: ReviewObligation; change: Change }) => {
      if (change.action === 'override')
        return client.overrideObligation(item.doc_id, item.id, change.fields);
      if (change.action === 'approve') return client.approveObligation(item.doc_id, item.id);
      return client.rejectObligation(item.doc_id, item.id, change.reason);
    },
    onMutate: async ({ item, change }) => {
      await cache.cancelQueries({ queryKey: key });
      const previous = cache.getQueryData<ReviewList>(key);
      if (previous)
        cache.setQueryData<ReviewList>(key, {
          obligations: previous.obligations.map((row) =>
            sameObligation(row, item) ? optimistic(row, change, options.data) : row,
          ),
        });
      return previous;
    },
    onError: (_error, _variables, previous) => {
      if (previous) cache.setQueryData(key, previous);
    },
    onSuccess: ({ obligation }, { item, change }) => {
      cache.setQueryData<ReviewList>(
        key,
        (current) =>
          current && {
            obligations:
              change.action === 'override'
                ? current.obligations.map((row) => (sameObligation(row, item) ? obligation : row))
                : current.obligations.filter((row) => !sameObligation(row, item)),
          },
      );
    },
    onSettled: async () => {
      await Promise.all([
        cache.invalidateQueries({ queryKey: key }),
        cache.invalidateQueries({ queryKey: ['task-feed', sessionId] }),
      ]);
    },
  });
  if (!allowed)
    return (
      <ErrorState
        title="Access denied"
        description="Your account does not have permission to review obligations."
      />
    );

  const rows = queue.data?.obligations ?? [];
  return (
    <div className="work-page">
      <header>
        <h1>Review queue</h1>
        <p className="page-description">Check extracted obligations before they are assigned.</p>
      </header>
      {queue.isPending && <LoadingState label="Loading obligations for review…" />}
      {queue.isError && (
        <ErrorState
          title="Review queue could not load"
          action={
            <button className="secondary-button" type="button" onClick={() => void queue.refetch()}>
              Try again
            </button>
          }
        />
      )}
      {options.isError && (
        <ErrorState
          title="Owner options could not load"
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
      {mutation.isError && (
        <ErrorState
          title="Review could not be saved"
          description={mutation.error instanceof ApiError ? mutation.error.message : 'Try again.'}
        />
      )}
      {queue.data && !rows.length && <EmptyState title="No obligations need review" />}
      {rows.length > 0 && (
        <div
          className="work-table-wrap"
          role="region"
          aria-label="Review obligations table"
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- Keyboard users can scroll a wide table.
          tabIndex={0}
        >
          <table className="work-table review-table">
            <caption className="sr-only">Obligations that need review</caption>
            <thead>
              <tr>
                <th scope="col">Obligation</th>
                <th scope="col">Confidence</th>
                <th scope="col">Owner</th>
                <th scope="col">Deadline</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((item) => (
                <ReviewRow
                  key={item.doc_id + ':' + item.id}
                  item={item}
                  options={options.data}
                  locked={mutation.isPending}
                  pending={mutation.isPending && sameObligation(mutation.variables.item, item)}
                  change={(change) => mutation.mutate({ item, change })}
                  open={setCitation}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {citation && <CitationPanel citation={citation} onClose={() => setCitation(undefined)} />}
    </div>
  );
}
