import Icon from '../components/Icon';
import NotificationCenter from '../components/NotificationCenter';
import SearchSuggest from '../components/SearchSuggest';
import { t } from '../lib/i18n';
import AccountMenu from './AccountMenu';
import ConversationItem from './ConversationItem';
import ProjectsSection from './ProjectsSection';

function NavItem({ icon, label, count, active, collapsed, onClick }) {
  return (
    <button
      type="button"
      className={`vx-nav-item${active ? ' is-active' : ''}`}
      onClick={onClick}
      aria-current={active ? 'page' : undefined}
      title={collapsed ? label : undefined}
      aria-label={collapsed ? label : undefined}
    >
      <Icon name={icon} size={18} />
      <span className="vx-nav-label">{label}</span>
      {typeof count === 'number' && count > 0 && <em className="vx-nav-count">{count}</em>}
    </button>
  );
}

function Sidebar({
  collapsed,
  open,
  onClose,
  onCollapse,
  onExpand,
  token,
  user,
  state,
  panel,
  onNewChat,
  onShowPanel,
  onNavigate,
  onLogout,
  search,
  setSearch,
  onSearch,
  movieSearchRef,
  onPickMovie,
  onPickPerson,
  convos,
  conversationGroups,
  cid,
  onOpenConversation,
  onRenameConversation,
  onTogglePin,
  onToggleArchive,
  onDeleteConversation,
  onExportConversation,
  projects = [],
  activeProject = null,
  onOpenProject,
  onCreateProject,
  onUpdateProject,
  onDeleteProject,
  onMoveToProject,
  showArchived,
  setShowArchived,
  conversationSearch,
  setConversationSearch,
  conversationSearchRef,
  onLoadMoreConversations,
  onOpenMovieFromNotification,
}) {
  const rail = collapsed && !open;

  return (
    <aside
      id="main-sidebar"
      className={`vx-sidebar sidebar${open ? ' open' : ''}${rail ? ' is-rail' : ''}`}
      aria-label="Sidebar"
    >
      <div className="vx-sidebar-head">
        <button type="button" className="vx-sidebar-brand" onClick={onNewChat} aria-label="Grace, start a new chat" title="New chat">
          <span className="vx-brand-mark" aria-hidden="true"><Icon name="spark" size={17} strokeWidth={1.6} /></span>
          <span className="vx-brand-name">Grace</span>
        </button>
        {rail ? (
          <button type="button" className="vx-icon-btn vx-sidebar-toggle" onClick={onExpand} aria-label="Open sidebar" title="Open sidebar (Ctrl+\)">
            <Icon name="panelOpen" size={18} />
          </button>
        ) : (
          <button
            type="button"
            className="vx-icon-btn vx-sidebar-toggle sidebar-close"
            onClick={open ? onClose : onCollapse}
            aria-label="Close sidebar"
            title="Close sidebar (Ctrl+\)"
          >
            <Icon name={open ? 'close' : 'panelClose'} size={18} />
          </button>
        )}
      </div>

      <button type="button" className="vx-new-chat new" onClick={onNewChat} title={rail ? t('nav.newChat') : undefined} aria-label={rail ? t('nav.newChat') : undefined}>
        <span className="vx-new-chat-icon" aria-hidden="true"><Icon name="plus" size={16} strokeWidth={2.2} /></span>
        <span className="vx-nav-label">{t('nav.newChat')}</span>
      </button>

      {rail ? (
        <button
          type="button"
          className="vx-nav-item"
          onClick={() => {
            onExpand();
            window.requestAnimationFrame(() => movieSearchRef.current?.focus());
          }}
          aria-label="Search movies and people"
          title="Search movies and people (/)"
        >
          <Icon name="search" size={18} />
        </button>
      ) : (
        <div className="vx-sidebar-search">
          <SearchSuggest
            inputRef={movieSearchRef}
            value={search}
            onChange={setSearch}
            onSubmit={onSearch}
            placeholder={t('search.placeholder')}
            onPickMovie={onPickMovie}
            onPickPerson={onPickPerson}
          />
        </div>
      )}

      <nav className="vx-nav side-nav" aria-label="Library">
        <NavItem icon="message" label="Chats" active={panel === 'chat'} collapsed={rail} onClick={() => onShowPanel('chat')} />
        <NavItem icon="bookmark" label={t('nav.watchlist')} count={state?.watchlist?.length} active={panel === 'watchlist'} collapsed={rail} onClick={() => onShowPanel('watchlist')} />
        <NavItem icon="check" label={t('nav.watched')} count={state?.watched?.length} active={panel === 'watched'} collapsed={rail} onClick={() => onShowPanel('watched')} />
        <NavItem icon="heart" label={t('nav.taste')} active={panel === 'taste'} collapsed={rail} onClick={() => (token ? onShowPanel('taste') : onNavigate('/signup', { state: { prompt: 'Create a free account so Grace can remember your taste.' } }))} />
      </nav>

      {!rail && token && (
        <div className="vx-recents" aria-label="Conversations">
          {token ? (
            <>
              {!showArchived && onOpenProject && (
                <ProjectsSection
                  projects={projects}
                  activeProject={activeProject}
                  onOpen={onOpenProject}
                  onCreate={onCreateProject}
                  onUpdate={onUpdateProject}
                  onDelete={onDeleteProject}
                />
              )}
              <div className="vx-recents-head">
                <h2>{showArchived ? 'Archived' : activeProject ? activeProject.name : 'Recents'}</h2>
                <button
                  type="button"
                  className="vx-link-quiet"
                  onClick={() => setShowArchived((value) => !value)}
                  aria-pressed={showArchived}
                >
                  {showArchived ? 'Back to recents' : 'Archived'}
                </button>
              </div>
              {activeProject && !showArchived && (
                <div className="vx-project-filter">
                  <strong><Icon name="folder" size={14} />Chats in {activeProject.name}</strong>
                  {activeProject.instructions ? <p>{activeProject.instructions}</p> : <p>No instructions yet. Edit the project to add some.</p>}
                  <button type="button" className="vx-link-quiet" onClick={() => onOpenProject(null)}>All chats</button>
                </div>
              )}
              <div className="vx-recents-filter">
                <Icon name="search" size={14} />
                <input
                  ref={conversationSearchRef}
                  value={conversationSearch}
                  onChange={(event) => setConversationSearch(event.target.value)}
                  placeholder="Search chats"
                  aria-label="Search conversations by title or message"
                />
              </div>
              <div
                className="vx-recents-scroll conversation-list"
                onScroll={(event) => {
                  const { scrollTop, scrollHeight, clientHeight } = event.currentTarget;
                  if (scrollHeight - scrollTop - clientHeight < 120) onLoadMoreConversations();
                }}
              >
                {convos.length ? conversationGroups.map((group) => (
                  <section className="vx-recents-group" key={group.label} aria-label={group.label}>
                    <h3>{group.label}</h3>
                    <ul>
                      {group.conversations.map((conversation) => (
                        <ConversationItem
                          key={conversation.id}
                          conversation={conversation}
                          selected={cid != null && String(cid) === String(conversation.id)}
                          onOpen={onOpenConversation}
                          onRename={onRenameConversation}
                          onTogglePin={onTogglePin}
                          onToggleArchive={onToggleArchive}
                          onDelete={onDeleteConversation}
                          onExport={onExportConversation}
                          projects={projects}
                          onMoveToProject={onMoveToProject}
                        />
                      ))}
                    </ul>
                  </section>
                )) : (
                  <p className="vx-recents-empty">
                    {conversationSearch ? 'No chats match that.' : showArchived ? 'Nothing archived.' : 'Your chats will appear here.'}
                  </p>
                )}
              </div>
            </>
          ) : null}
        </div>
      )}

      {token && (
      <div className="vx-sidebar-foot sidebar-account">
        {token ? (
          <div className="vx-sidebar-account sidebar-account-row">
            <AccountMenu user={user} onNavigate={onNavigate} onLogout={onLogout} compact={rail} />
            {!rail && (
              <NotificationCenter
                userId={user?.id}
                onOpenMovie={onOpenMovieFromNotification}
                onOpenSettings={() => onNavigate('/settings/personalisation')}
                onNavigate={onNavigate}
              />
            )}
          </div>
        ) : null}
      </div>
      )}
    </aside>
  );
}

export default Sidebar;
