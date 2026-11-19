import { useQuery } from '@tanstack/react-query';

import type { RoutingRule } from '../../../../contracts/core-api.v1.js';
import { useAuth } from '../auth.js';
import { ErrorState, LoadingState } from '../states.js';
import './oversight.css';

const destinations = {
  user: 'User',
  role_queue: 'Role queue',
  department_queue: 'Department queue',
};
const conditionLabels = {
  obligation_type: 'Obligation type',
  owner_department: 'Owner department',
  document_type: 'Document type',
  keywords: 'Keywords',
  deadline_within_days: 'Due within (days)',
} satisfies Record<keyof RoutingRule['conditions'], string>;

function RuleConditions({ conditions }: { conditions: RoutingRule['conditions'] }) {
  const entries = Object.entries(conditions) as [
    keyof RoutingRule['conditions'],
    string | number | string[],
  ][];
  if (!entries.length) return 'Default';
  return (
    <dl className="audit-metadata">
      {entries.map(([key, value]) => (
        <div key={key}>
          <dt>{conditionLabels[key]}</dt>
          <dd>{Array.isArray(value) ? value.join(', ') : value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function OrganisationPage() {
  const { client, sessionId } = useAuth();
  const query = useQuery({
    queryKey: ['organisation', sessionId],
    queryFn: client.organisation,
    retry: false,
  });
  const data = query.data;
  const rules =
    data?.routing_rules
      .filter((rule): rule is RoutingRule => 'id' in rule)
      .sort((left, right) => right.priority - left.priority) ?? [];
  const mappings = data?.routing_rules.filter((rule) => 'document_type' in rule) ?? [];

  return (
    <div className="work-page">
      <header>
        <h1>Organisation</h1>
        <p className="page-description">
          Loaded configuration for {data?.org_id ?? 'your organisation'}.
        </p>
      </header>
      {query.isPending && <LoadingState label="Loading organisation configuration…" />}
      {query.isError && (
        <ErrorState
          title="Organisation configuration could not load"
          description={query.error.message}
          action={
            <button type="button" className="secondary-button" onClick={() => void query.refetch()}>
              Try again
            </button>
          }
        />
      )}
      {data && (
        <>
          <dl className="org-overview">
            {(
              [
                ['Languages', data.languages],
                ['Departments', data.departments],
                ['Document types', data.document_types],
                ['OCR engine', [data.ocr_engine]],
              ] as const
            ).map(([label, values]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{values.join(', ') || 'None configured'}</dd>
              </div>
            ))}
          </dl>
          <section aria-labelledby="org-routing-title">
            <h2 id="org-routing-title">Routing rules</h2>
            {data.routing_policy && (
              <dl className="audit-metadata org-routing-policy">
                <div>
                  <dt>Review role</dt>
                  <dd>{data.routing_policy.review_role}</dd>
                </div>
                <div>
                  <dt>Minimum auto-routing confidence</dt>
                  <dd>
                    {new Intl.NumberFormat(undefined, {
                      style: 'percent',
                      maximumFractionDigits: 2,
                    }).format(data.routing_policy.min_confidence)}
                  </dd>
                </div>
              </dl>
            )}
            {rules.length > 0 && (
              <div
                role="region"
                aria-label="Routing rules table"
                className="work-table-wrap"
                // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- Keyboard users can scroll a wide table.
                tabIndex={0}
              >
                <table className="work-table">
                  <caption className="sr-only">Routing rules in evaluation order</caption>
                  <thead>
                    <tr>
                      <th scope="col">Rule</th>
                      <th scope="col">Priority</th>
                      <th scope="col">Match conditions</th>
                      <th scope="col">Target</th>
                      <th scope="col">Escalates after</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rules.map((rule) => (
                      <tr key={rule.id}>
                        <th scope="row">{rule.id}</th>
                        <td>{rule.priority}</td>
                        <td>
                          <RuleConditions conditions={rule.conditions} />
                        </td>
                        <td>
                          {rule.target.value}
                          <small className="audit-entity-id">
                            {destinations[rule.target.kind]}
                          </small>
                        </td>
                        <td>
                          {rule.escalation_after_days === undefined
                            ? '—'
                            : rule.escalation_after_days +
                              (rule.escalation_after_days === 1 ? ' day' : ' days')}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {mappings.length > 0 && (
              <dl className="audit-metadata org-routing-policy">
                {mappings.map((rule, index) => (
                  <div key={index}>
                    <dt>{rule.document_type}</dt>
                    <dd>{rule.department}</dd>
                  </div>
                ))}
              </dl>
            )}
            {!rules.length && !mappings.length && (
              <p className="page-description">No routing rules configured.</p>
            )}
          </section>
        </>
      )}
    </div>
  );
}
