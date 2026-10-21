import CommandPalette from '../components/CommandPalette';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, newRequestId, userFacingError, validatePageResponse } from '../lib/api';
import { useAuth } from '../lib/authContext';
import { t } from '../lib/i18n';
import Icon from '../components/Icon';
import ToastViewport from '../components/ToastViewport';
import Sidebar from './Sidebar';
import { conversationMarkdown, downloadText } from '../lib/share';
import ShareDialog from '../components/ShareDialog';
import ShortcutsDialog from '../components/ShortcutsDialog';
import useProjects from './hooks/useProjects';
import { forgetGuest } from '../lib/firstRun';
import WorkspaceRoutes from './WorkspaceRoutes';
import useConversations from './hooks/useConversations';
import useSidebarState from './hooks/useSidebarState';
import useWatchlistState from './hooks/useWatchlistState';

function withMessageIds(messages = []) {
  return (Array.isArray(messages) ? messages : []).map((message) => (
    message.id ? message : { ...message, id: newRequestId() }
  ));
}

function readRecentSearches() {
  try {
    const stored = JSON.parse(localStorage.getItem('grace_recent_searches') || '[]');
    return Array.isArray(stored) ? stored.filter((item) => typeof item === 'string').slice(0, 6) : [];
  } catch {
    return [];
  }
}

function App() {
  const {
    isAuthenticated: token,
    user,
    isLoading: authLoading,
    logout: signOut,
  } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const [panel, setPanel] = useState(() => (
    location.pathname === '/watchlist' ? 'watchlist'
      : location.pathname === '/watched' ? 'watched'
      : location.pathname === '/taste' ? 'taste'
        : location.pathname.startsWith('/settings/') ? 'settings'
          : location.pathname.startsWith('/movie/') ? 'movie'
          : location.pathname.startsWith('/person/') ? 'person'
            : location.search.includes('q=') ? 'search' : 'chat'
  ));
  const [convos, setConvos] = useState([]);
  const [cid, setCid] = useState(() => sessionStorage.getItem('grace_guest_conversation_id'));
  const [messages, setMessages] = useState([]);
  const [activeMovieTitle, setActiveMovieTitle] = useState(null);
  const [providerData, setProviderData] = useState({});
  const { data: state, refetch: refreshState } = useQuery({
    queryKey: ['user-state', user?.id],
    queryFn: () => api('/state'),
    enabled: token && !authLoading,
  });
  const conversationGroups = useConversations(convos);
  const watchlistState = useWatchlistState(state);
  const [selected, setSelected] = useState(() => (
    location.pathname.startsWith('/movie/') ? location.pathname.split('/')[2] : null
  ));
  const [search, setSearch] = useState(() => new URLSearchParams(location.search).get('q') || '');
  const [conversationSearch, setConversationSearch] = useState('');
  // The chat whose share dialog is open; switching chats closes it.
  const [shareFor, setShareFor] = useState(null);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const closeShortcuts = useCallback(() => setShortcutsOpen(false), []);
  const closeShare = useCallback(() => setShareFor(null), []);
  const projectsApi = useProjects(token);
  const activeProjectId = projectsApi.activeProject?.id ?? null;
  const [showArchived, setShowArchived] = useState(false);
  const [results, setResults] = useState([]);
  const [error, setErrorState] = useState('');
  const [toasts, setToasts] = useState([]);
  const sidebar = useSidebarState();
  const { open: sidebarOpen, setOpen: setSidebarOpen, collapsed: sidebarCollapsed } = sidebar;
  const [loadingPanel, setLoadingPanel] = useState(false);
  const [savingMovie, setSavingMovie] = useState(null);
  const [isOnline, setIsOnline] = useState(() => typeof navigator === 'undefined' || navigator.onLine !== false);
  const [undoDelete, setUndoDelete] = useState(null);
  const [undoCollection, setUndoCollection] = useState(null);
  const [movieOrigin, setMovieOrigin] = useState('search');
  const [selectedPerson, setSelectedPerson] = useState(() => (
    location.pathname.startsWith('/person/') ? location.pathname.split('/')[2] : null
  ));
  const [personOrigin, setPersonOrigin] = useState('chat');
  const [pendingPrompt, setPendingPrompt] = useState('');
  const [peopleResults, setPeopleResults] = useState([]);
  const [conversationResults, setConversationResults] = useState([]);
  const [recentSearches, setRecentSearches] = useState(() => readRecentSearches());
  const [paletteOpen, setPaletteOpen] = useState(false);
  const movieSearchRef = useRef(null);

  function rememberSearch(query) {
    setRecentSearches((current) => {
      const next = [query, ...current.filter((item) => item.toLowerCase() !== query.toLowerCase())].slice(0, 6);
      try {
        localStorage.setItem('grace_recent_searches', JSON.stringify(next));
      } catch {
        // Recent searches are a convenience; storage may be unavailable.
      }
      return next;
    });
  }
  const searchRequestRef = useRef(0);
  const lastRoutedSearchRef = useRef('');
  const conversationRequestRef = useRef(0);
  const loadedConversationRef = useRef(null);
  const searchControllerRef = useRef(null);
  const conversationSearchRef = useRef(null);
  const conversationCursorRef = useRef(null);
  const messageCursorRef = useRef(null);
  const moreConversationsRef = useRef(true);
  const loadingConversationsRef = useRef(false);
  const conversationListRequestRef = useRef(0);
  const conversationListControllerRef = useRef(null);

  function setError(value) {
    setErrorState(value);
    if (typeof value === 'string' && value.trim()) {
      const id = `${Date.now()}-${Math.random()}`;
      // One toast per distinct message: a repeated failure refreshes the existing
      // toast instead of stacking copies over the page.
      setToasts((current) => [
        ...current.filter((toast) => toast.message !== value),
        { id, message: value, kind: 'error', duration: 6000 },
      ].slice(-3));
    }
  }

  const setActiveConversationId = useCallback((id) => {
    loadedConversationRef.current = id == null ? null : String(id);
    setCid(id);
  }, []);
  // Confirmations ("Link copied") use the same toast area, without alarm styling.
  const notify = useCallback((message) => {
    const id = `${Date.now()}-${Math.random()}`;
    setToasts((current) => [
      ...current.filter((toast) => toast.message !== message),
      { id, message, kind: 'info', duration: 3500 },
    ].slice(-3));
  }, []);

  const dismissToast = useCallback((id) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  useEffect(() => {
    if (!undoDelete) return undefined;
    const timer = window.setTimeout(() => setUndoDelete(null), 5000);
    return () => window.clearTimeout(timer);
  }, [undoDelete]);

  useEffect(() => {
    if (!undoCollection) return undefined;
    const timer = window.setTimeout(() => setUndoCollection(null), 5000);
    return () => window.clearTimeout(timer);
  }, [undoCollection]);

  useEffect(() => {
    const handleOnline = () => setIsOnline(true);
    const handleOffline = () => setIsOnline(false);
    window.addEventListener('online', handleOnline);
    window.addEventListener('offline', handleOffline);
    return () => {
      window.removeEventListener('online', handleOnline);
      window.removeEventListener('offline', handleOffline);
    };
  }, []);

  const refresh = useCallback(async () => {
    if (token) await refreshState();
  }, [refreshState, token]);

  useEffect(() => {
    if (token) sessionStorage.removeItem('grace_guest_conversation_id');
    else if (cid) sessionStorage.setItem('grace_guest_conversation_id', cid);
  }, [cid, token]);

  const loadConversationPage = useCallback(async ({ reset = false } = {}) => {
    if (!token || (!reset && (loadingConversationsRef.current || !moreConversationsRef.current))) return;
    loadingConversationsRef.current = true;
    const requestId = ++conversationListRequestRef.current;
    conversationListControllerRef.current?.abort();
    const controller = new AbortController();
    conversationListControllerRef.current = controller;
    const cursor = reset ? null : conversationCursorRef.current;
    try {
      const params = new URLSearchParams({
        limit: '30',
        archived: String(showArchived),
      });
      if (cursor) params.set('cursor', cursor);
      if (conversationSearch.trim()) params.set('q', conversationSearch.trim());
      if (activeProjectId != null) params.set('project', String(activeProjectId));
      const page = await api(`/conversations?${params}`, { signal: controller.signal });
      validatePageResponse(page, 'Conversations');
      if (requestId !== conversationListRequestRef.current) return;
      setConvos((current) => reset ? page.items : [...current, ...page.items]);
      conversationCursorRef.current = page.next_cursor;
      moreConversationsRef.current = Boolean(page.next_cursor);
    } catch (err) {
      if (err.name !== 'AbortError' && requestId === conversationListRequestRef.current) {
        setError(userFacingError(err));
      }
    } finally {
      if (requestId === conversationListRequestRef.current) {
        loadingConversationsRef.current = false;
        conversationListControllerRef.current = null;
      }
    }
  }, [token, showArchived, conversationSearch, activeProjectId]);

  useEffect(() => {
    if (!token) return;
    const timer = window.setTimeout(() => {
      conversationCursorRef.current = null;
      moreConversationsRef.current = true;
      loadConversationPage({ reset: true });
    }, 180);
    return () => window.clearTimeout(timer);
  }, [loadConversationPage, token]);

  useEffect(() => {
    const handleShortcut = (event) => {
      const target = event.target;
      const typing = target instanceof HTMLElement
        && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName));
      const modifier = event.ctrlKey || event.metaKey;
      if (modifier && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setPaletteOpen((open) => !open);
      } else if (modifier && event.shiftKey && event.key.toLowerCase() === 'o') {
        event.preventDefault();
        newConversation();
      } else if (modifier && event.key === '\\') {
        event.preventDefault();
        const mobile = window.matchMedia('(max-width: 760px)').matches;
        const visible = mobile ? sidebarOpen : !sidebarCollapsed;
        if (visible) sidebar.close();
        else sidebar.reopen();
      } else if (!modifier && !event.altKey && event.key === '?' && !typing) {
        event.preventDefault();
        setShortcutsOpen(true);
      } else if (event.shiftKey && event.key === 'Escape') {
        // Back to the message box from anywhere (ChatGPT's shortcut).
        const composer = document.querySelector('textarea[aria-label="Message Grace"]');
        if (composer) {
          event.preventDefault();
          if (panel !== 'chat') setPanel('chat');
          window.requestAnimationFrame(() => composer.focus());
        }
      } else if (!modifier && !event.altKey && event.key === '/' && !typing) {
        // "/" jumps to search, but never while the user is typing.
        event.preventDefault();
        sidebar.reopen();
        window.requestAnimationFrame(() => movieSearchRef.current?.focus());
      }
    };
    window.addEventListener('keydown', handleShortcut);
    return () => window.removeEventListener('keydown', handleShortcut);
  });

  useEffect(() => {
    const handleAuthExpired = () => {
      conversationRequestRef.current += 1;
      setCid(null);
      loadedConversationRef.current = null;
      queryClient.removeQueries({ queryKey: ['user-state'] });
      setConvos([]);
      navigate('/login');
    };

    window.addEventListener('auth-expired', handleAuthExpired);

    return () => window.removeEventListener('auth-expired', handleAuthExpired);
  }, [navigate, queryClient]);

  const loadConversation = useCallback(async (id) => {
    const requestId = ++conversationRequestRef.current;
    messageCursorRef.current = null;
    setLoadingPanel(true);

    try {
      const data = token
        ? await Promise.all([
          api(`/conversations/${id}`),
          api(`/conversations/${id}/messages?limit=30`),
        ])
        : [await api(`/guest/conversations/${id}`), null];

      if (requestId !== conversationRequestRef.current) return;
      loadedConversationRef.current = id;
      setCid(id);
      const [metadata, messagesPage] = data;
      setMessages(withMessageIds(messagesPage ? messagesPage.items : metadata.messages));
      messageCursorRef.current = messagesPage?.next_cursor || null;
      setActiveMovieTitle(metadata.active_movie_title || null);
      setPanel('chat');
      setSidebarOpen(false);
      navigate(`/c/${encodeURIComponent(id)}`);
    } catch (err) {
      if (requestId !== conversationRequestRef.current) return;
      if (err?.status === 404 || err?.status === 403) {
        // The link points at a conversation this visitor cannot open (deleted,
        // expired guest chat, someone else's). Remember the miss so route
        // changes never retry it, and land on a fresh chat instead.
        loadedConversationRef.current = id;
        sessionStorage.removeItem('grace_guest_conversation_id');
        setCid(null);
        setMessages([]);
        setActiveMovieTitle(null);
        setPanel('chat');
        navigate('/', { replace: true });
        setError('That conversation isn’t available. You can start a new one here.');
        return;
      }
      setError(userFacingError(err));
    } finally {
      if (requestId === conversationRequestRef.current) setLoadingPanel(false);
    }
  }, [navigate, token, setSidebarOpen]);

  const loadOlderMessages = useCallback(async () => {
    if (!token || !cid || !messageCursorRef.current) return false;
    try {
      const params = new URLSearchParams({
        cursor: messageCursorRef.current,
        limit: '30',
      });
      const page = await api(`/conversations/${cid}/messages?${params}`);
      messageCursorRef.current = page.next_cursor;
      setMessages((current) => [...withMessageIds(page.items), ...current]);
      return page.items.length > 0;
    } catch (err) {
      setError(userFacingError(err));
      return false;
    }
  }, [cid, token]);

  async function deleteConversation(event, conversation) {
    event.stopPropagation();
    try {
      await api(`/conversations/${conversation.id}`, { method: 'DELETE' });
      setConvos((current) => current.filter((item) => item.id !== conversation.id));
      setUndoDelete(conversation);
      if (cid != null && String(cid) === String(conversation.id)) {
        setCid(null);
        loadedConversationRef.current = null;
        setMessages([]);
        setActiveMovieTitle(null);
        setSelected(null);
      }
    } catch (err) {
      setError(userFacingError(err));
    }
  }

  async function restoreConversation() {
    if (!undoDelete) return;
    try {
      await api(`/conversations/${undoDelete.id}/restore`, { method: 'POST' });
      setConvos((current) => [undoDelete, ...current]);
      setUndoDelete(null);
    } catch (err) {
      setError(userFacingError(err));
    }
  }

  async function toggleConversationFlag(conversation, kind) {
    const pinned = kind === 'pin' ? !conversation.is_pinned : conversation.is_pinned;
    const archived = kind === 'archive' ? !conversation.is_archived : conversation.is_archived;
    const path = kind === 'pin'
      ? `/conversations/${conversation.id}/pin?pinned=${pinned}`
      : `/conversations/${conversation.id}/archive?archived=${archived}`;
    try {
      await api(path, { method: 'PATCH' });
      if (kind === 'archive' && archived !== showArchived) {
        setConvos((current) => current.filter((item) => item.id !== conversation.id));
      } else {
        setConvos((current) => current.map((item) => (
          item.id === conversation.id
            ? { ...item, is_pinned: pinned, is_archived: archived }
            : item
        )));
      }
    } catch (err) {
      setError(userFacingError(err));
    }
  }

  async function exportConversation(conversation) {
    try {
      const page = await api(`/conversations/${conversation.id}/messages?limit=200`);
      downloadText('grace-conversation.md', conversationMarkdown(page.items, conversation.title || 'Grace conversation'));
      notify('Conversation exported as Markdown.');
    } catch (err) {
      setError(userFacingError(err));
    }
  }

  async function renameConversation(conversation, title) {
    try {
      const updated = await api(`/conversations/${conversation.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ title }),
      });
      setConvos((current) => current.map((item) => (
        item.id === conversation.id ? { ...item, title: updated.title } : item
      )));
      return true;
    } catch (err) {
      setError(userFacingError(err));
      return false;
    }
  }

  const doSearch = useCallback(async (event, requestedSearch = search) => {
    event?.preventDefault();

    const query = requestedSearch.trim();
    if (!query) return;

    const requestId = ++searchRequestRef.current;
    searchControllerRef.current?.abort();
    const controller = new AbortController();
    searchControllerRef.current = controller;

    setLoadingPanel(true);
    setResults([]);
    setPeopleResults([]);
    setConversationResults([]);
    rememberSearch(query);
    lastRoutedSearchRef.current = query;
    setPanel('search');
    navigate(`/?q=${encodeURIComponent(query)}`);
    setError('');

    try {
      // Movies, people and conversations load together; one group failing
      // never hides the others (UI spec §21, §102).
      const [movies, people, conversations] = await Promise.allSettled([
        api(`/movies/search?q=${encodeURIComponent(query)}`, { signal: controller.signal }),
        api(`/people/search?q=${encodeURIComponent(query)}`, { signal: controller.signal }),
        token
          ? api(`/conversations?${new URLSearchParams({ q: query, limit: '5', archived: 'false' })}`, { signal: controller.signal })
          : Promise.resolve({ items: [] }),
      ]);
      if (requestId !== searchRequestRef.current) return;
      if ([movies, people, conversations].some((item) => item.status === 'rejected' && item.reason?.name === 'AbortError')) return;

      setResults(movies.status === 'fulfilled' ? movies.value.results || [] : []);
      setPeopleResults(people.status === 'fulfilled' ? people.value.results || [] : []);
      setConversationResults(conversations.status === 'fulfilled' ? conversations.value.items || [] : []);
      setSidebarOpen(false);
      if (movies.status === 'rejected' && people.status === 'rejected') {
        setError(userFacingError(movies.reason));
      }
    } finally {
      if (requestId === searchRequestRef.current) {
        setLoadingPanel(false);
        searchControllerRef.current = null;
      }
    }
  }, [navigate, search, setSidebarOpen, token]);

  async function toggleSearchWatchlist(id, saved) {
    if (!token) {
      navigate('/signup', { state: { prompt: 'Create a free account to save movies to your watchlist.' } });
      return;
    }
    if (savingMovie === id) return;

    setSavingMovie(id);
    setError('');

    try {
      await api(`/movies/${id}/watchlist`, {
        method: saved ? 'DELETE' : 'POST',
      });

      await refresh();
    } catch (err) {
      setError(userFacingError(err));
    } finally {
      setSavingMovie(null);
    }
  }

  async function removeCollectionMovie(id, collection) {
    const resource = collection === 'watchlist' ? 'watchlist' : 'watched';
    const title = watchlistState.find(collection, id)?.title;
    try {
      await api(`/movies/${id}/${resource}`, { method: 'DELETE' });
      setUndoCollection({ id, collection, title: title || 'Movie' });
      await refresh();
    } catch (err) {
      setError(userFacingError(err));
    }
  }

  async function undoCollectionRemoval() {
    if (!undoCollection) return;
    const resource = undoCollection.collection === 'watchlist' ? 'watchlist' : 'watched';
    try {
      await api(`/movies/${undoCollection.id}/${resource}`, { method: 'POST' });
      setUndoCollection(null);
      await refresh();
    } catch (err) {
      setError(userFacingError(err));
    }
  }

  async function markCollectionMovieWatched(id) {
    try {
      await api(`/movies/${id}/move-to-watched`, { method: 'POST' });
      await refresh();
    } catch (err) {
      setError(userFacingError(err));
    }
  }

  async function rateMovie(id, rating) {
    if (rating === '') return;
    try {
      await api(`/movies/${id}/rating`, {
        method: 'PUT',
        body: JSON.stringify({ rating: Number(rating) }),
      });
      await refresh();
    } catch (err) {
      setError(userFacingError(err));
    }
  }

  function newConversation() {
    conversationRequestRef.current += 1;
    setCid(null);
    loadedConversationRef.current = null;
    setMessages([]);
    setActiveMovieTitle(null);
    setSelected(null);
    setPanel('chat');
    setSidebarOpen(false);
  }

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const path = location.pathname;
      if (path.startsWith('/movie/')) {
        setSelected(decodeURIComponent(path.split('/')[2] || ''));
        setPanel('movie');
        return;
      }
      if (path.startsWith('/person/')) {
        setSelectedPerson(decodeURIComponent(path.split('/')[2] || ''));
        setPanel('person');
        return;
      }
      if (path.startsWith('/c/')) {
        const id = decodeURIComponent(path.slice(3));
        setPanel('chat');
        if (id && loadedConversationRef.current !== id) void loadConversation(id);
        return;
      }
      if (path === '/watchlist') setPanel('watchlist');
      else if (path === '/watched') setPanel('watched');
      else if (path === '/taste') setPanel('taste');
      else if (path.startsWith('/settings/')) setPanel('settings');
      else if (path === '/') {
        const query = new URLSearchParams(location.search).get('q') || '';
        if (query) {
          if (query !== lastRoutedSearchRef.current) {
            // A URL we have not searched yet (first load, back/forward).
            setSearch(query);
            void doSearch(null, query);
          } else {
            setPanel('search');
          }
        } else {
          lastRoutedSearchRef.current = '';
          setPanel('chat');
        }
      }
    }, 0);
    return () => window.clearTimeout(timer);
    // Typing in the search box must not re-run route handling (each keystroke
    // used to retry a failed conversation load); only the URL drives this.
  }, [location.pathname, location.search, loadConversation, doSearch]);

  useEffect(() => {
    let target = '/';
    if (panel === 'movie' && selected) target = `/movie/${encodeURIComponent(selected)}`;
    else if (panel === 'person' && selectedPerson) target = `/person/${encodeURIComponent(selectedPerson)}`;
    else if (panel === 'chat' && cid) target = `/c/${encodeURIComponent(cid)}`;
    else if (panel === 'watchlist') target = '/watchlist';
    else if (panel === 'watched') target = '/watched';
    else if (panel === 'settings') target = '/settings/account';
    else if (panel === 'taste') target = '/taste';
    else if (panel === 'search' && search.trim()) target = `/?q=${encodeURIComponent(search.trim())}`;
    const current = `${location.pathname}${location.search}`;
    if (target !== current) navigate(target, { replace: true });
  }, [panel, cid, selected, selectedPerson, search, location.pathname, location.search, navigate]);

  function openPerson(id) {
    setPersonOrigin(panel === 'person' ? personOrigin : panel);
    setSelectedPerson(String(id));
    setPanel('person');
  }

  function askGrace(prompt) {
    newConversation();
    setPendingPrompt(prompt);
  }

  async function logout() {
    conversationRequestRef.current += 1;
    try {
      // Unsent drafts may be personal; don't leave them for the next person on this device.
      Object.keys(localStorage)
        .filter((key) => key.startsWith('grace_draft_'))
        .forEach((key) => localStorage.removeItem(key));
    } catch {
      // Storage unavailable.
    }
    setCid(null);
    loadedConversationRef.current = null;
    queryClient.removeQueries({ queryKey: ['user-state'] });
    setConvos([]);
    setResults([]);
    setSelected(null);
    setPanel('chat');
    try {
      await signOut();
    } catch {
      setError('Could not contact the server to clear your session cookie.');
    }
    // Like any account-based app, logging out returns to the welcome screen.
    forgetGuest();
    navigate('/', { replace: true });
  }

  // Route params are strings and API ids are numbers; compare as strings.
  const activeConversation = cid == null ? undefined : convos.find((conversation) => String(conversation.id) === String(cid));
  const canManageConversation = Boolean(token && activeConversation);
  const shareOpen = canManageConversation && shareFor != null && String(shareFor) === String(activeConversation.id);

  function showPanel(next) {
    if (next === 'chat') setPanel('chat');
    else setPanel(next);
    setSidebarOpen(false);
  }

  return (
    <div className={`vx-app${sidebarCollapsed ? ' sidebar-collapsed' : ''}${sidebarOpen ? ' sidebar-open' : ''}`}>
      <button
        type="button"
        className={`sidebar-backdrop vx-backdrop${sidebarOpen ? ' visible' : ''}`}
        onClick={() => setSidebarOpen(false)}
        aria-label="Close navigation"
        tabIndex={sidebarOpen ? 0 : -1}
      />

      <Sidebar
        collapsed={sidebarCollapsed}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        onCollapse={sidebar.close}
        onExpand={sidebar.reopen}
        token={token}
        user={user}
        state={state}
        panel={panel}
        onNewChat={newConversation}
        onShowPanel={showPanel}
        onNavigate={(path, options) => {
          setSidebarOpen(false);
          navigate(path, options);
        }}
        onLogout={logout}
        search={search}
        setSearch={setSearch}
        onSearch={(event) => {
          setSidebarOpen(false);
          return doSearch(event);
        }}
        movieSearchRef={movieSearchRef}
        onPickMovie={(id) => {
          setMovieOrigin('search');
          setSelected(String(id));
          setPanel('movie');
          setSidebarOpen(false);
        }}
        onPickPerson={(id) => {
          openPerson(id);
          setSidebarOpen(false);
        }}
        convos={convos}
        conversationGroups={conversationGroups}
        cid={cid}
        onOpenConversation={(id) => void loadConversation(id)}
        onRenameConversation={renameConversation}
        onTogglePin={(conversation) => toggleConversationFlag(conversation, 'pin')}
        onToggleArchive={(conversation) => toggleConversationFlag(conversation, 'archive')}
        onDeleteConversation={deleteConversation}
        onExportConversation={exportConversation}
        projects={projectsApi.projects}
        activeProject={projectsApi.activeProject}
        onOpenProject={(id) => {
          projectsApi.setActiveProjectId(id);
          newConversation();
        }}
        onCreateProject={projectsApi.create}
        onUpdateProject={projectsApi.update}
        onDeleteProject={projectsApi.remove}
        onMoveToProject={async (conversation, projectId) => {
          try {
            await projectsApi.moveConversation(conversation.id, projectId);
            setConvos((current) => (
              activeProjectId != null && projectId !== activeProjectId
                ? current.filter((item) => item.id !== conversation.id)
                : current.map((item) => (item.id === conversation.id ? { ...item, project_id: projectId } : item))
            ));
            const target = projectsApi.projects.find((item) => item.id === projectId);
            notify(target ? `Moved to ${target.name}.` : 'Removed from the project.');
          } catch (moveError) {
            setError(userFacingError(moveError));
          }
        }}
        showArchived={showArchived}
        setShowArchived={setShowArchived}
        conversationSearch={conversationSearch}
        setConversationSearch={setConversationSearch}
        conversationSearchRef={conversationSearchRef}
        onLoadMoreConversations={() => loadConversationPage()}
        onOpenMovieFromNotification={(id) => {
          setMovieOrigin('chat');
          setSelected(String(id));
          setPanel('movie');
        }}
      />

      <div className="vx-main">
        <header className="vx-topbar">
          <button
            type="button"
            className="vx-icon-btn vx-mobile-menu mobile-menu"
            onClick={() => setSidebarOpen((value) => !value)}
            aria-label="Toggle navigation"
            aria-expanded={sidebarOpen}
            aria-controls="main-sidebar"
          >
            <Icon name="menu" size={20} />
          </button>
          <div className="vx-topbar-title" />
          <div className="vx-topbar-actions">
            {canManageConversation && (
              <div className="share-anchor">
                <button
                  type="button"
                  className="vx-btn vx-btn-secondary vx-btn-sm"
                  data-share-trigger
                  aria-haspopup="dialog"
                  aria-expanded={shareOpen}
                  onClick={() => setShareFor(shareOpen ? null : activeConversation.id)}
                >
                  <Icon name="share" size={15} />
                  Share
                </button>
                {shareOpen && (
                  <ShareDialog
                    conversationId={activeConversation.id}
                    notify={notify}
                    onClose={closeShare}
                  />
                )}
              </div>
            )}
            {!token && (
              <>
                <button type="button" className="vx-btn vx-btn-ghost vx-btn-sm" onClick={() => navigate('/login')}>Log in</button>
                <button type="button" className="vx-btn vx-btn-primary vx-btn-sm" onClick={() => navigate('/signup')}>Sign up</button>
              </>
            )}
          </div>
        </header>

      <WorkspaceRoutes
        searchQuery={lastRoutedSearchRef.current || ''}
        panel={panel}
        token={token}
        cid={cid}
        projectId={activeProjectId}
        setCid={setActiveConversationId}
        messages={messages}
        setMessages={setMessages}
        activeMovieTitle={activeMovieTitle}
        setActiveMovieTitle={setActiveMovieTitle}
        providerData={providerData}
        setProviderData={setProviderData}
        convos={convos}
        setConvos={setConvos}
        error={error}
        setError={setError}
        isOnline={isOnline}
        state={state}
        onLoadOlderMessages={loadOlderMessages}
        loadingPanel={loadingPanel}
        results={results}
        savingMovie={savingMovie}
        onOpenMovie={(id) => {
          setMovieOrigin(['watchlist', 'watched', 'person', 'chat'].includes(panel) ? panel : 'search');
          setSelected(id);
          setPanel('movie');
        }}
        onToggleSearchWatchlist={toggleSearchWatchlist}
        selected={selected}
        movieOrigin={movieOrigin}
        onBack={() => setPanel(movieOrigin)}
        selectedPerson={selectedPerson}
        personOrigin={personOrigin}
        onBackFromPerson={() => setPanel(personOrigin === 'person' ? 'chat' : personOrigin)}
        onOpenPerson={openPerson}
        onAsk={askGrace}
        onRefreshState={refresh}
        pendingPrompt={pendingPrompt}
        onNavigate={navigate}
        onPendingPromptConsumed={() => setPendingPrompt('')}
        peopleResults={peopleResults}
        conversationResults={conversationResults}
        onOpenConversation={(id) => void loadConversation(id)}
        recentSearches={recentSearches}
        onRecentSearch={(query) => {
          setSearch(query);
          void doSearch(null, query);
        }}
        onStateChange={refresh}
        onRate={rateMovie}
        onRequireAuth={(kind) => {
          if (!token) {
            navigate('/signup', {
              state: {
                prompt: kind === 'watchlist'
                  ? 'Create a free account to save movies to your watchlist.'
                  : kind === 'rating'
                    ? 'Create a free account to rate movies.'
                    : 'Create a free account to keep track of movies you have watched.',
              },
            });
            return true;
          }
          return false;
        }}
        onRemove={removeCollectionMovie}
        onMarkWatched={markCollectionMovieWatched}
      />
      </div>

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        recentSearches={recentSearches}
        onSearchMovies={(query) => {
          setSearch(query);
          void doSearch(null, query);
        }}
        conversations={convos.slice(0, 20).map((conversation) => ({
          id: conversation.id,
          title: conversation.title,
          open: () => void loadConversation(conversation.id),
        }))}
        actions={[
          { id: 'new-chat', label: t('palette.newChat'), icon: 'plus', shortcut: 'Ctrl+Shift+O', run: newConversation },
          { id: 'search', label: t('palette.search'), icon: 'search', shortcut: '/', run: () => { sidebar.reopen(); window.requestAnimationFrame(() => movieSearchRef.current?.focus()); } },
          { id: 'watchlist', label: t('palette.watchlist'), icon: 'bookmark', run: () => setPanel('watchlist') },
          { id: 'watched', label: t('palette.watched'), icon: 'check', run: () => setPanel('watched') },
          { id: 'taste', label: t('palette.taste'), icon: 'heart', run: () => navigate(token ? '/taste' : '/login') },
          { id: 'settings', label: t('palette.settings'), icon: 'settings', run: () => navigate(token ? '/settings/account' : '/login') },
          { id: 'help', label: t('palette.help'), icon: 'lightbulb', run: () => navigate('/settings/help') },
          { id: 'sidebar', label: t('palette.sidebar'), icon: 'menu', shortcut: 'Ctrl+\\', run: () => (sidebarCollapsed ? sidebar.reopen() : sidebar.close()) },
          { id: 'shortcuts', label: 'Keyboard shortcuts', icon: 'lightbulb', shortcut: '?', run: () => setShortcutsOpen(true) },
        ]}
      />

      {shortcutsOpen && <ShortcutsDialog onClose={closeShortcuts} />}

      <ToastViewport
        toasts={toasts}
        onDismiss={dismissToast}
      />

      {undoDelete && (
        <div className="toast" role="status">
          <span>Conversation deleted.</span>
          <button type="button" onClick={restoreConversation}>Undo</button>
          <button type="button" onClick={() => setUndoDelete(null)} aria-label="Dismiss undo">×</button>
        </div>
      )}

      {undoCollection && (
        <div className="toast" role="status">
          <span>Removed {undoCollection.title} from {undoCollection.collection}.</span>
          <button type="button" onClick={undoCollectionRemoval}>Undo</button>
          <button type="button" onClick={() => setUndoCollection(null)} aria-label="Dismiss undo">×</button>
        </div>
      )}

    </div>
  );
}

export default App;