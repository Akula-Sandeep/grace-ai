import { initializeErrorReporting } from './lib/observability';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import '@fontsource-variable/inter';
import './style.css';
import './styles/tokens.css';
import './styles/base.css';
import './styles/shell.css';
import './styles/chat.css';
import './styles/auth.css';
import './styles/cinema.css';
import App from './App';
import ErrorBoundary from './components/ErrorBoundary';
import { AuthProvider } from './lib/authContext';
import { currentLocale } from './lib/i18n';
import { registerServiceWorker } from './lib/serviceWorker';

void initializeErrorReporting();
registerServiceWorker();

function readSetting(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
const theme = readSetting('grace_appearance');
const density = readSetting('grace_density');
const motion = readSetting('grace_motion');
// Only the dark theme is complete. The light/system variants recolor a few
// containers while hundreds of component rules stay dark, which rendered the
// sign-in page half light, half dark. Dark is enforced until the redesigned
// token system ships a complete light theme.
void theme;
document.documentElement.dataset.theme = 'dark';
document.documentElement.dataset.density = ['comfortable', 'compact'].includes(density) ? density : 'comfortable';
document.documentElement.dataset.motion = motion === 'reduced' ? 'reduced' : 'system';
document.documentElement.lang = currentLocale();

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, refetchOnWindowFocus: false, retry: 1 },
  },
});

createRoot(document.getElementById('root')).render(
  <ErrorBoundary>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <App />
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </ErrorBoundary>
);
