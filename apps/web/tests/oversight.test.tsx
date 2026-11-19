import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { expect, it, vi } from 'vitest';

import type { AuditEntry, OrgOverview, UserRecord } from '../../../contracts/core-api.v1.js';
import { createClient } from '../src/api.js';
import { App } from '../src/app.js';
import { AuthProvider } from '../src/auth.js';

async function show(
  path: string,
  permissions: string[],
  handle: (url: URL, init?: RequestInit) => Response,
) {
  const fetcher = vi.fn<typeof fetch>((input, init) => {
    if (typeof input !== 'string') throw new Error('Expected an API URL');
    const url = new URL(input);
    if (url.pathname === '/api/auth/login') {
      return Promise.resolve(Response.json({ access_token: 'fixture', refresh_token: 'fixture' }));
    }
    if (url.pathname === '/api/auth/me') {
      return Promise.resolve(
        Response.json({
          org_id: 'example',
          user_id: 1,
          roles: ['configured-role'],
          permissions,
        }),
      );
    }
    return Promise.resolve(handle(url, init));
  });
  const client = createClient('https://core.example.test', fetcher);
  await client.login({ org_id: 'example', email: 'test@example.test', password: 'fixture' });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

it('filters audit entries, pages by cursor, and shows only changed fields', async () => {
  const entry: AuditEntry = {
    id: 41,
    timestamp: '2026-11-18T13:00:00Z',
    actor_user_id: 12,
    action: 'task.transition',
    entity_type: 'task',
    entity_id: '7',
    before: { status: 'in_progress', extra: { a: 1, b: 2 } },
    after: { status: 'done', extra: { b: 2, a: 1 } },
    request_id: 'demo-request',
    ip: null,
  };
  const seen: string[] = [];
  const user = await show('/audit', ['audit:read'], (url) => {
    expect(url.pathname).toBe('/api/audit');
    seen.push(url.search);
    if (!url.searchParams.has('action')) return Response.json({ entries: [], next_cursor: null });
    if (url.searchParams.has('before_id')) {
      return Response.json({ entries: [{ ...entry, id: 40, entity_id: '6' }], next_cursor: null });
    }
    return Response.json({ entries: [entry], next_cursor: entry.id });
  });
  await screen.findByText('No audit entries match');
  await user.type(screen.getByLabelText('Actor ID or system'), '12');
  await user.type(screen.getByLabelText('Action'), 'task.transition');
  await user.click(screen.getByRole('button', { name: 'Apply filters' }));
  await screen.findByRole('rowheader', { name: 'task.transition' });
  await user.click(screen.getByText('View changes'));
  const diff = screen.getByRole('table', { name: 'Changed fields' });
  expect(within(diff).getByText('"in_progress"')).toBeVisible();
  expect(within(diff).getByText('"done"')).toBeVisible();
  expect(within(diff).queryByText('extra')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Next' }));
  await screen.findByText('6', { exact: true });
  expect(screen.getByText('Page 2')).toBeVisible();
  expect(seen).toContain('?actor_user_id=12&action=task.transition&before_id=41');
  await user.click(screen.getByRole('button', { name: 'Previous' }));
  expect(await screen.findByText('7', { exact: true })).toBeVisible();
});

it('saves a department and roles from configuration and requires explicit deactivation', async () => {
  let record: UserRecord = {
    id: 3,
    name: 'Rina Rao',
    email: 'rina@example.test',
    roles: ['reader'],
    department: null,
    active: true,
  };
  const writes: unknown[] = [];
  const user = await show('/users', ['users:roles'], (url, init) => {
    if (url.pathname === '/api/users') {
      return Response.json({
        users: [record],
        roles: ['reader', 'editor'],
        departments: ['Records'],
      });
    }
    if (typeof init?.body !== 'string') throw new Error('Expected a JSON request body');
    const change = JSON.parse(init.body) as Record<string, unknown>;
    writes.push(change);
    if (init?.method === 'PUT') {
      expect(url.pathname).toBe('/api/users/3/roles');
      record = { ...record, roles: change.roles as string[] };
      return new Response(null, { status: 204 });
    }
    expect(url.pathname).toBe('/api/users/3');
    record = { ...record, ...change };
    return Response.json({ user: record });
  });
  await user.selectOptions(
    await screen.findByRole('combobox', { name: 'Department for Rina Rao' }),
    'Records',
  );
  await user.click(screen.getByRole('button', { name: 'Save department' }));
  await screen.findByText('Saved');
  await user.click(screen.getByText('Edit roles'));
  await user.click(screen.getByRole('checkbox', { name: 'editor' }));
  await user.click(screen.getByRole('button', { name: 'Save roles' }));
  await screen.findByText('editor, reader');
  await user.click(screen.getByText('Deactivate', { exact: true }));
  expect(writes).toEqual([{ department: 'Records' }, { roles: ['editor', 'reader'] }]);
  await user.click(screen.getByRole('button', { name: 'Confirm deactivation' }));
  expect(await screen.findByText('Inactive')).toBeVisible();
  expect(writes.at(-1)).toEqual({ active: false });
});

it('renders loaded organisation rules in priority order for a permitted account', async () => {
  const data: OrgOverview = {
    org_id: 'example',
    languages: ['hi', 'en'],
    departments: ['Records'],
    ocr_engine: 'tesseract',
    document_types: ['circular'],
    routing_rules: [
      {
        id: 'last',
        priority: 0,
        conditions: {},
        target: { kind: 'department_queue', value: 'Records' },
      },
      {
        id: 'first',
        priority: 5,
        conditions: { document_type: 'circular' },
        target: { kind: 'role_queue', value: 'editor' },
        escalation_after_days: 2,
      },
    ],
  };
  await show('/organisation', ['org:read'], (url) => {
    expect(url.pathname).toBe('/api/organisation');
    return Response.json(data);
  });
  expect(await screen.findByText('hi, en')).toBeVisible();
  const table = screen.getByRole('table', { name: 'Routing rules in evaluation order' });
  expect(
    within(table)
      .getAllByRole('rowheader')
      .map((cell) => cell.textContent),
  ).toEqual(['first', 'last']);
  expect(within(table).getByText('2 days')).toBeVisible();
  expect(screen.queryByRole('link', { name: 'Users and roles' })).not.toBeInTheDocument();
});
