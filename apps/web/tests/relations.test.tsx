import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { expect, it, vi } from 'vitest';
import type { StoredDocumentRelation } from '../../../contracts/core-api.v1.js';
import { createClient } from '../src/api.js';
import { App } from '../src/app.js';
import { AuthProvider } from '../src/auth.js';

const relation: StoredDocumentRelation = {
  id: 'r1',
  source_doc_id: 'newer',
  source_filename: 'Amendment.pdf',
  target_doc_id: 'older',
  target_filename: 'Original.pdf',
  relation_type: 'references',
  scope: 'clauses',
  status: 'proposed',
  source_citations: [
    { doc_id: 'newer', page: 3, char_start: 0, char_end: 13, quote: 'Status update' },
  ],
  target_citations: [
    { doc_id: 'older', page: 8, char_start: 0, char_end: 10, quote: 'Old status' },
  ],
  confidence: 0.8,
  confidence_breakdown: {
    explicit_reference: 0.2,
    both_quotes_verified: 0.45,
    date_order_consistent: 0.15,
    recognized_cue: 0,
  },
  detected_by: 'fixture',
  created_at: '2025-01-10T00:00:00Z',
  reviewed_by: null,
  reviewed_at: null,
  review_reason: null,
};

async function show(permissions: string[], handle: (url: URL, init?: RequestInit) => Response) {
  const fetcher = vi.fn<typeof fetch>((input, init) => {
    if (typeof input !== 'string') throw new Error('Expected an API URL');
    const url = new URL(input);
    if (url.pathname === '/api/auth/login')
      return Promise.resolve(Response.json({ access_token: 'a', refresh_token: 'r' }));
    if (url.pathname === '/api/auth/me')
      return Promise.resolve(
        Response.json({ user_id: 1, org_id: 'example', roles: ['custom'], permissions }),
      );
    return Promise.resolve(handle(url, init));
  });
  const client = createClient('https://core.example.test', fetcher);
  await client.login({ org_id: 'example', email: 'test@example.test', password: 'fixture' });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={['/relations']}>
          <App />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

it('compares both sources, saves a corrected type, and moves the relation to Confirmed', async () => {
  let stored = relation;
  const handle = vi.fn((url: URL, init?: RequestInit) => {
    if (url.pathname === '/api/relations')
      return Response.json({
        relations: url.searchParams.get('status') === stored.status ? [stored] : [],
      });
    if (url.pathname === '/api/documents/older/source')
      return Response.json({ error: 'No PDF' }, { status: 415 });
    if (url.pathname === '/api/relations/r1/type') {
      expect(init).toMatchObject({ method: 'PATCH', body: '{"relation_type":"amends"}' });
      stored = { ...stored, relation_type: 'amends' };
    } else if (url.pathname === '/api/relations/r1/confirm') {
      expect(init?.method).toBe('POST');
      stored = { ...stored, status: 'confirmed' };
    } else throw new Error(url.pathname);
    return Response.json({ relation: stored });
  });
  const user = await show(['relations:review'], handle);
  expect(await screen.findByRole('link', { name: 'Relations' })).toBeVisible();
  expect(
    within(await screen.findByRole('region', { name: 'Newer document' })).getByText(
      'Status update',
    ),
  ).toBeVisible();
  const older = within(screen.getByRole('region', { name: 'Older document' }));
  expect(older.getByText('Old status')).toBeVisible();
  expect(screen.getByText('Quotes verified on both sides')).toBeVisible();
  await user.click(older.getByRole('button', { name: 'Page 8' }));
  const viewer = within(screen.getByRole('dialog', { name: 'Source citation' }));
  expect(viewer.getByText('Verified quote · Page 8')).toBeVisible();
  await user.click(viewer.getByRole('button', { name: 'Close citation' }));
  await user.selectOptions(screen.getByLabelText('Relation type'), 'amends');
  expect(screen.getByRole('button', { name: 'Confirm relation' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Save type' }));
  expect(await screen.findByRole('heading', { name: 'Amends' })).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Confirm relation' }));
  expect(await screen.findByRole('button', { name: 'Confirmed', pressed: true })).toBeVisible();
  expect(await screen.findByRole('article', { name: 'Amends' })).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Confirm relation' })).not.toBeInTheDocument();
});

it('requires a nonblank reason to dismiss', async () => {
  let dismissed = false;
  const user = await show(['relations:review'], (url, init) => {
    if (url.pathname === '/api/relations')
      return Response.json({ relations: dismissed ? [] : [relation] });
    expect(url.pathname).toBe('/api/relations/r1/dismiss');
    expect(init?.body).toBe('{"reason":"Wrong circular"}');
    dismissed = true;
    return Response.json({ relation: { ...relation, status: 'dismissed' } });
  });
  await user.click(await screen.findByRole('button', { name: 'Dismiss relation' }));
  expect(screen.getByRole('button', { name: 'Dismiss with reason' })).toBeDisabled();
  await user.type(screen.getByLabelText('Dismissal reason'), 'Wrong circular');
  await user.click(screen.getByRole('button', { name: 'Dismiss with reason' }));
  expect(await screen.findByText('Relation dismissed.')).toBeVisible();
  expect(await screen.findByText('No proposed relations')).toBeVisible();
});

it('denies access without the grant', async () => {
  const handle = vi.fn(() => {
    throw new Error('Unexpected relation request');
  });
  await show(['tasks:read'], handle);
  expect(await screen.findByText('Access denied')).toBeVisible();
  expect(screen.queryByRole('link', { name: 'Relations' })).not.toBeInTheDocument();
  expect(handle).not.toHaveBeenCalled();
});
