import { useCallback, useEffect, useState } from 'react';
import { api } from '../../lib/api';

/**
 * The signed-in user's projects (C5) and which one the sidebar is showing.
 * Chats started while a project is open are created inside it.
 */
export default function useProjects(token) {
  const [projects, setProjects] = useState([]);
  const [activeProjectId, setActiveProjectId] = useState(null);

  const refresh = useCallback(async () => {
    if (!token) return;
    const page = await api('/projects');
    setProjects(Array.isArray(page?.items) ? page.items : []);
  }, [token]);

  useEffect(() => {
    if (!token) return undefined;
    let live = true;
    api('/projects')
      .then((page) => {
        if (live) setProjects(Array.isArray(page?.items) ? page.items : []);
      })
      .catch(() => {
        // The sidebar works without projects; the section simply stays empty.
      });
    return () => {
      live = false;
    };
  }, [token]);

  const create = useCallback(async ({ name, instructions }) => {
    const project = await api('/projects', { method: 'POST', body: JSON.stringify({ name, instructions }) });
    setProjects((current) => [project, ...current]);
    return project;
  }, []);

  const update = useCallback(async (id, changes) => {
    const project = await api(`/projects/${id}`, { method: 'PATCH', body: JSON.stringify(changes) });
    setProjects((current) => current.map((item) => (item.id === id ? { ...item, ...project } : item)));
    return project;
  }, []);

  const remove = useCallback(async (id) => {
    await api(`/projects/${id}`, { method: 'DELETE' });
    setProjects((current) => current.filter((item) => item.id !== id));
    setActiveProjectId((current) => (current === id ? null : current));
  }, []);

  const moveConversation = useCallback(async (conversationId, projectId) => {
    await api(`/conversations/${conversationId}/project`, {
      method: 'PATCH',
      body: JSON.stringify({ project_id: projectId }),
    });
    void refresh().catch(() => {});
  }, [refresh]);

  const activeProject = token ? projects.find((item) => item.id === activeProjectId) || null : null;
  return { projects: token ? projects : [], activeProject, setActiveProjectId, create, update, remove, moveConversation, refresh };
}
