import { useQuery } from '@tanstack/react-query';
import { createContext, useContext, useSyncExternalStore } from 'react';
import type { ReactNode } from 'react';

import type { ApiClient } from './api.js';

function useSession(client: ApiClient) {
  const sessionId = useSyncExternalStore(client.subscribe, client.sessionId, () => 0);
  const profile = useQuery({
    queryKey: ['core-profile', sessionId],
    queryFn: client.profile,
    enabled: sessionId > 0,
    retry: false,
    gcTime: 0,
  });
  return { sessionId, profile, client };
}

const AuthContext = createContext<ReturnType<typeof useSession> | null>(null);

export function AuthProvider({ client, children }: { client: ApiClient; children: ReactNode }) {
  const auth = useSession(client);
  return <AuthContext.Provider value={auth}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error('AuthProvider is required');
  return auth;
}
