import { useEffect, useRef, useState } from 'react';
import Icon from '../components/Icon';

/** Create or edit a project: a name and the instructions every chat in it follows. */
export function ProjectEditor({ project, onSave, onDelete, onClose }) {
  const [name, setName] = useState(project?.name || '');
  const [instructions, setInstructions] = useState(project?.instructions || '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);
  const nameRef = useRef(null);

  useEffect(() => {
    nameRef.current?.focus();
    const escape = (event) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', escape);
    return () => document.removeEventListener('keydown', escape);
  }, [onClose]);

  async function submit(event) {
    event.preventDefault();
    if (!name.trim() || saving) return;
    setSaving(true);
    setError('');
    try {
      await onSave({ name: name.trim(), instructions: instructions.trim() });
      onClose();
    } catch (err) {
      setError(err?.message || 'Could not save the project.');
      setSaving(false);
    }
  }

  return (
    <div className="vx-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <form className="vx-modal project-editor" role="dialog" aria-modal="true" aria-labelledby="project-editor-title" onSubmit={submit}>
        <h2 id="project-editor-title">{project ? 'Edit project' : 'New project'}</h2>
        <label htmlFor="project-name">Name</label>
        <input
          id="project-name"
          ref={nameRef}
          value={name}
          maxLength={80}
          placeholder="Date night"
          onChange={(event) => setName(event.target.value)}
          required
        />
        <label htmlFor="project-instructions">Instructions for every chat in this project</label>
        <textarea
          id="project-instructions"
          rows={5}
          maxLength={2000}
          value={instructions}
          placeholder="We're two adults. Keep it under two hours. No horror."
          onChange={(event) => setInstructions(event.target.value)}
        />
        <p className="project-editor-hint">Grace follows these in every chat here, unless a message asks otherwise.</p>
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="project-editor-actions">
          {project && onDelete && (
            confirmDelete ? (
              <button type="button" className="vx-btn vx-btn-danger vx-btn-sm" onClick={() => void onDelete().then(onClose)}>
                Remove project (chats stay)
              </button>
            ) : (
              <button type="button" className="vx-btn vx-btn-ghost vx-btn-sm" onClick={() => setConfirmDelete(true)}>
                Remove…
              </button>
            )
          )}
          <span className="project-editor-spacer" />
          <button type="button" className="vx-btn vx-btn-ghost vx-btn-sm" onClick={onClose}>Cancel</button>
          <button type="submit" className="vx-btn vx-btn-primary vx-btn-sm" disabled={!name.trim() || saving}>
            {saving ? 'Saving…' : project ? 'Save' : 'Create project'}
          </button>
        </div>
      </form>
    </div>
  );
}

/** The sidebar's project list; opening one shows only its chats, and new chats start in it. */
function ProjectsSection({ projects, activeProject, onOpen, onCreate, onUpdate, onDelete }) {
  const [editing, setEditing] = useState(null); // null | 'new' | project

  return (
    <section className="vx-projects" aria-label="Projects">
      <div className="vx-recents-head">
        <h2>Projects</h2>
        <button type="button" className="vx-icon-btn vx-projects-add" onClick={() => setEditing('new')} aria-label="New project" title="New project">
          <Icon name="plus" size={15} />
        </button>
      </div>
      {projects.length > 0 ? (
        <ul>
          {projects.map((project) => (
            <li key={project.id} className={activeProject?.id === project.id ? 'is-selected' : ''}>
              <button
                type="button"
                className="vx-project-open"
                aria-current={activeProject?.id === project.id ? 'true' : undefined}
                onClick={() => onOpen(activeProject?.id === project.id ? null : project.id)}
              >
                <Icon name="folder" size={15} />
                <span>{project.name}</span>
                {project.conversation_count > 0 && <em>{project.conversation_count}</em>}
              </button>
              <button type="button" className="vx-convo-more" aria-label={`Edit project ${project.name}`} onClick={() => setEditing(project)}>
                <Icon name="edit" size={14} />
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="vx-projects-empty">Group chats with shared instructions, like “Date night”.</p>
      )}
      {editing && (
        <ProjectEditor
          project={editing === 'new' ? null : editing}
          onSave={(values) => (editing === 'new' ? onCreate(values).then((project) => onOpen(project.id)) : onUpdate(editing.id, values))}
          onDelete={editing === 'new' ? null : () => onDelete(editing.id)}
          onClose={() => setEditing(null)}
        />
      )}
    </section>
  );
}

export default ProjectsSection;
