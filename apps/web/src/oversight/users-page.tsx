import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import type { CoreAdminApi, UserChange, UserRecord } from '../../../../contracts/core-api.v1.js';
import { useAuth } from '../auth.js';
import { EmptyState, ErrorState, LoadingState } from '../states.js';
import './oversight.css';

type Users = CoreAdminApi['/api/users']['result'];
type Change = UserChange | { roles: string[] };

function UserRow({
  user,
  roles,
  departments,
}: {
  user: UserRecord;
  roles: string[];
  departments: string[];
}) {
  const { client, sessionId, profile } = useAuth();
  const cache = useQueryClient();
  const key = ['admin-users', sessionId];
  const [roleDraft, setRoleDraft] = useState<string[]>();
  const [departmentDraft, setDepartmentDraft] = useState<string>();
  const selectedRoles = roleDraft ?? user.roles;
  const department = departmentDraft ?? user.department ?? '';
  const ownAccount = profile.data?.user_id === user.id;
  const mutation = useMutation({
    mutationFn: async (change: Change) => {
      if ('roles' in change) {
        await client.replaceUserRoles(user.id, change.roles);
        return { ...user, roles: change.roles };
      }
      return (await client.changeUser(user.id, change)).user;
    },
    onSuccess: (updated, change) => {
      cache.setQueryData<Users>(
        key,
        (current) =>
          current && {
            ...current,
            users: current.users.map((item) => (item.id === updated.id ? updated : item)),
          },
      );
      if ('roles' in change) {
        setRoleDraft(undefined);
        if (ownAccount) void profile.refetch();
      }
      if ('department' in change) setDepartmentDraft(undefined);
      if (!updated.active && ownAccount) void client.logout().catch(() => undefined);
    },
  });
  const roleChanged =
    selectedRoles.length !== user.roles.length ||
    selectedRoles.some((role) => !user.roles.includes(role));
  const selectableRoles = [...new Set([...roles, ...user.roles])].sort();

  return (
    <tr>
      <th scope="row">
        {user.name}
        <small className="audit-entity-id">
          {user.email} · #{user.id}
        </small>
      </th>
      <td>
        <span className="work-status">{user.active ? 'Active' : 'Inactive'}</span>
      </td>
      <td>
        {user.active ? (
          <form
            className="user-department"
            onSubmit={(event) => {
              event.preventDefault();
              mutation.mutate({ department: department || null });
            }}
          >
            <label className="field">
              <span className="sr-only">Department for {user.name}</span>
              <select
                value={department}
                disabled={mutation.isPending}
                onChange={(event) => setDepartmentDraft(event.target.value)}
              >
                <option value="">No department</option>
                {user.department && !departments.includes(user.department) && (
                  <option value={user.department} disabled>
                    {user.department}
                  </option>
                )}
                {departments.map((name) => (
                  <option key={name}>{name}</option>
                ))}
              </select>
            </label>
            <button
              type="submit"
              className="secondary-button"
              disabled={mutation.isPending || department === (user.department ?? '')}
            >
              Save department
            </button>
          </form>
        ) : (
          (user.department ?? '—')
        )}
      </td>
      <td>
        <p>{user.roles.join(', ') || '—'}</p>
        {user.active && (
          <details className="user-roles">
            <summary>Edit roles</summary>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                mutation.mutate({ roles: [...selectedRoles].sort() });
              }}
            >
              <fieldset disabled={mutation.isPending}>
                <legend className="sr-only">Roles for {user.name}</legend>
                {selectableRoles.map((name) => (
                  <label key={name} className="role-choice">
                    <input
                      type="checkbox"
                      checked={selectedRoles.includes(name)}
                      onChange={(event) => {
                        setRoleDraft(
                          event.target.checked
                            ? [...selectedRoles, name]
                            : selectedRoles.filter((role) => role !== name),
                        );
                      }}
                    />
                    {name}
                  </label>
                ))}
              </fieldset>
              <button
                type="submit"
                className="secondary-button"
                disabled={
                  mutation.isPending ||
                  !roleChanged ||
                  !selectedRoles.length ||
                  selectedRoles.some((role) => !roles.includes(role))
                }
              >
                Save roles
              </button>
            </form>
          </details>
        )}
      </td>
      <td>
        {user.active && (
          <details className="user-deactivation">
            <summary>Deactivate</summary>
            <p className="work-muted">This signs the user out and blocks future sign-ins.</p>
            <button
              type="button"
              className="secondary-button work-danger"
              disabled={mutation.isPending}
              onClick={() => mutation.mutate({ active: false })}
            >
              Confirm deactivation
            </button>
          </details>
        )}
        {mutation.isPending && <p role="status">Saving…</p>}
        {mutation.isError && (
          <ErrorState title="User change could not be saved" description={mutation.error.message} />
        )}
        {mutation.isSuccess && <p role="status">Saved</p>}
      </td>
    </tr>
  );
}

export function UsersPage() {
  const { client, sessionId } = useAuth();
  const users = useQuery({
    queryKey: ['admin-users', sessionId],
    queryFn: client.users,
    retry: false,
  });
  const data = users.data;

  return (
    <div className="work-page">
      <header>
        <h1>Users and roles</h1>
        <p className="page-description">Manage access and department membership.</p>
      </header>
      {users.isPending && <LoadingState label="Loading users…" />}
      {users.isError && (
        <ErrorState
          title="Users could not load"
          description={users.error.message}
          action={
            <button type="button" className="secondary-button" onClick={() => void users.refetch()}>
              Try again
            </button>
          }
        />
      )}
      {data?.users.length === 0 && <EmptyState title="No users found" />}
      {!!data?.users.length && (
        <div
          role="region"
          aria-label="Users table"
          className="work-table-wrap"
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- Keyboard users can scroll a wide table.
          tabIndex={0}
        >
          <table className="work-table oversight-users">
            <caption className="sr-only">Organisation users</caption>
            <thead>
              <tr>
                <th scope="col">User</th>
                <th scope="col">Status</th>
                <th scope="col">Department</th>
                <th scope="col">Roles</th>
                <th scope="col">Account actions</th>
              </tr>
            </thead>
            <tbody>
              {data.users.map((user) => (
                <UserRow
                  key={user.id}
                  user={user}
                  roles={data.roles}
                  departments={data.departments}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
