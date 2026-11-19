import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { expect, it, vi } from 'vitest';

import type {
  AuthProfile,
  ObligationDeadline,
  ObligationOwner,
  ReviewObligation,
  ReviewOptions,
  TaskFeed,
  TaskRecord,
} from '../../../contracts/core-api.v1.js';
import { createClient } from '../src/api.js';
import { App } from '../src/app.js';
import { AuthProvider } from '../src/auth.js';

const task: TaskRecord = {
  id: 7,
  obligation_id: 'obligation-7',
  doc_id: 'document-4',
  action: 'File the quarterly return',
  owner: 'Alex Patel',
  due_date: '2025-07-01',
  overdue: true,
  status: 'open',
  rejection_reason: null,
  citations: [
    {
      doc_id: 'document-4',
      page: 3,
      char_start: 14,
      char_end: 39,
      quote: 'File the quarterly return',
    },
  ],
  scope: 'mine',
  assignee_user_id: 1,
  queue_type: null,
  queue_name: null,
  allowed_transitions: ['acknowledged', 'rejected'],
};

const extractedOwner: ObligationOwner = {
  raw_text: 'The filing office',
  resolved_department: null,
  resolved_role: null,
  resolved: false,
};
const extractedDeadline: ObligationDeadline = {
  raw_text: 'Thirty days from acceptance',
  normalized_date: null,
  is_relative: true,
  anchor_date: null,
  resolved: false,
};
const obligation: ReviewObligation = {
  id: 'obligation-7',
  doc_id: 'document-4',
  action: task.action,
  owner: extractedOwner,
  deadline: extractedDeadline,
  status: 'needs_review',
  citations: task.citations,
  confidence: 0.45,
  confidence_breakdown: {
    quote_exact: 0.35,
    quote_fuzzy: 0,
    owner_resolved: 0,
    deadline_resolved: 0,
    recognized_cue: 0.1,
    cross_chunk_agreement: 0,
  },
  obligation_type: 'reporting',
  extractor_version: 'fixture',
};

function pendingResponse() {
  let resolve: (response: Response) => void = () => undefined;
  const promise = new Promise<Response>((finish) => {
    resolve = finish;
  });
  return { promise, resolve };
}

async function show(
  path: string,
  permissions: string[],
  handle: (url: string, init?: RequestInit) => Response | Promise<Response>,
) {
  const profile: AuthProfile = {
    user_id: 1,
    org_id: 'example',
    roles: ['custom-role'],
    permissions,
  };
  const fetcher = vi.fn<typeof fetch>(async (input, init) => {
    if (typeof input !== 'string') throw new Error('Expected a URL');
    const url = new URL(input).pathname;
    if (url === '/api/auth/login') return Response.json({ access_token: 'a', refresh_token: 'r' });
    if (url === '/api/auth/me') return Response.json(profile);
    return handle(url, init);
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

it('shows API-granted queues and actions, opens the cited page, and rolls back a failed transition', async () => {
  const { promise, resolve } = pendingResponse();
  const feed: TaskFeed = { tasks: [task], available_scopes: ['mine', 'department'] };
  const user = await show('/tasks', ['tasks:read', 'tasks:transition'], (url, init) => {
    if (url === '/api/tasks') return Response.json(feed);
    if (url === '/api/documents/document-4/source')
      return Response.json({ error: 'No PDF' }, { status: 415 });
    expect(url).toBe('/api/tasks/7/status');
    expect(init?.body).toBe(JSON.stringify({ status: 'acknowledged' }));
    return promise;
  });
  const table = await screen.findByRole('table', { name: 'My tasks' });
  expect(within(table).getByText('Alex Patel')).toBeInTheDocument();
  expect(within(table).getByText('Overdue')).toBeInTheDocument();
  expect(within(table).queryByRole('button', { name: 'Start' })).not.toBeInTheDocument();
  expect(within(table).queryByRole('button', { name: 'Mark done' })).not.toBeInTheDocument();
  expect(screen.queryByRole('tab', { name: 'Review queue' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('tab', { name: 'Department queue' }));
  expect(screen.getByText('No tasks in this queue')).toBeInTheDocument();
  await user.keyboard('{ArrowLeft}');
  expect(screen.getByRole('tab', { name: 'My tasks', selected: true })).toHaveFocus();
  await user.click(screen.getByRole('button', { name: /page 3/i }));
  const source = screen.getByRole('dialog', { name: 'Source citation' });
  expect(within(source).getByText('Page 3')).toBeInTheDocument();
  await user.click(within(source).getByRole('button', { name: 'Close citation' }));
  await user.click(screen.getByRole('button', { name: 'Acknowledge' }));
  expect(await screen.findByText('Acknowledged')).toBeInTheDocument();
  resolve(Response.json({ error: 'Task changed' }, { status: 409 }));
  expect(await screen.findByText('Task changed')).toBeInTheDocument();
  expect(screen.getByText('Open')).toBeInTheDocument();
});

it('requires a nonblank reason and submits the trimmed reason for rejection', async () => {
  let current = task;
  const reject = vi.fn((init?: RequestInit) => {
    expect(init?.body).toBe(JSON.stringify({ status: 'rejected', reason: 'Superseded' }));
    current = {
      ...task,
      status: 'rejected',
      overdue: false,
      allowed_transitions: [],
      rejection_reason: 'Superseded',
    };
    return Response.json({ task: current });
  });
  const user = await show('/tasks', ['tasks:read', 'tasks:transition'], (url, init) =>
    url === '/api/tasks'
      ? Response.json({ tasks: [current], available_scopes: ['mine', 'review'] })
      : reject(init),
  );
  await user.click(await screen.findByRole('button', { name: 'Reject' }));
  expect(screen.getByRole('tab', { name: 'Review queue' })).toBeInTheDocument();
  const reason = screen.getByRole('textbox', { name: 'Rejection reason' });
  const confirm = screen.getByRole('button', { name: 'Confirm rejection' });
  expect(reason).toBeRequired();
  await user.type(reason, '   ');
  expect(confirm).toBeDisabled();
  expect(reject).not.toHaveBeenCalled();
  await user.type(reason, 'Superseded ');
  await user.click(confirm);
  expect(await screen.findByText('Rejected')).toBeInTheDocument();
  expect(await screen.findByText('Superseded')).toBeInTheDocument();
  expect(reject).toHaveBeenCalledTimes(1);
});

it('retains the original owner and deadline during and after a manager override', async () => {
  const { promise, resolve } = pendingResponse();
  let current = obligation;
  let approved = false;
  const options: ReviewOptions = {
    departments: ['Legal'],
    roles: [{ name: 'Advisor', department: 'Legal' }],
  };
  const user = await show('/review', ['obligations:review'], (url, init) => {
    if (url === '/api/obligations/review') {
      return Response.json({ obligations: approved ? [] : [current] });
    }
    if (url === '/api/obligations/review/options') return Response.json(options);
    if (url === '/api/documents/document-4/obligations/obligation-7/review/approve') {
      expect(init?.method).toBe('POST');
      approved = true;
      return Response.json({ obligation: { ...current, status: 'verified' } });
    }
    expect(url).toBe('/api/documents/document-4/obligations/obligation-7/review');
    expect(init?.method).toBe('PATCH');
    expect(init?.body).toBe(
      JSON.stringify({ owner: { department: 'Legal', role: 'Advisor' }, due_date: '2027-08-30' }),
    );
    return promise;
  });
  const table = await screen.findByRole('table', { name: 'Obligations that need review' });
  expect(within(table).getByText('45%')).toBeInTheDocument();
  expect(within(table).getByText('Exact quote')).toBeInTheDocument();
  expect(within(table).getByText('35 pp')).toBeInTheDocument();
  await within(table).findByRole('option', { name: 'Advisor' });
  await user.selectOptions(within(table).getByLabelText('Owner role'), 'Advisor');
  fireEvent.change(within(table).getByLabelText('Due date'), { target: { value: '2027-08-30' } });
  await user.click(within(table).getByRole('button', { name: 'Save override' }));
  expect(await within(table).findByText('Current · Legal · Advisor')).toBeInTheDocument();
  expect(within(table).getByText('Current · 2027-08-30')).toBeInTheDocument();
  expect(within(table).getByText('The filing office')).toBeInTheDocument();
  expect(within(table).getByText('Thirty days from acceptance')).toBeInTheDocument();
  current = {
    ...obligation,
    owner: {
      ...extractedOwner,
      resolved_department: 'Legal',
      resolved_role: 'Advisor',
      resolved: true,
    },
    deadline: {
      ...extractedDeadline,
      normalized_date: '2027-08-30',
      is_relative: false,
      resolved: true,
    },
    extracted_owner: extractedOwner,
    extracted_deadline: extractedDeadline,
  };
  resolve(Response.json({ obligation: current }));
  await vi.waitFor(() =>
    expect(within(table).getByRole('button', { name: 'Approve' })).toBeEnabled(),
  );
  expect(within(table).getByText('The filing office')).toBeInTheDocument();
  expect(within(table).getByText('Thirty days from acceptance')).toBeInTheDocument();
  await user.click(within(table).getByRole('button', { name: 'Approve' }));
  expect(await screen.findByText('No obligations need review')).toBeInTheDocument();
  expect(approved).toBe(true);
});

it('restores the effective review values while preserving an unsaved edit if the override fails', async () => {
  const { promise, resolve } = pendingResponse();
  const user = await show('/review', ['obligations:review'], (url) => {
    if (url === '/api/obligations/review') return Response.json({ obligations: [obligation] });
    if (url === '/api/obligations/review/options') {
      return Response.json({ departments: ['Legal'], roles: [] });
    }
    return promise;
  });
  const department = await screen.findByLabelText('Owner department');
  await screen.findByRole('option', { name: 'Legal' });
  await user.selectOptions(department, 'Legal');
  await user.click(screen.getByRole('button', { name: 'Save override' }));
  expect(await screen.findByText('Current · Legal')).toBeInTheDocument();
  resolve(Response.json({ error: 'Override could not be saved' }, { status: 503 }));
  expect(await screen.findByText('Override could not be saved')).toBeInTheDocument();
  expect(screen.getByText('Current · The filing office')).toBeInTheDocument();
  expect(screen.getByText('The filing office')).toBeInTheDocument();
  expect(department).toHaveValue('Legal');
});

it.each([
  ['Keep task', { decision: 'keep', reason: 'Reviewed the amendment' }],
  ['Update task', { decision: 'update', reason: 'Reviewed the amendment', action: 'File weekly' }],
  ['Close task', { decision: 'close', reason: 'Reviewed the amendment' }],
])('saves the stale-task decision %s with its reason', async (label, change) => {
  let pending = true;
  const user = await show(
    '/tasks',
    ['tasks:read', 'tasks:transition', 'relations:review'],
    (url, init) => {
      if (url === '/api/tasks') {
        return Response.json({
          tasks: pending ? [{ ...task, stale: true, stale_relation_ids: ['r1'] }] : [],
          available_scopes: ['mine'],
        });
      }
      expect(url).toBe('/api/relations/r1/stale-obligations/7');
      expect(init).toMatchObject({ method: 'PATCH', body: JSON.stringify(change) });
      pending = false;
      return new Response(null, { status: 204 });
    },
  );
  expect(await screen.findByText('Stale · manager decision required')).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Acknowledge' })).not.toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'View relation' })).toHaveAttribute(
    'href',
    '/relations/r1',
  );
  await user.click(screen.getByRole('button', { name: label }));
  expect(screen.getByRole('button', { name: label })).toBeDisabled();
  await user.type(screen.getByLabelText('Decision reason'), change.reason);
  if ('action' in change && change.action) {
    await user.clear(screen.getByLabelText('Replacement action'));
    await user.type(screen.getByLabelText('Replacement action'), change.action);
  }
  await user.click(screen.getByRole('button', { name: label }));
  expect(await screen.findByText('No tasks in this queue')).toBeVisible();
});
