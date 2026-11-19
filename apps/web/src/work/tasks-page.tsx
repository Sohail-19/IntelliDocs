import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useId, useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import type {
  AuthProfile,
  Citation,
  TaskAction,
  TaskFeed,
  TaskScope,
  TaskStatus,
  TaskTransition,
} from '../../../../contracts/core-api.v1.js';
import { ApiError } from '../api.js';
import { useAuth } from '../auth.js';
import { CitationChips, CitationPanel } from '../search/citation-panel.js';
import { EmptyState, ErrorState, LoadingState } from '../states.js';
import { RejectionForm } from './rejection-form.js';
import './work.css';

const tabLabels: Record<TaskScope, string> = {
  mine: 'My tasks',
  department: 'Department queue',
  review: 'Review queue',
};
const statuses: Record<TaskStatus, string> = {
  open: 'Open',
  acknowledged: 'Acknowledged',
  in_progress: 'In progress',
  done: 'Done',
  rejected: 'Rejected',
  overdue: 'Overdue',
};
const actions: Record<TaskAction, string> = {
  acknowledged: 'Acknowledge',
  in_progress: 'Start',
  done: 'Mark done',
  rejected: 'Reject',
};

export function TasksPage() {
  const profile = useOutletContext<AuthProfile>();
  const { client, sessionId } = useAuth();
  const canRead = profile.permissions.includes('tasks:read');
  const canChange = profile.permissions.includes('tasks:transition');
  const cache = useQueryClient();
  const tabs = useId();
  const [scope, setScope] = useState<TaskScope>('mine');
  const [citation, setCitation] = useState<Citation>();
  const [rejection, setRejection] = useState<number>();
  const key = ['task-feed', sessionId];
  const feed = useQuery({ queryKey: key, queryFn: client.tasks, enabled: canRead, retry: false });
  const mutation = useMutation({
    mutationFn: ({ id, change }: { id: number; change: TaskTransition }) =>
      client.transitionTask(id, change),
    onMutate: async ({ id, change }) => {
      await cache.cancelQueries({ queryKey: key });
      const previous = cache.getQueryData<TaskFeed>(key);
      if (previous) {
        cache.setQueryData<TaskFeed>(key, {
          ...previous,
          tasks: previous.tasks.map((task) =>
            task.id !== id
              ? task
              : {
                  ...task,
                  status: change.status,
                  allowed_transitions: [],
                  rejection_reason: change.status === 'rejected' ? change.reason : null,
                  overdue:
                    change.status === 'done' || change.status === 'rejected' ? false : task.overdue,
                },
          ),
        });
      }
      return previous;
    },
    onError: (_error, _variables, previous) => {
      if (previous) cache.setQueryData(key, previous);
    },
    onSuccess: ({ task }) => {
      setRejection(undefined);
      cache.setQueryData<TaskFeed>(
        key,
        (current) =>
          current && {
            ...current,
            tasks: current.tasks.map((item) => (item.id === task.id ? task : item)),
          },
      );
    },
    onSettled: () => cache.invalidateQueries({ queryKey: key }),
  });
  const scopes = feed.data?.available_scopes ?? ['mine'];
  const selected = scopes.includes(scope) ? scope : 'mine';
  const items = feed.data?.tasks.filter((task) => task.scope === selected) ?? [];
  if (!canRead)
    return (
      <ErrorState title="Access denied" description="Your account does not have access to tasks." />
    );

  return (
    <div className="work-page">
      <header>
        <h1>My tasks</h1>
        <p className="page-description">Track assigned obligations and their due dates.</p>
      </header>
      <div className="work-tabs" role="tablist" aria-label="Task queues">
        {scopes.map((tab, index) => (
          <button
            key={tab}
            id={tabs + '-' + tab}
            type="button"
            role="tab"
            aria-controls={tabs + '-panel'}
            aria-selected={selected === tab}
            tabIndex={selected === tab ? 0 : -1}
            onClick={() => setScope(tab)}
            onKeyDown={(event) => {
              const position =
                event.key === 'Home'
                  ? 0
                  : event.key === 'End'
                    ? scopes.length - 1
                    : event.key === 'ArrowRight'
                      ? (index + 1) % scopes.length
                      : event.key === 'ArrowLeft'
                        ? (index + scopes.length - 1) % scopes.length
                        : -1;
              const next = scopes[position];
              if (!next) return;
              event.preventDefault();
              setScope(next);
              document.getElementById(tabs + '-' + next)?.focus();
            }}
          >
            {tabLabels[tab]}
          </button>
        ))}
      </div>
      <section
        id={tabs + '-panel'}
        role="tabpanel"
        aria-labelledby={tabs + '-' + selected}
        tabIndex={0}
      >
        {feed.isPending && <LoadingState label="Loading tasks…" />}
        {feed.isError && (
          <ErrorState
            title="Tasks could not load"
            action={
              <button
                className="secondary-button"
                type="button"
                onClick={() => void feed.refetch()}
              >
                Try again
              </button>
            }
          />
        )}
        {mutation.isError && (
          <ErrorState
            title="Task status could not be updated"
            description={mutation.error instanceof ApiError ? mutation.error.message : 'Try again.'}
          />
        )}
        {feed.data && !items.length && <EmptyState title="No tasks in this queue" />}
        {items.length > 0 && (
          <div
            className="work-table-wrap"
            role="region"
            aria-label={tabLabels[selected] + ' table'}
            // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- Keyboard users can scroll a wide table.
            tabIndex={0}
          >
            <table className="work-table">
              <caption className="sr-only">{tabLabels[selected]}</caption>
              <thead>
                <tr>
                  <th scope="col">Obligation</th>
                  <th scope="col">Owner</th>
                  <th scope="col">Due date</th>
                  <th scope="col">Status</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {items.map((task) => (
                  <tr key={task.id}>
                    <th scope="row">
                      <span className="work-obligation">{task.action}</span>
                      {task.citations.length > 0 && (
                        <CitationChips citations={task.citations} open={setCitation} />
                      )}
                    </th>
                    <td>{task.owner ?? task.queue_name ?? '—'}</td>
                    <td>
                      {task.due_date ? <time dateTime={task.due_date}>{task.due_date}</time> : '—'}
                      {task.overdue && <span className="work-overdue">Overdue</span>}
                    </td>
                    <td>
                      <span className="work-status">{statuses[task.status]}</span>
                      {task.rejection_reason && (
                        <p className="work-muted">{task.rejection_reason}</p>
                      )}
                    </td>
                    <td>
                      {rejection === task.id ? (
                        <RejectionForm
                          pending={mutation.isPending}
                          onReject={(reason) =>
                            mutation.mutate({ id: task.id, change: { status: 'rejected', reason } })
                          }
                          onCancel={() => setRejection(undefined)}
                        />
                      ) : (
                        <div className="work-actions">
                          {canChange &&
                            task.allowed_transitions.map((action) => (
                              <button
                                key={action}
                                className="secondary-button"
                                type="button"
                                disabled={mutation.isPending}
                                onClick={() =>
                                  action === 'rejected'
                                    ? setRejection(task.id)
                                    : mutation.mutate({ id: task.id, change: { status: action } })
                                }
                              >
                                {actions[action]}
                              </button>
                            ))}
                          {mutation.isPending && mutation.variables?.id === task.id && (
                            <span role="status">Saving…</span>
                          )}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {citation && <CitationPanel citation={citation} onClose={() => setCitation(undefined)} />}
    </div>
  );
}
