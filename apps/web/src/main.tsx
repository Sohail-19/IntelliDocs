import '@fontsource-variable/manrope/wght.css';
import './styles.css';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';

import { createClient } from './api.js';
import { App } from './app.js';
import { AuthProvider } from './auth.js';

const queryClient = new QueryClient();
const client = createClient(import.meta.env.VITE_API_URL ?? 'http://localhost:3000');
const root = document.getElementById('root');
if (!root) throw new Error('Application root is missing');

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
);
