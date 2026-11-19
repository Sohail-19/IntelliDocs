import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import type { AuthCredentials, AuthProfile, AuthTokens } from '../../../contracts/core-api.v1.js';
import { createClient } from '../src/api.js';
import type { ApiClient } from '../src/api.js';
import { App } from '../src/app.js';
import { AuthProvider } from '../src/auth.js';

const base = 'https://core.example.test';
const credentials: AuthCredentials = {
  org_id: 'example',
  email: 'user@example.test',
  password: 'password',
};
const first: AuthTokens = {
  access_token: 'short-lived',
  refresh_token: 'refresh-one',
  token_type: 'Bearer',
  expires_in: 60,
};
const rotated: AuthTokens = { ...first, access_token: 'new-token', refresh_token: 'refresh-two' };
const profile: AuthProfile = {
  user_id: 1,
  org_id: 'example',
  roles: ['custom-reviewer'],
  permissions: ['obligations:review', 'tasks:read'],
};

function json(value: unknown, status = 200) {
  return Promise.resolve(Response.json(value, { status }));
}

function Location() {
  return <span data-testid="location">{useLocation().pathname}</span>;
}

function show(client: ApiClient, path: string) {
  const queryClient = new QueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={[path]}>
          <App />
          <Location />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe('web authentication', () => {
  it('redirects a logged-out visitor to login and restores the protected URL after login', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(first))
      .mockResolvedValueOnce(Response.json(profile))
      .mockResolvedValueOnce(Response.json({ tasks: [], available_scopes: ['mine'] }));
    show(createClient(base, fetcher), '/tasks');
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/login');
    expect(fetcher).not.toHaveBeenCalled();

    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Organization ID'), credentials.org_id);
    await user.type(screen.getByLabelText('Email address'), credentials.email);
    await user.type(screen.getByLabelText('Password'), credentials.password);
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(await screen.findByRole('heading', { name: 'My tasks' })).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/tasks');
    expect(fetcher).toHaveBeenCalledWith(
      base + '/api/auth/login',
      expect.objectContaining({ body: JSON.stringify(credentials) }),
    );
  });

  it('only lists permitted items and protects direct navigation regardless of the role name', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(first))
      .mockResolvedValueOnce(Response.json({ ...profile, roles: ['admin'] }));
    const client = createClient(base, fetcher);
    await client.login(credentials);
    show(client, '/audit');
    const nav = within(await screen.findByRole('navigation', { name: 'Workspace' }));
    expect(nav.getAllByRole('link').map((link) => link.textContent?.trim())).toEqual([
      'Workspace',
      'Review queue',
      'Task feed',
    ]);
    expect(screen.getByRole('alert')).toHaveTextContent('Access denied');
  });

  it('rotates an expired access token once and retries each rejected request', async () => {
    const fetcher = vi.fn<typeof fetch>((url, init) => {
      if (url === base + '/api/auth/login') return json(first);
      if (url === base + '/api/auth/refresh') {
        expect(init?.body).toBe(JSON.stringify({ refresh_token: first.refresh_token }));
        return json(rotated);
      }
      const token = new Headers(init?.headers).get('Authorization');
      if (token === 'Bearer short-lived') return json({ error: 'Expired' }, 401);
      expect(token).toBe('Bearer new-token');
      return json(profile);
    });
    const client = createClient(base, fetcher);
    await client.login(credentials);
    await expect(Promise.all([client.profile(), client.profile()])).resolves.toEqual([
      profile,
      profile,
    ]);
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      base + '/api/auth/login',
      base + '/api/auth/me',
      base + '/api/auth/me',
      base + '/api/auth/refresh',
      base + '/api/auth/me',
      base + '/api/auth/me',
    ]);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it('clears the shell on logout and asks core to revoke the refresh token', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(first))
      .mockResolvedValueOnce(Response.json(profile))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    const client = createClient(base, fetcher);
    await client.login(credentials);
    show(client, '/');
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Sign out' }));
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    await waitFor(() =>
      expect(fetcher).toHaveBeenCalledWith(
        base + '/api/auth/logout',
        expect.objectContaining({ body: JSON.stringify({ refresh_token: first.refresh_token }) }),
      ),
    );
  });
});
