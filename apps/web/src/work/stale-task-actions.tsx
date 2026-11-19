import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { StaleDecision, TaskRecord } from '../../../../contracts/core-api.v1.js';
import { useAuth } from '../auth.js';
import { errorMessage } from '../documents/document-state.js';

const labels: Record<StaleDecision['decision'], string> = {
  keep: 'Keep task',
  update: 'Update task',
  close: 'Close task',
};

export function StaleTaskActions({ task }: { task: TaskRecord }) {
  const { client, sessionId } = useAuth();
  const cache = useQueryClient();
  const relations = task.stale_relation_ids ?? [];
  const [selected, setSelected] = useState('');
  const relationId = relations.includes(selected) ? selected : relations[0];
  const [decision, setDecision] = useState<StaleDecision['decision']>();
  const [reason, setReason] = useState('');
  const [action, setAction] = useState(task.action);
  const save = useMutation({
    mutationFn: ({ id, change }: { id: string; change: StaleDecision }) =>
      client.decideStaleness(id, task.id, change),
    onSuccess: async () => {
      setDecision(undefined);
      setReason('');
      await Promise.all([
        cache.invalidateQueries({ queryKey: ['task-feed', sessionId] }),
        cache.invalidateQueries({ queryKey: ['documents', sessionId, task.doc_id] }),
      ]);
    },
  });

  if (!relationId) return null;

  return (
    <div className="relation-decision-form">
      {relations.length > 1 && (
        <label className="field">
          Related change
          <select
            value={relationId}
            disabled={save.isPending}
            onChange={(event) => {
              setSelected(event.target.value);
              setDecision(undefined);
              save.reset();
            }}
          >
            {relations.map((id, index) => (
              <option key={id} value={id}>
                Related change {index + 1}
              </option>
            ))}
          </select>
        </label>
      )}
      <Link className="text-link" to={'/relations/' + encodeURIComponent(relationId)}>
        View relation
      </Link>
      {!decision ? (
        <div className="work-actions">
          {(['keep', 'update', 'close'] as const).map((choice) => (
            <button
              key={choice}
              className="secondary-button"
              type="button"
              disabled={save.isPending}
              onClick={() => {
                save.reset();
                setDecision(choice);
                setAction(task.action);
                setReason('');
              }}
            >
              {labels[choice]}
            </button>
          ))}
        </div>
      ) : (
        <form
          className="relation-decision-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (!reason.trim() || (decision === 'update' && !action.trim()) || save.isPending)
              return;
            const change =
              decision === 'update'
                ? { decision, reason: reason.trim(), action: action.trim() }
                : { decision, reason: reason.trim() };
            save.mutate({ id: relationId, change });
          }}
        >
          {decision === 'update' && (
            <>
              <label className="field">
                Replacement action
                <textarea
                  value={action}
                  required
                  rows={3}
                  maxLength={2000}
                  onChange={(event) => setAction(event.target.value)}
                />
              </label>
              <p className="work-muted">
                The updated task will cite the newer passage in this relation.
              </p>
            </>
          )}
          <label className="field">
            Decision reason
            <textarea
              value={reason}
              required
              rows={2}
              maxLength={2000}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          <div className="work-actions">
            <button
              className="primary-button"
              type="submit"
              disabled={
                save.isPending || !reason.trim() || (decision === 'update' && !action.trim())
              }
            >
              {labels[decision]}
            </button>
            <button
              className="text-link"
              disabled={save.isPending}
              type="button"
              onClick={() => setDecision(undefined)}
            >
              Cancel
            </button>
          </div>
        </form>
      )}
      {save.isPending && <p role="status">Saving decision…</p>}
      {save.isError && (
        <p className="document-row-error" role="alert">
          {errorMessage(save.error, 'The task decision could not be saved.')}
        </p>
      )}
    </div>
  );
}
