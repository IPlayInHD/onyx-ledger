import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { AuthProvider } from './auth/AuthProvider'
import { TaxYearProvider } from './components/Shell'
import { shouldRetry } from './api/client'
import './styles/tokens.css'
import './styles/base.css'
import './styles/trust.css'
import './styles/shell.css'
import './styles/product.css'

/* Retry policy is set ONCE, here, from the client's own rule: reads may be
   retried, writes may not. A component cannot opt a dangerous write back in
   by forgetting to pass an option. */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: shouldRetry,
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
    mutations: { retry: false },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <TaxYearProvider>
            <App />
          </TaxYearProvider>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
