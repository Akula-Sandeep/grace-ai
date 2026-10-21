import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import useProjects from '../useProjects';
import { api } from '../../../lib/api';

vi.mock('../../../lib/api', () => ({ api: vi.fn() }));

const dateNight = { id: 3, name: 'Date night', instructions: 'Under two hours.' };

describe('useProjects', () => {
  beforeEach(() => {
    api.mockReset();
  });

  it('loads, creates, updates, opens and removes projects', async () => {
    api.mockResolvedValueOnce({ items: [dateNight] });
    const { result } = renderHook(() => useProjects('token'));
    await waitFor(() => expect(result.current.projects).toEqual([dateNight]));

    api.mockResolvedValueOnce({ id: 9, name: 'Horror', instructions: '' });
    await act(async () => {
      await result.current.create({ name: 'Horror', instructions: '' });
    });
    expect(api).toHaveBeenLastCalledWith('/projects', { method: 'POST', body: JSON.stringify({ name: 'Horror', instructions: '' }) });
    expect(result.current.projects.map((p) => p.id)).toEqual([9, 3]);

    api.mockResolvedValueOnce({ name: 'Date night in' });
    await act(async () => {
      await result.current.update(3, { name: 'Date night in' });
    });
    expect(result.current.projects.find((p) => p.id === 3).name).toBe('Date night in');

    act(() => result.current.setActiveProjectId(3));
    expect(result.current.activeProject.id).toBe(3);

    api.mockResolvedValueOnce(undefined);
    await act(async () => {
      await result.current.remove(3);
    });
    expect(result.current.projects.map((p) => p.id)).toEqual([9]);
    expect(result.current.activeProject).toBeNull();
  });

  it('moves a chat into a project and refreshes the counts', async () => {
    api.mockResolvedValueOnce({ items: [] });
    const { result } = renderHook(() => useProjects('token'));
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));
    api.mockResolvedValueOnce(undefined).mockResolvedValueOnce({ items: [{ ...dateNight, conversation_count: 1 }] });
    await act(async () => {
      await result.current.moveConversation(12, 3);
    });
    expect(api).toHaveBeenCalledWith('/conversations/12/project', { method: 'PATCH', body: JSON.stringify({ project_id: 3 }) });
    await waitFor(() => expect(result.current.projects[0].conversation_count).toBe(1));
  });

  it('a guest has no projects and a failed load leaves the section empty', async () => {
    const guest = renderHook(() => useProjects(null));
    expect(guest.result.current.projects).toEqual([]);
    await act(async () => {
      await guest.result.current.refresh();
    });
    expect(api).not.toHaveBeenCalled();

    api.mockRejectedValueOnce(new Error('offline'));
    const { result } = renderHook(() => useProjects('token'));
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));
    expect(result.current.projects).toEqual([]);
  });
});
