import { Link } from 'react-router-dom';
import type {
  AuthorityWarning,
  DocumentAuthorityStatus,
} from '../../../../contracts/core-api.v1.js';
import './relations.css';

const labels: Record<DocumentAuthorityStatus, string> = {
  in_force: 'In force',
  superseded: 'Superseded',
  partially_amended: 'Amended',
  withdrawn: 'Withdrawn',
  under_review: 'Under review',
};

export function AuthorityBadge({ status }: { status: DocumentAuthorityStatus }) {
  return (
    <span className={'authority-badge' + (status === 'in_force' ? ' authority-badge-current' : '')}>
      {labels[status]}
    </span>
  );
}

export function AuthorityWarnings({
  warnings = [],
  stale = false,
  document = false,
}: {
  warnings?: AuthorityWarning[] | undefined;
  stale?: boolean | undefined;
  document?: boolean;
}) {
  if (!warnings.length && !stale) return null;
  return (
    <div className="authority-warning" role="note">
      <strong>{document ? 'Document authority changed' : 'Stale source warning'}</strong>
      {warnings.length ? (
        <ul>
          {warnings.map((warning) => (
            <li key={warning.relation_id}>
              {document && warning.status === 'partially_amended'
                ? 'A newer document amended part of this document.'
                : warning.message}{' '}
              <Link
                className="text-link"
                to={'/documents/' + encodeURIComponent(warning.newer_doc_id)}
              >
                View newer document
              </Link>
            </li>
          ))}
        </ul>
      ) : (
        <p>Some source instructions are no longer current.</p>
      )}
    </div>
  );
}
