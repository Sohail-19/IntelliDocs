import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';

import type { AuditEntry, AuditQuery, JsonValue } from '../../../../contracts/core-api.v1.js';
import { useAuth } from '../auth.js';
import { EmptyState, ErrorState, LoadingState } from '../states.js';
import './oversight.css';

const dateFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'medium',
  timeStyle: 'short',
  timeZone: 'UTC',
});
const filterFields = [
  ['actor_user_id', 'Actor ID or system', 'text'],
  ['entity_type', 'Entity type', 'text'],
  ['entity_id', 'Entity ID', 'text'],
  ['action', 'Action', 'text'],
  ['from', 'From date (UTC)', 'date'],
  ['to', 'To date (UTC)', 'date'],
] as const;

function comparable(value: JsonValue | undefined): string {
  if (value === undefined) return '';
  if (Array.isArray(value)) return '[' + value.map(comparable).join(',') + ']';
  if (value !== null && typeof value === 'object') {
    return (
      '{' +
      Object.keys(value)
        .sort()
        .map((key) => JSON.stringify(key) + ':' + comparable(value[key]))
        .join(',') +
      '}'
    );
  }
  return JSON.stringify(value);
}

function Changes({ entry }: { entry: AuditEntry }) {
  const { before, after } = entry;
  const keys = [...new Set([...Object.keys(before ?? {}), ...Object.keys(after ?? {})])]
    .sort()
    .filter((key) => comparable(before?.[key]) !== comparable(after?.[key]));
  return (
    <details className="audit-details">
      <summary>View changes</summary>
      {keys.length ? (
        <table className="audit-diff">
          <caption className="sr-only">Changed fields</caption>
          <thead>
            <tr>
              <th scope="col">Field</th>
              <th scope="col">Before</th>
              <th scope="col">After</th>
            </tr>
          </thead>
          <tbody>
            {keys.map((key) => (
              <tr key={key}>
                <th scope="row">{key}</th>
                <td>
                  <pre>{JSON.stringify(before?.[key], null, 2) ?? 'Absent'}</pre>
                </td>
                <td>
                  <pre>{JSON.stringify(after?.[key], null, 2) ?? 'Absent'}</pre>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="work-muted">No changed fields recorded.</p>
      )}
      <dl className="audit-metadata">
        <div>
          <dt>Request</dt>
          <dd>{entry.request_id}</dd>
        </div>
        {entry.ip && (
          <div>
            <dt>IP address</dt>
            <dd>{entry.ip}</dd>
          </div>
        )}
      </dl>
    </details>
  );
}

export function AuditPage() {
  const { client, sessionId } = useAuth();
  const [filters, setFilters] = useState<AuditQuery>({});
  const [cursors, setCursors] = useState<number[]>([]);
  const cursor = cursors.at(-1);
  const query = useQuery({
    queryKey: ['audit', sessionId, filters, cursor],
    queryFn: () => client.audit(cursor === undefined ? filters : { ...filters, before_id: cursor }),
    retry: false,
  });
  const data = query.data;

  return (
    <div className="work-page">
      <header>
        <h1>Audit trail</h1>
        <p className="page-description">Review changes made by users and automated jobs.</p>
      </header>
      <form
        className="oversight-filters"
        onSubmit={(event) => {
          event.preventDefault();
          const values = new FormData(event.currentTarget);
          const next: AuditQuery = {};
          for (const [key] of filterFields) {
            const value = values.get(key);
            if (typeof value === 'string' && value.trim()) next[key] = value.trim();
          }
          setFilters(next);
          setCursors([]);
        }}
        onReset={() => {
          setFilters({});
          setCursors([]);
        }}
      >
        {filterFields.map(([key, label, type]) => (
          <label className="field" key={key}>
            {label}
            <input type={type} name={key} maxLength={256} />
          </label>
        ))}
        <div className="work-actions">
          <button type="submit" className="primary-button">
            Apply filters
          </button>
          <button type="reset" className="secondary-button">
            Clear
          </button>
        </div>
      </form>
      {query.isPending && <LoadingState label="Loading audit entries…" />}
      {query.isError && (
        <ErrorState
          title="Audit entries could not load"
          description={query.error.message}
          action={
            <button type="button" className="secondary-button" onClick={() => void query.refetch()}>
              Try again
            </button>
          }
        />
      )}
      {data?.entries.length === 0 && <EmptyState title="No audit entries match" />}
      {!!data?.entries.length && (
        <div
          role="region"
          aria-label="Audit table"
          className="work-table-wrap"
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- Keyboard users can scroll a wide table.
          tabIndex={0}
        >
          <table className="work-table oversight-audit">
            <caption className="sr-only">Audit entries</caption>
            <thead>
              <tr>
                <th scope="col">Time (UTC)</th>
                <th scope="col">Actor</th>
                <th scope="col">Entity</th>
                <th scope="col">Action</th>
                <th scope="col">Changes</th>
              </tr>
            </thead>
            <tbody>
              {data.entries.map((entry) => (
                <tr key={entry.id}>
                  <td>
                    <time dateTime={entry.timestamp}>
                      {dateFormat.format(new Date(entry.timestamp))}
                    </time>
                  </td>
                  <td>{entry.actor_user_id === null ? 'System' : '#' + entry.actor_user_id}</td>
                  <td>
                    {entry.entity_type}
                    <small className="audit-entity-id">{entry.entity_id}</small>
                  </td>
                  <th scope="row">{entry.action}</th>
                  <td>
                    <Changes entry={entry} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {(data || cursors.length > 0) && (
        <nav aria-label="Audit pagination" className="work-actions audit-pagination">
          <button
            type="button"
            className="secondary-button"
            disabled={!cursors.length}
            onClick={() => setCursors((pages) => pages.slice(0, -1))}
          >
            Previous
          </button>
          <span aria-live="polite">Page {cursors.length + 1}</span>
          <button
            type="button"
            className="secondary-button"
            disabled={data?.next_cursor == null}
            onClick={() => {
              if (data?.next_cursor != null) setCursors([...cursors, data.next_cursor]);
            }}
          >
            Next
          </button>
        </nav>
      )}
    </div>
  );
}
